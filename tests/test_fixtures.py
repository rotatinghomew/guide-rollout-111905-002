import csv
import unittest
from pathlib import Path


class FixtureTests(unittest.TestCase):
    def test_taxonomy_and_case_records_are_present(self):
        path = Path(__file__).parents[1] / "fixtures" / "classifications.csv"
        with path.open(encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertGreaterEqual(len(rows), 8)
        self.assertTrue(all(row["record_id"] for row in rows))
        self.assertEqual(len({row["record_id"] for row in rows}), len(rows))
        self.assertTrue({row["record_type"] for row in rows} >= {"taxonomy", "case"})


if __name__ == "__main__":
    unittest.main()
