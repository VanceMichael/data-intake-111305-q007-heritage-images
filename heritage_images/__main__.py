import argparse
import json
import tempfile
from pathlib import Path

from .archive import Archive
from .catalog import Catalog
from .client import Client, collect
from .decoding import records
from .origin import serve
from .partners import PartnerDirectory


def capture(base_url, archive_path, source, path):
    archive = Archive(archive_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path)
        result = {"capture_id": capture_id, "status": response.status, "bytes": len(response.body), "url": response.url}
        if response.status == 200:
            result["source_records"] = records(response.body, response.headers.get("content-type", ""))
        result["archived_observations"] = len(archive.captures())
        return result
    finally:
        archive.close()


def fixture_path(name: str) -> Path:
    packaged = Path(__file__).parent / "fixtures" / name
    if packaged.is_file():
        return packaged
    return Path(__file__).parents[1] / "fixtures" / name


def build_catalog(partners_path: Path, catalog_path: Path) -> Catalog:
    return Catalog(catalog_path, PartnerDirectory.load(partners_path))


def run_catalog(partners_path: Path, catalog_path: Path, feed_path: Path) -> dict:
    """把一份已归档的来源记录读入对象级采集目录；全程不发起网络请求。"""
    media_type = "application/json" if feed_path.suffix.lower() == ".json" else "application/octet-stream"
    envelopes = records(feed_path.read_bytes(), media_type)
    with build_catalog(partners_path, catalog_path) as catalog:
        journal = [catalog.ingest(envelope) for envelope in envelopes]
        view = catalog.display_view()
    return {
        "ingested": len(journal),
        "decisions": [entry.decision for entry in journal],
        "reasons": [entry.reason for entry in journal],
        "display_view": view,
    }


def run_review(catalog_path: Path) -> dict:
    with Catalog.reopen(catalog_path) as catalog:
        return catalog.display_view()


def run_federation_demo() -> dict:
    """纯本地合成演练：不启动联邦连接、不发送任何活动。"""
    scenario = json.loads(fixture_path("federation_cases.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as temporary:
        catalog_path = Path(temporary) / "catalog.sqlite3"
        with build_catalog(fixture_path("partners.json"), catalog_path) as catalog:
            summaries = []
            for case in scenario["cases"]:
                entries = [catalog.ingest(envelope) for envelope in case["envelopes"]]
                summaries.append({
                    "case": case["name"],
                    "situation": case["situation"],
                    "decisions": [entry.decision for entry in entries],
                    "reasons": [entry.reason for entry in entries],
                })
            view = catalog.display_view()
            journal = [
                {
                    "seq": entry.seq,
                    "source": entry.source,
                    "activity_id": entry.activity_id,
                    "decision": entry.decision,
                    "reason": entry.reason,
                    "detail": entry.detail,
                }
                for entry in catalog.journal()
            ]
    return {
        "operation": "federation_catalog_demo",
        "network": "none (synthetic accounts, no federation messages sent)",
        "cases": summaries,
        "display_view": view,
        "journal": journal,
    }


def main():
    parser = argparse.ArgumentParser(description="来源响应接收与归档")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    subcommands.add_parser("federation-demo")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")
    catalog_parser = subcommands.add_parser("catalog")
    catalog_parser.add_argument("--partners", type=Path, required=True)
    catalog_parser.add_argument("--catalog", type=Path, required=True)
    catalog_parser.add_argument("--feed", type=Path, required=True)
    review_parser = subcommands.add_parser("review")
    review_parser.add_argument("--catalog", type=Path, required=True)
    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)
    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            result = capture(origin.base_url, Path(temporary) / "archive.sqlite3", "local-source", "/feed")
            print(json.dumps({"operation": "raw_capture", **result}, ensure_ascii=False, indent=2))
    elif args.command == "federation-demo":
        print(json.dumps(run_federation_demo(), ensure_ascii=False, indent=2))
    elif args.command == "catalog":
        print(json.dumps(run_catalog(args.partners, args.catalog, args.feed), ensure_ascii=False, indent=2))
    elif args.command == "review":
        print(json.dumps(run_review(args.catalog), ensure_ascii=False, indent=2))
    else:
        from .origin import load_scenario
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
