import json
import tempfile
import unittest
from pathlib import Path

from heritage_images.catalog import (
    APPLIED,
    DUPLICATE,
    PENDING,
    REJECTED,
    Catalog,
)
from heritage_images.partners import PartnerDirectory

FIXTURES = Path(__file__).parents[1] / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureScenarioTests(unittest.TestCase):
    """逐一核对 fixtures/federation_cases.json 中声明的可观察口径。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = PartnerDirectory.load(FIXTURES / "partners.json")
        self.scenarios = load_fixture("federation_cases.json")["cases"]
        self._catalog_count = 0

    def tearDown(self):
        self.temporary.cleanup()

    def fresh_catalog(self) -> Catalog:
        self._catalog_count += 1
        return Catalog(
            Path(self.temporary.name) / f"catalog-{self._catalog_count}.sqlite3",
            self.directory,
        )

    def _ids(self, rows, key):
        return [row[key] for row in rows]

    def _current_state(self, catalog):
        view = catalog.display_view()
        raw_shares = catalog.shares()
        return {
            "visible_object_ids": self._ids(view["visible_objects"], "object_id"),
            "tombstoned_object_ids": view["tombstoned_object_ids"],
            "active_share_activity_ids": [
                row["share_activity_id"] for row in raw_shares if row["active"]
            ],
            "observable_active_share_activity_ids": self._ids(view["active_shares"], "share_activity_id"),
            "withdrawn_share_activity_ids": self._ids(view["withdrawn_shares"], "share_activity_id"),
            "tombstone_hidden_share_activity_ids": view["tombstone_hidden_share_ids"],
            "pending_object_ids": view["pending_object_ids"],
        }

    def test_every_documented_scenario_matches_its_observable_contract(self):
        for case in self.scenarios:
            with self.subTest(case=case["name"]), self.fresh_catalog() as catalog:
                entries = [catalog.ingest(envelope) for envelope in case["envelopes"]]
                expected = case["expected"]
                self.assertEqual([entry.decision for entry in entries], expected["decisions"])
                self.assertEqual([entry.reason for entry in entries], expected["reasons"])
                state = self._current_state(catalog)
                for key, value in expected.items():
                    if key in ("decisions", "reasons", "final_content"):
                        continue
                    self.assertEqual(state[key], value, key)
                if "final_content" in expected:
                    target = next(
                        envelope["activity"]["object"]["id"]
                        for envelope in case["envelopes"]
                        if envelope["activity"].get("type") == "Update"
                        and isinstance(envelope["activity"].get("object"), dict)
                    )
                    contents = {row["object_id"]: row["content"] for row in catalog.objects()}
                    self.assertEqual(contents[target], expected["final_content"])

    def test_undo_announce_keeps_original_object_untouched(self):
        case = next(c for c in self.scenarios if c["name"] == "undo_announce")
        with self.fresh_catalog() as catalog:
            for envelope in case["envelopes"]:
                catalog.ingest(envelope)
            objects = {row["object_id"]: row for row in catalog.objects()}
            original = objects["https://museum-a.invalid/objects/7"]
            self.assertFalse(original["is_tombstone"])
            self.assertEqual(original["content"], "合成展览动态")
            share = catalog.shares()[0]
            self.assertFalse(share["active"])
            self.assertEqual(share["undo_activity_id"], "https://museum-b.invalid/a/9")
            # 事件依据完整保留：三条活动、三条台账、一条撤展记录。
            self.assertEqual(
                catalog.connection.execute("SELECT count(*) FROM activities").fetchone()[0], 3
            )
            self.assertEqual(
                catalog.connection.execute("SELECT count(*) FROM journal").fetchone()[0], 3
            )

    def test_journal_is_append_only_evidence(self):
        case = next(c for c in self.scenarios if c["name"] == "untrusted_delete")
        with self.fresh_catalog() as catalog:
            entries = [catalog.ingest(envelope) for envelope in case["envelopes"]]
            self.assertEqual(entries[1].decision, REJECTED)
            # 被拒活动不进活动表，但台账保留事件依据。
            stored = {
                row["activity_id"]
                for row in catalog.connection.execute("SELECT activity_id FROM activities").fetchall()
            }
            self.assertNotIn("https://museum-b.invalid/a/13", stored)
            rejected = catalog.journal()[-1]
            self.assertEqual(rejected.reason, "actor_is_not_object_owner")
            self.assertIn("retained", rejected.detail)
            # 原对象正文仍然保留。
            self.assertEqual(
                catalog.objects()[0]["content"], "待保护的原帖"
            )


class CatalogRuleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = PartnerDirectory.load(FIXTURES / "partners.json")
        self.catalog = Catalog(Path(self.temporary.name) / "catalog.sqlite3", self.directory)

    def tearDown(self):
        self.catalog.close()
        self.temporary.cleanup()

    def envelope(self, source, observation, activity):
        return {"source": source, "observation": observation, "activity": activity}

    def test_three_identities_are_stored_separately(self):
        self.catalog.ingest(self.envelope("museum-a", 1, {
            "id": "https://museum-a.invalid/a/1", "type": "Create",
            "actor": "https://museum-a.invalid/actors/main",
            "object": {
                "id": "https://museum-a.invalid/o/1", "type": "Note",
                "attributedTo": "https://museum-a.invalid/actors/main",
                "content": "正文", "to": ["https://www.w3.org/ns/activitystreams#Public"],
            },
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
        }))
        self.catalog.ingest(self.envelope("museum-b", 2, {
            "id": "https://museum-b.invalid/a/2", "type": "Announce",
            "actor": "https://museum-b.invalid/actors/main",
            "object": "https://museum-a.invalid/o/1",
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
        }))
        # 同一对象只有一份投影；转发是独立的展示关系行。
        self.assertEqual(len(self.catalog.objects()), 1)
        shares = self.catalog.shares()
        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0]["announcer"], "https://museum-b.invalid/actors/main")
        self.assertNotEqual(shares[0]["share_activity_id"], "https://museum-a.invalid/o/1")

    def test_reference_url_is_never_treated_as_content(self):
        entry = self.catalog.ingest(self.envelope("museum-b", 1, {
            "id": "https://museum-b.invalid/a/1", "type": "Announce",
            "actor": "https://museum-b.invalid/actors/main",
            "object": "https://museum-a.invalid/o/missing",
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
        }))
        self.assertEqual((entry.decision, entry.reason), (PENDING, "object_details_missing"))
        obj = self.catalog.objects()[0]
        self.assertFalse(obj["details_complete"])
        self.assertIsNone(obj["content"])
        # 详情未补前不进入可展示视图。
        self.assertEqual(self.catalog.display_view()["visible_objects"], [])
        self.assertEqual(self.catalog.display_view()["active_shares"], [])

    def test_remote_context_urls_are_ignored_not_fetched(self):
        import urllib.request

        def forbidden_open(*args, **kwargs):
            raise AssertionError("catalog must not perform any network access")

        original = urllib.request.urlopen
        urllib.request.urlopen = forbidden_open
        try:
            entry = self.catalog.ingest(self.envelope("museum-a", 1, {
                "@context": ["https://www.w3.org/ns/activitystreams", "https://context.invalid/x"],
                "id": "https://museum-a.invalid/a/1", "type": "Create",
                "actor": "https://museum-a.invalid/actors/main",
                "object": {
                    "id": "https://museum-a.invalid/o/1", "type": "Note",
                    "attributedTo": "https://museum-a.invalid/actors/main",
                    "content": "上下文不抓取",
                    "to": ["https://www.w3.org/ns/activitystreams#Public"],
                },
                "to": ["https://www.w3.org/ns/activitystreams#Public"],
            }))
            # 指向未配置域的引用同样只能落占位，不能触发抓取。
            foreign = self.catalog.ingest(self.envelope("museum-b", 2, {
                "id": "https://museum-b.invalid/a/2", "type": "Announce",
                "actor": "https://museum-b.invalid/actors/main",
                "object": "https://elsewhere.invalid/o/1",
                "to": ["https://www.w3.org/ns/activitystreams#Public"],
            }))
        finally:
            urllib.request.urlopen = original
        self.assertEqual(entry.decision, APPLIED)
        self.assertEqual(foreign.decision, PENDING)
        payload = json.loads(
            self.catalog.connection.execute(
                "SELECT payload FROM activities WHERE activity_id=?",
                ("https://museum-a.invalid/a/1",),
            ).fetchone()["payload"]
        )
        self.assertIn("https://context.invalid/x", payload["@context"])

    def test_foreign_domain_stub_is_hidden_from_display_but_kept_as_evidence(self):
        self.catalog.ingest(self.envelope("museum-b", 1, {
            "id": "https://museum-b.invalid/a/1", "type": "Announce",
            "actor": "https://museum-b.invalid/actors/main",
            "object": "https://elsewhere.invalid/o/9",
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
        }))
        all_objects = self.catalog.objects()
        self.assertEqual(all_objects, [])  # 未配置域不进馆方视图
        row = self.catalog.connection.execute(
            "SELECT * FROM objects WHERE object_id=?", ("https://elsewhere.invalid/o/9",)
        ).fetchone()
        self.assertEqual(row["configured_domain"], 0)
        self.assertEqual(self.catalog.display_view()["active_shares"], [])

    def test_duplicate_activity_changes_nothing(self):
        activity = {
            "id": "https://museum-a.invalid/a/1", "type": "Create",
            "actor": "https://museum-a.invalid/actors/main",
            "object": {
                "id": "https://museum-a.invalid/o/1", "type": "Note",
                "attributedTo": "https://museum-a.invalid/actors/main",
                "content": "原稿", "to": ["https://www.w3.org/ns/activitystreams#Public"],
            },
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
        }
        first = self.catalog.ingest(self.envelope("museum-a", 1, activity))
        second = self.catalog.ingest(self.envelope("museum-a", 2, dict(activity)))
        self.assertEqual(first.decision, APPLIED)
        self.assertEqual((second.decision, second.reason), (DUPLICATE, "activity_id_seen"))
        self.assertEqual(
            self.catalog.connection.execute("SELECT count(*) FROM activities").fetchone()[0], 1
        )

    def test_confirmed_withdrawal_survives_source_outage_and_late_delivery(self):
        # 建立并撤回一次转发。
        self.catalog.ingest(self.envelope("museum-a", 1, {
            "id": "https://museum-a.invalid/a/1", "type": "Create",
            "actor": "https://museum-a.invalid/actors/main",
            "object": {
                "id": "https://museum-a.invalid/o/1", "type": "Note",
                "attributedTo": "https://museum-a.invalid/actors/main",
                "content": "原稿", "to": ["https://www.w3.org/ns/activitystreams#Public"],
            },
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
        }))
        announce = {
            "id": "https://museum-b.invalid/a/2", "type": "Announce",
            "actor": "https://museum-b.invalid/actors/main",
            "object": "https://museum-a.invalid/o/1",
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
        }
        self.catalog.ingest(self.envelope("museum-b", 2, announce))
        self.catalog.ingest(self.envelope("museum-b", 3, {
            "id": "https://museum-b.invalid/a/3", "type": "Undo",
            "actor": "https://museum-b.invalid/actors/main",
            "object": "https://museum-b.invalid/a/2",
        }))
        withdrawn_seq = self.catalog.connection.execute(
            "SELECT withdrawn_seq FROM shares WHERE share_activity_id=?",
            ("https://museum-b.invalid/a/2",),
        ).fetchone()["withdrawn_seq"]

        # 模拟源站短时失联：采集侧没有任何新信封入库，状态保持不变。
        before = self.catalog.display_view()
        self.assertEqual(before["active_shares"], [])

        # 失联后重发旧 Announce（同 id）以及乱序的旧引用都不能复活展示。
        late = self.catalog.ingest(self.envelope("museum-b", 4, announce))
        self.assertEqual(late.decision, DUPLICATE)
        self.assertEqual(
            self.catalog.connection.execute(
                "SELECT withdrawn_seq FROM shares WHERE share_activity_id=?",
                ("https://museum-b.invalid/a/2",),
            ).fetchone()["withdrawn_seq"],
            withdrawn_seq,
        )
        self.assertEqual(self.catalog.display_view()["active_shares"], [])

    def test_persistence_across_reopen(self):
        self.catalog.ingest(self.envelope("museum-a", 1, {
            "id": "https://museum-a.invalid/a/1", "type": "Create",
            "actor": "https://museum-a.invalid/actors/main",
            "object": {
                "id": "https://museum-a.invalid/o/1", "type": "Note",
                "attributedTo": "https://museum-a.invalid/actors/main",
                "content": "持久化", "to": ["https://www.w3.org/ns/activitystreams#Public"],
            },
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
        }))
        path = self.catalog.path
        self.catalog.close()
        with Catalog.reopen(path) as reopened:
            view = reopened.display_view()
            self.assertEqual(view["visible_objects"][0]["content"], "持久化")
            with self.assertRaises(ValueError):
                reopened.ingest(self.envelope("museum-a", 2, {}))

    def test_malformed_envelopes_are_rejected_without_state(self):
        for bad in ({}, {"source": "museum-a"}, {"source": "museum-a", "observation": 1}):
            with self.subTest(bad=bad):
                entry = self.catalog.ingest(bad)
                self.assertEqual(entry.decision, REJECTED)
        self.assertEqual(self.catalog.journal()[-1].reason, "malformed_envelope")
        self.assertEqual(
            self.catalog.connection.execute("SELECT count(*) FROM activities").fetchone()[0], 0
        )

    def test_unsupported_activity_type_is_not_interpreted(self):
        entry = self.catalog.ingest(self.envelope("museum-a", 1, {
            "id": "https://museum-a.invalid/a/like", "type": "Like",
            "actor": "https://museum-a.invalid/actors/main",
            "object": "https://museum-a.invalid/o/1",
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
        }))
        self.assertEqual((entry.decision, entry.reason), (REJECTED, "activity_type_unsupported"))
        self.assertEqual(
            self.catalog.connection.execute("SELECT count(*) FROM activities").fetchone()[0], 0
        )

    def test_undo_targeting_non_announce_is_rejected(self):
        self.catalog.ingest(self.envelope("museum-a", 1, {
            "id": "https://museum-a.invalid/a/1", "type": "Create",
            "actor": "https://museum-a.invalid/actors/main",
            "object": {
                "id": "https://museum-a.invalid/o/1", "type": "Note",
                "attributedTo": "https://museum-a.invalid/actors/main",
                "content": "x", "to": ["https://www.w3.org/ns/activitystreams#Public"],
            },
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
        }))
        entry = self.catalog.ingest(self.envelope("museum-a", 2, {
            "id": "https://museum-a.invalid/a/2", "type": "Undo",
            "actor": "https://museum-a.invalid/actors/main",
            "object": "https://museum-a.invalid/a/1",
        }))
        self.assertEqual((entry.decision, entry.reason), (REJECTED, "undo_target_not_announce"))

    def test_returned_entries_are_exactly_the_append_only_journal(self):
        entries = []
        for index in range(3):
            entries.append(self.catalog.ingest(self.envelope("museum-a", index, {
                "id": f"https://museum-a.invalid/a/{index}", "type": "Like",
                "actor": "https://museum-a.invalid/actors/main",
                "object": f"https://museum-a.invalid/o/{index}",
                "to": ["https://www.w3.org/ns/activitystreams#Public"],
            })))
        journal = self.catalog.journal()
        self.assertEqual([e.seq for e in entries], [e.seq for e in journal])
        self.assertEqual([e.seq for e in journal], [1, 2, 3])
        self.assertTrue(all(e.decision == REJECTED for e in journal))
        self.assertTrue(all(e.activity_id for e in journal))


class PartnerDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = PartnerDirectory.load(FIXTURES / "partners.json")

    def test_trust_follows_local_registration_only(self):
        self.assertTrue(self.directory.trusts("museum-a", "https://museum-a.invalid/actors/main"))
        self.assertFalse(self.directory.trusts("museum-a", "https://museum-b.invalid/actors/main"))
        self.assertFalse(self.directory.trusts("museum-x", "https://museum-a.invalid/actors/main"))
        self.assertFalse(self.directory.is_configured_domain("https://elsewhere.invalid/o/1"))

    def test_invalid_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"
            path.write_text(json.dumps({"partners": [
                {"source": "z", "name": "z", "actors": ["not-a-url"], "domains": ["z.invalid"]}
            ]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                PartnerDirectory.load(path)

    def test_public_and_private_addressing_classification(self):
        from heritage_images.partners import is_private, is_public
        public = {"to": ["https://www.w3.org/ns/activitystreams#Public"]}
        directed = {"to": ["https://museum-b.invalid/actors/main"]}
        blind = {"bcc": ["https://museum-b.invalid/actors/main"]}
        self.assertTrue(is_public(public))
        self.assertFalse(is_private(public))
        self.assertTrue(is_private(directed))
        self.assertFalse(is_public(directed))
        self.assertTrue(is_private(blind))


if __name__ == "__main__":
    unittest.main()
