import argparse
import json
import tempfile
from pathlib import Path

from . import fixture_path
from .archive import Archive
from .client import Client, collect
from .decoding import records
from .domain import DomainStore, load_partners
from .origin import load_scenario, serve


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


def ingest(archive_path, domain_path, partners_path):
    archive = Archive(archive_path)
    try:
        domain = DomainStore(domain_path, load_partners(partners_path))
        try:
            return domain.ingest(archive)
        finally:
            domain.close()
    finally:
        archive.close()


def domain_view(domain_path, source):
    domain = DomainStore(domain_path)
    try:
        return domain.view(source)
    finally:
        domain.close()


def domain_events(domain_path, source):
    domain = DomainStore(domain_path)
    try:
        return {"ingest_log": domain.ingest_log(), "activity_events": domain.events(source)}
    finally:
        domain.close()


def drill():
    """空库启动合成来源，完成捕获、领域采集并输出资料员视图，结束自动清理。"""
    with tempfile.TemporaryDirectory() as temporary, serve(load_scenario(fixture_path("domain_origin.json"))) as origin:
        archive = Archive(Path(temporary) / "raw.sqlite3")
        domain = DomainStore(Path(temporary) / "domain.sqlite3", load_partners())
        try:
            capture_id, response = collect(Client(origin.base_url), archive, "local-gateway", "/feed")
            summary = domain.ingest(archive)
            return {
                "operation": "domain_drill",
                "capture": {"capture_id": capture_id, "status": response.status, "bytes": len(response.body)},
                "ingest": summary,
                "view": domain.view(),
            }
        finally:
            domain.close()
            archive.close()


def main():
    parser = argparse.ArgumentParser(description="来源响应接收与归档")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    subcommands.add_parser("drill")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")
    interpreter = subcommands.add_parser("ingest")
    interpreter.add_argument("--archive", type=Path, required=True)
    interpreter.add_argument("--domain", type=Path, required=True)
    interpreter.add_argument("--partners", type=Path)
    inspector = subcommands.add_parser("view")
    inspector.add_argument("--domain", type=Path, required=True)
    inspector.add_argument("--source")
    evidence = subcommands.add_parser("events")
    evidence.add_argument("--domain", type=Path, required=True)
    evidence.add_argument("--source")
    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)
    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            result = capture(origin.base_url, Path(temporary) / "archive.sqlite3", "local-source", "/feed")
            print(json.dumps({"operation": "raw_capture", **result}, ensure_ascii=False, indent=2))
    elif args.command == "drill":
        print(json.dumps(drill(), ensure_ascii=False, indent=2))
    elif args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path), ensure_ascii=False, indent=2))
    elif args.command == "ingest":
        print(json.dumps(ingest(args.archive, args.domain, args.partners), ensure_ascii=False, indent=2))
    elif args.command == "view":
        print(json.dumps(domain_view(args.domain, args.source), ensure_ascii=False, indent=2))
    elif args.command == "events":
        print(json.dumps(domain_events(args.domain, args.source), ensure_ascii=False, indent=2))
    else:
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
