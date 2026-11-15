import json
import tempfile
import unittest
from pathlib import Path

from heritage_images import fixture_path
from heritage_images.__main__ import drill
from heritage_images.archive import Archive
from heritage_images.client import Client, collect
from heritage_images.domain import DomainStore, load_partners
from heritage_images.origin import load_scenario, serve

PUBLIC = "https://www.w3.org/ns/activitystreams#Public"
ACTOR_A = "https://museum-a.invalid/actors/main"
ACTOR_B = "https://museum-b.invalid/actors/main"
OBJECT_7 = "https://museum-a.invalid/objects/7"
OBJECT_9 = "https://museum-a.invalid/objects/9"
ANNOUNCE_8 = "https://museum-b.invalid/a/8"


class DomainCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.archive = Archive(root / "raw.sqlite3")
        self.domain = DomainStore(root / "domain.sqlite3", load_partners())

    def tearDown(self):
        self.domain.close()
        self.archive.close()
        self.temporary.cleanup()

    def capture(self, origin, path):
        return collect(Client(origin.base_url), self.archive, "drill", path)

    def feed(self, records):
        body = json.dumps({"items": records}, ensure_ascii=False).encode()
        return self.archive.append("synthetic", "/feed", 200, {"content-type": "application/json"}, body)

    def ingest(self):
        return self.domain.ingest(self.archive)

    def partner(self, source, view=None):
        view = self.domain.view() if view is None else view
        matches = [entry for entry in view["partners"] if entry["source"] == source]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def object_entry(self, source, object_id):
        matches = [item for item in self.partner(source)["objects"] if item["object_id"] == object_id]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def forward_entry(self, source, announce_id):
        matches = [item for item in self.partner(source)["forwards"] if item["announce_id"] == announce_id]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def decisions(self):
        return {event["activity_id"]: (event["decision"], event["detail"]) for event in self.domain.events()}


class BaseScenarioTests(DomainCase):
    def test_undo_announce_keeps_original_object(self):
        with serve(load_scenario()) as origin:
            self.capture(origin, "/feed")
        summary = self.ingest()
        self.assertEqual(summary["records"], 3)
        obj = self.object_entry("museum-a", OBJECT_7)
        self.assertEqual(obj["status"], "current")
        self.assertTrue(obj["displayable"])
        forward = self.forward_entry("museum-b", ANNOUNCE_8)
        self.assertEqual(forward["status"], "withdrawn")
        self.assertFalse(forward["displayable"])
        self.assertEqual(forward["object_status"], "current")

    def test_untrusted_delete_keeps_object(self):
        with serve(load_scenario(fixture_path("domain_origin.json"))) as origin:
            self.capture(origin, "/observations/1")
            self.capture(origin, "/observations/4")
        self.ingest()
        self.assertEqual(self.object_entry("museum-a", OBJECT_7)["status"], "current")
        self.assertEqual(self.decisions()["https://museum-b.invalid/a/10"], ("rejected", "not_object_owner"))


