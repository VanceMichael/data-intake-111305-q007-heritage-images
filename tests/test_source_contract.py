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
            self.assertNotIn("commercial", records[0]["purpose"])
            self.assertIn("commercial", records[1]["purpose"])
            self.assertNotEqual(records[0]["rendition"], records[1]["rendition"])
            response = client.fetch(records[2]["href"])
            self.assertTrue(response.url.endswith("/assets/original"))
            self.assertEqual(response.body, b"synthetic-original")

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
