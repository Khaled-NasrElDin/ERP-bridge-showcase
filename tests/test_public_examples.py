import unittest

from examples.sample_mapping import (
    CANONICAL_MAPPING,
    MappingValidationError,
    build_mapping,
    inspect_record,
    normalize_record,
)
from examples.sample_reconciliation import compare_snapshots


class FieldMappingTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "itemCode": "ERP001", "description": "Coffee Beans",
            "availableQty": 310, "depot": "New York",
        }

    def test_override_changes_only_requested_field(self):
        new_mapping = build_mapping({"warehouse": "depot"})
        self.assertEqual(new_mapping["warehouse"], "depot")
        self.assertEqual(CANONICAL_MAPPING["warehouse"], "site")
        self.assertEqual(new_mapping["sku"], "itemCode")

    def test_successful_normalization(self):
        normalized = normalize_record(self.payload, build_mapping({"warehouse": "depot"}))
        self.assertEqual(normalized["stock_on_hand"], 310)
        self.assertEqual(normalized["warehouse"], "New York")

    def test_missing_field_is_reported(self):
        report = inspect_record({"itemCode": "ERP001"}, build_mapping())
        self.assertFalse(report["valid"])
        self.assertIn("availableQty", report["missing"])

    def test_unknown_field_does_not_become_canonical_automatically(self):
        record = dict(self.payload, unknown_flag=True)
        report = inspect_record(record, build_mapping({"warehouse": "depot"}))
        self.assertIn("unknown_flag", report["unknown"])
        self.assertNotIn("unknown_flag", normalize_record(record, build_mapping({"warehouse": "depot"})))

    def test_missing_field_rejects_normalization(self):
        with self.assertRaises(MappingValidationError):
            normalize_record({"itemCode": "ERP001"}, build_mapping())

    def test_unsupported_override_is_rejected(self):
        with self.assertRaises(MappingValidationError):
            build_mapping({"other": "field"})


class ReconciliationSampleTests(unittest.TestCase):
    def test_quantity_difference_and_missing_row(self):
        source = [
            {"sku": "A", "warehouse": "Paris", "stock_on_hand": 20},
            {"sku": "B", "warehouse": "London", "stock_on_hand": 7},
        ]
        stored = [{"sku": "A", "warehouse": "Paris", "stock_on_hand": 18}]
        report = compare_snapshots(source, stored)
        self.assertEqual(report["missing_in_database"], [("B", "London")])
        self.assertEqual(len(report["quantity_mismatches"]), 1)
        self.assertIsNone(report["extra_in_database"])

    def test_extra_rows_only_with_complete_source_snapshot(self):
        source = []
        stored = [{"sku": "A", "warehouse": "Paris", "stock_on_hand": 20}]
        self.assertIsNone(compare_snapshots(source, stored)["extra_in_database"])
        self.assertEqual(compare_snapshots(source, stored, complete_source_snapshot=True)["extra_in_database"], [("A", "Paris")])

    def test_rejects_duplicate_identity(self):
        rec = {"sku": "A", "warehouse": "Paris", "stock_on_hand": 20}
        with self.assertRaises(ValueError):
            compare_snapshots([rec, rec], [], complete_source_snapshot=True)


if __name__ == "__main__":
    unittest.main()