class OrderingTests(DomainCase):
    def setUp(self):
        super().setUp()
        self.origin_context = serve(load_scenario(fixture_path("domain_origin.json")))
        self.origin = self.origin_context.__enter__()

    def tearDown(self):
        self.origin_context.__exit__(None, None, None)
        super().tearDown()

    def observe(self, *numbers):
        for number in numbers:
            self.capture(self.origin, "/observations/" + str(number))
        return self.ingest()

    def test_reference_before_object_stays_pending_then_resolves(self):
        self.observe(5)
        forward = self.forward_entry("museum-b", "https://museum-b.invalid/a/11")
        self.assertEqual(forward["object_status"], "pending")
        self.assertFalse(forward["displayable"])
        self.assertEqual(self.partner("museum-a")["objects"], [])
        self.observe(6)
        forward = self.forward_entry("museum-b", "https://museum-b.invalid/a/11")
        self.assertEqual(forward["object_status"], "current")
        self.assertTrue(forward["displayable"])
        obj = self.object_entry("museum-a", OBJECT_9)
        self.assertEqual(obj["content"]["content"], "合成夜场导览")

    def test_reference_only_update_is_deferred(self):
        self.observe(6, 7)
        obj = self.object_entry("museum-a", OBJECT_9)
        self.assertEqual(obj["content"]["content"], "合成夜场导览")
        self.assertEqual(self.decisions()["https://museum-a.invalid/a/13"], ("deferred", "reference_only_object"))

    def test_full_update_revises_content(self):
        self.observe(6, 8)
        obj = self.object_entry("museum-a", OBJECT_9)
        self.assertEqual(obj["content"]["content"], "合成夜场导览（修订）")

    def test_update_before_create_establishes_object(self):
        self.observe(8)
        obj = self.object_entry("museum-a", OBJECT_9)
        self.assertEqual(obj["content"]["content"], "合成夜场导览（修订）")
        self.observe(6)
        self.assertEqual(self.decisions()["https://museum-a.invalid/a/12"], ("duplicate", "object_already_known"))
        obj = self.object_entry("museum-a", OBJECT_9)
        self.assertEqual(obj["content"]["content"], "合成夜场导览（修订）")

    def test_undo_before_announce_is_stillborn(self):
        self.observe(1, 9, 10)
        forward = self.forward_entry("museum-b", "https://museum-b.invalid/a/16")
        self.assertEqual(forward["status"], "withdrawn")
        self.assertFalse(forward["displayable"])
        self.assertEqual(self.decisions()["https://museum-b.invalid/a/16"], ("applied", "withdrawn_before_arrival"))

    def test_announce_then_undo_withdraws(self):
        self.observe(1, 10)
        forward = self.forward_entry("museum-b", "https://museum-b.invalid/a/16")
        self.assertEqual(forward["status"], "active")
        self.assertTrue(forward["displayable"])
        self.observe(9)
        forward = self.forward_entry("museum-b", "https://museum-b.invalid/a/16")
        self.assertEqual(forward["status"], "withdrawn")
        self.assertFalse(forward["displayable"])

    def test_duplicate_announce_does_not_restore_withdrawal(self):
        self.observe(1, 2, 3, 11)
        forward = self.forward_entry("museum-b", ANNOUNCE_8)
        self.assertEqual(forward["status"], "withdrawn")
        self.assertEqual(self.decisions()["https://museum-b.invalid/a/8"], ("duplicate", "activity_already_recorded"))
        self.capture(self.origin, "/observations/2")
        self.ingest()
        forward = self.forward_entry("museum-b", ANNOUNCE_8)
        self.assertEqual(forward["status"], "withdrawn")

    def test_owner_delete_then_late_reference(self):
        self.observe(1, 12, 13)
        obj = self.object_entry("museum-a", OBJECT_7)
        self.assertEqual(obj["status"], "deleted")
        self.assertFalse(obj["displayable"])
        forward = self.forward_entry("museum-b", "https://museum-b.invalid/a/18")
        self.assertEqual(forward["status"], "active")
        self.assertEqual(forward["object_status"], "deleted")
        self.assertFalse(forward["displayable"])

    def test_rejected_delete_is_not_sticky_but_reobservation_applies(self):
        self.observe(12)
        self.assertEqual(self.decisions()["https://museum-a.invalid/a/17"], ("rejected", "object_unknown"))
        self.observe(1)
        self.assertEqual(self.object_entry("museum-a", OBJECT_7)["status"], "current")
        self.observe(12)
        self.assertEqual(self.object_entry("museum-a", OBJECT_7)["status"], "deleted")

    def test_gateway_relayed_delete_is_rejected(self):
        self.observe(6, 16)
        self.assertEqual(self.object_entry("museum-a", OBJECT_9)["status"], "current")
        self.assertEqual(self.decisions()["https://museum-b.invalid/a/23"], ("rejected", "actor_mismatch"))


