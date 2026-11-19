import csv
from pathlib import Path
import unittest


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
HEADERS = {'cards': ['id', 'topic', 'policy', 'destination', 'active_from', 'active_to'], 'applications': ['profile', 'topic', 'window', 'card', 'rank', 'requested'], 'observation_orders': ['id', 'profile', 'window', 'card', 'included_from', 'cap', 'used', 'status'], 'issued': ['id', 'order_id', 'card', 'visitor', 'issued_at'], 'card_changes': ['id', 'card', 'kind', 'effective', 'recorded', 'related_card'], 'policy': ['min_visitors', 'confidence', 'min_correct', 'max_redirect', 'followup_days'], 'requests': ['id', 'visitor', 'window', 'topic', 'policy', 'day', 'cutoff'], 'offers': ['request_id', 'card', 'probability', 'display_position'], 'responses': ['request_id', 'card', 'outcome', 'confirmed'], 'visitor_assignments': ['id', 'visitor', 'order_id', 'arm', 'assigned', 'expires'], 'request_queue': ['id', 'visitor', 'window', 'topic', 'policy', 'day', 'orders'], 'callbacks': ['order_id', 'office', 'start', 'end', 'reserved', 'committed'], 'callback_capacity': ['office', 'start', 'end', 'maximum']}
LINKS = [('offers', 'request_id', 'requests', 'id'), ('responses', 'request_id', 'requests', 'id'), ('issued', 'order_id', 'observation_orders', 'id')]


def read(name):
    with (FIXTURES / (name + ".csv")).open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        return reader.fieldnames, list(reader)


class SourceRecords(unittest.TestCase):
    def test_records_match_source_columns(self):
        for name, expected in HEADERS.items():
            with self.subTest(table=name):
                header, rows = read(name)
                self.assertEqual(header, expected)
                self.assertTrue(rows)
                self.assertTrue(all(None not in row and None not in row.values() for row in rows))
                if "id" in header and (name != "receipts" or "batch" not in header):
                    ids = [row["id"] for row in rows]
                    self.assertTrue(all(ids))
                    self.assertEqual(len(ids), len(set(ids)))

    def test_record_references_exist(self):
        for source, field, target, key in LINKS:
            with self.subTest(table=source, column=field):
                _, source_rows = read(source)
                _, target_rows = read(target)
                keys = {row[key] for row in target_rows}
                self.assertTrue(all(row[field] in keys for row in source_rows))
