import json
import tempfile
import unittest
from pathlib import Path

from heritage_images.archive import Archive
from heritage_images.catalog import Catalog
from heritage_images.client import Client, collect
from heritage_images.decoding import records
from heritage_images.origin import serve
from heritage_images.partners import PartnerDirectory

FIXTURES = Path(__file__).parents[1] / "fixtures"


class CaptureToCatalogTests(unittest.TestCase):
    def _scenario(self):
        cases = json.loads((FIXTURES / "federation_cases.json").read_text(encoding="utf-8"))["cases"]
        case = next(c for c in cases if c["name"] == "undo_announce")
        return {
            "portal": {"csrf": "unused"},
            "routes": {
                "/feed": {"json": {"items": case["envelopes"]}},
                "/unstable": [
                    {"status": 503, "body": "temporarily unavailable"},
                    {"json": {"items": case["envelopes"]}},
                ],
            },
        }

    def test_http_capture_flows_into_object_level_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "raw.sqlite3"
            catalog_path = Path(temporary) / "catalog.sqlite3"
            with serve(self._scenario()) as origin:
                client = Client(origin.base_url)
                archive = Archive(archive_path)
                try:
                    capture_id, response = collect(client, archive, "museum-b")
                finally:
                    archive.close()
                self.assertEqual(response.status, 200)
                envelopes = records(response.body, response.headers["content-type"])
            self.assertEqual(len(envelopes), 3)
            # 原始归档与业务解释分层：归档里仍是一条未解释的来源观察。
            archive = Archive(archive_path)
            try:
                self.assertEqual(len(archive.captures()), 1)
                self.assertEqual(json.loads(archive.body(capture_id)), json.loads(response.body))
            finally:
                archive.close()
            with Catalog(catalog_path, PartnerDirectory.load(FIXTURES / "partners.json")) as catalog:
                entries = [catalog.ingest(envelope) for envelope in envelopes]
                self.assertEqual([e.decision for e in entries], ["applied", "applied", "applied"])
                view = catalog.display_view()
                self.assertEqual(len(view["visible_objects"]), 1)
                self.assertEqual(view["active_shares"], [])
                self.assertEqual(len(view["withdrawn_shares"]), 1)

    def test_source_outage_keeps_confirmed_state_and_retry_recovers(self):
        with tempfile.TemporaryDirectory() as temporary:
            catalog_path = Path(temporary) / "catalog.sqlite3"
            with serve(self._scenario()) as origin, Catalog(
                catalog_path, PartnerDirectory.load(FIXTURES / "partners.json")
            ) as catalog:
                client = Client(origin.base_url)
                # 首次 503：没有响应体可解释，目录保持空状态。
                failure = client.fetch("/unstable")
                self.assertEqual(failure.status, 503)
                self.assertEqual(catalog.display_view()["journal_size"], 0)
                # 源站恢复后完成同一份采集。
                response = client.fetch("/unstable")
                for envelope in records(response.body, response.headers["content-type"]):
                    catalog.ingest(envelope)
                withdrawn = catalog.display_view()["withdrawn_shares"]
                self.assertEqual(len(withdrawn), 1)
                # 再次模拟失联（重放 503 序列之外的失败不新增任何事件）。
                self.assertEqual(client.fetch("/rate-missing").status, 404)
                self.assertEqual(len(catalog.display_view()["withdrawn_shares"]), 1)


if __name__ == "__main__":
    unittest.main()