class FullFeedTests(DomainCase):
    def setUp(self):
        super().setUp()
        self.origin_context = serve(load_scenario(fixture_path("domain_origin.json")))
        self.origin = self.origin_context.__enter__()
        self.capture(self.origin, "/feed")
        self.summary = self.ingest()

    def tearDown(self):
        self.origin_context.__exit__(None, None, None)
        super().tearDown()

    def test_view_reflects_final_state(self):
        view = self.domain.view()
        self.assertEqual(view["applied_captures"], 1)
        self.assertEqual(view["activity_events"], 16)
        museum_a = self.partner("museum-a", view)
        self.assertEqual({item["object_id"] for item in museum_a["objects"]}, {OBJECT_7, OBJECT_9})
        self.assertEqual(museum_a["forwards"], [])
        objects = {item["object_id"]: item for item in museum_a["objects"]}
        self.assertEqual(objects[OBJECT_7]["status"], "deleted")
        self.assertEqual(objects[OBJECT_9]["status"], "current")
        self.assertEqual(objects[OBJECT_9]["content"]["content"], "合成夜场导览（修订）")
        museum_b = self.partner("museum-b", view)
        self.assertEqual(museum_b["objects"], [])
        forwards = {item["announce_id"]: item for item in museum_b["forwards"]}
        self.assertEqual(
            set(forwards),
            {
                ANNOUNCE_8,
                "https://museum-b.invalid/a/11",
                "https://museum-b.invalid/a/16",
                "https://museum-b.invalid/a/18",
            },
        )
        self.assertEqual(forwards[ANNOUNCE_8]["status"], "withdrawn")
        self.assertTrue(forwards["https://museum-b.invalid/a/11"]["displayable"])
        self.assertEqual(forwards["https://museum-b.invalid/a/16"]["status"], "withdrawn")
        self.assertFalse(forwards["https://museum-b.invalid/a/18"]["displayable"])

    def test_private_and_unconfigured_activities_not_collected(self):
        decisions = self.decisions()
        self.assertEqual(decisions["https://museum-a.invalid/a/19"], ("rejected", "non_public"))
        self.assertEqual(decisions["https://museum-c.invalid/a/21"], ("rejected", "untrusted_source"))
        for entry in self.domain.view()["partners"]:
            self.assertNotIn("https://museum-a.invalid/objects/20", {item["object_id"] for item in entry["objects"]})
            self.assertNotIn("https://museum-c.invalid/objects/22", {item["object_id"] for item in entry["objects"]})

    def test_evidence_links_events_to_view(self):
        obj = self.object_entry("museum-a", OBJECT_7)
        self.assertEqual(obj["evidence"], [1, 4, 12])
        forward = self.forward_entry("museum-b", ANNOUNCE_8)
        self.assertEqual(forward["evidence"], [2, 3, 11])
        museum_b_events = self.domain.events("museum-b")
        self.assertTrue(all(event["source"] == "museum-b" for event in museum_b_events))
        self.assertEqual(len(self.domain.events()), 16)

    def test_reingest_and_recapture_are_idempotent(self):
        self.assertEqual(self.ingest()["applied_captures"], 0)
        self.capture(self.origin, "/feed")
        summary = self.ingest()
        self.assertEqual(summary["applied_captures"], 1)
        self.assertNotIn("applied", summary["decisions"])
        view = self.domain.view()
        museum_a = self.partner("museum-a", view)
        self.assertEqual(len(museum_a["objects"]), 2)
        museum_b = self.partner("museum-b", view)
        self.assertEqual(len(museum_b["forwards"]), 4)

    def test_ingest_performs_no_network(self):
        body = self.archive.body(1)
        self.assertIn(b"remote-context.invalid", body)
        self.origin.requests.clear()
        second = DomainStore(Path(self.temporary.name) / "fresh.sqlite3", load_partners())
        try:
            second.ingest(self.archive)
        finally:
            second.close()
        self.assertEqual(self.origin.requests, [])


