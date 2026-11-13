import json
import unittest
from pathlib import Path


class AnalysisInputsTests(unittest.TestCase):
    def load_data(self):
        return json.loads((Path(__file__).resolve().parents[1] / "fixtures/analysis/input.json").read_text())

    def test_identity_split(self):
        data = self.load_data()
        self.assertTrue(any(row['kind'] == 'split' for row in data['identity_changes']))

    def test_incomplete_funnels(self):
        data = self.load_data()
        self.assertTrue(len(data['events']) < 150)

    def test_dual_cutoffs(self):
        data = self.load_data()
        self.assertTrue(len(data['cohort_policy']['historical_cutoffs']) == 2)

    def test_source_versions_and_cutoff(self):
        data = self.load_data()
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["source_versions"])
        self.assertIn("+00:00", data["as_of"])
