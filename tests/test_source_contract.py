import hashlib
import json
import unittest

from heritage_images.client import Client
from heritage_images.decoding import parse_page, records as decode_records
from heritage_images.origin import load_scenario, serve


class SourceContractTests(unittest.TestCase):
    def test_domain_observations_preserve_source_distinctions(self):
        scenario = load_scenario()
        records = scenario["source_records"]
        with serve(scenario) as origin:
            client = Client(origin.base_url)
            create, announce, undo = [record["activity"] for record in records]
            self.assertEqual(announce["object"], create["object"]["id"])
            self.assertEqual(undo["object"], announce["id"])
            self.assertNotEqual(announce["actor"], create["actor"])
            self.assertEqual(undo["type"], "Undo")

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