class RobustnessTests(DomainCase):
    def test_outage_preserves_state(self):
        with serve(load_scenario()) as origin:
            self.capture(origin, "/observations/1")
            self.ingest()
            self.assertEqual(self.object_entry("museum-a", OBJECT_7)["status"], "current")
            self.capture(origin, "/unstable")
            summary = self.ingest()
            self.assertEqual(summary["records"], 0)
            self.assertEqual(self.domain.ingest_log()[-1]["outcome"], "skipped_status")
            self.assertEqual(self.object_entry("museum-a", OBJECT_7)["status"], "current")
            self.capture(origin, "/unstable")
            self.ingest()
            self.assertEqual(self.domain.ingest_log()[-1]["outcome"], "applied")
            self.assertEqual(self.object_entry("museum-a", OBJECT_7)["status"], "current")

    def test_decode_error_is_logged_and_watermark_advances(self):
        with serve(load_scenario()) as origin:
            self.capture(origin, "/range-ignored")
            self.ingest()
            self.assertEqual(self.domain.ingest_log()[-1]["outcome"], "decode_error")
            self.capture(origin, "/observations/1")
            self.ingest()
            self.assertEqual(self.object_entry("museum-a", OBJECT_7)["status"], "current")

    def test_undo_by_non_announcer_is_rejected(self):
        self.feed([
            {"source": "museum-b", "observation": 1, "activity": {
                "id": ANNOUNCE_8, "type": "Announce", "actor": ACTOR_B,
                "object": OBJECT_7, "to": [PUBLIC]}},
            {"source": "museum-a", "observation": 2, "activity": {
                "id": "https://museum-a.invalid/a/99", "type": "Undo", "actor": ACTOR_A,
                "object": ANNOUNCE_8}},
        ])
        self.ingest()
        self.assertEqual(self.decisions()["https://museum-a.invalid/a/99"], ("rejected", "not_announcer"))
        self.assertEqual(self.forward_entry("museum-b", ANNOUNCE_8)["status"], "active")

    def test_public_cc_accepted_and_private_only_rejected(self):
        note = {"id": OBJECT_7, "type": "Note", "attributedTo": ACTOR_A, "content": "公开"}
        self.feed([
            {"source": "museum-a", "observation": 1, "activity": {
                "id": "https://museum-a.invalid/a/1", "type": "Create", "actor": ACTOR_A,
                "object": note, "cc": [PUBLIC]}},
            {"source": "museum-a", "observation": 2, "activity": {
                "id": "https://museum-a.invalid/a/2", "type": "Create", "actor": ACTOR_A,
                "object": {"id": "https://museum-a.invalid/objects/8", "type": "Note",
                           "attributedTo": ACTOR_A, "content": "内部"},
                "to": ["https://museum-a.invalid/collections/staff"]}},
        ])
        self.ingest()
        self.assertEqual(self.object_entry("museum-a", OBJECT_7)["status"], "current")
        self.assertEqual(self.decisions()["https://museum-a.invalid/a/2"], ("rejected", "non_public"))

    def test_tombstone_delete_form(self):
        note = {"id": OBJECT_7, "type": "Note", "attributedTo": ACTOR_A, "content": "公开"}
        self.feed([
            {"source": "museum-a", "observation": 1, "activity": {
                "id": "https://museum-a.invalid/a/1", "type": "Create", "actor": ACTOR_A,
                "object": note, "to": [PUBLIC]}},
            {"source": "museum-a", "observation": 2, "activity": {
                "id": "https://museum-a.invalid/a/2", "type": "Delete", "actor": ACTOR_A,
                "object": {"id": OBJECT_7, "type": "Tombstone"}}},
        ])
        self.ingest()
        self.assertEqual(self.object_entry("museum-a", OBJECT_7)["status"], "deleted")

    def test_view_covers_silent_partners_and_filters(self):
        view = self.domain.view()
        self.assertEqual({entry["source"] for entry in view["partners"]}, {"museum-a", "museum-b"})
        for entry in view["partners"]:
            self.assertEqual(entry["objects"], [])
            self.assertEqual(entry["forwards"], [])
        only_a = self.domain.view("museum-a")
        self.assertEqual([entry["source"] for entry in only_a["partners"]], ["museum-a"])
        with self.assertRaises(ValueError):
            self.domain.view("museum-c")

    def test_drill_runs_end_to_end(self):
        result = drill()
        self.assertEqual(result["operation"], "domain_drill")
        self.assertEqual(result["ingest"]["records"], 16)
        sources = {entry["source"] for entry in result["view"]["partners"]}
        self.assertEqual(sources, {"museum-a", "museum-b"})


class ConfigTests(unittest.TestCase):
    def test_load_partners_requires_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "partners.json"
            path.write_text(json.dumps({"partners": [{"source": "x"}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_partners(path)
            path.write_text(json.dumps({"partners": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_partners(path)

    def test_view_requires_existing_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "partner config"):
                DomainStore(Path(temporary) / "missing.sqlite3")


if __name__ == "__main__":
    unittest.main()
