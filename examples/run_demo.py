"""Run: python -m examples.run_demo"""
from pprint import pprint
from .sample_mapping import build_mapping, inspect_record, normalize_record
from .sample_reconciliation import compare_snapshots


def main():
    source = {
        "itemCode": "ERP001", "description": "Coffee Beans",
        "availableQty": 310, "depot": "New York",
        "metadata_comment": "Fictional example only",
    }
    mapping = build_mapping({"warehouse": "depot"})
    print("Schema inspection:")
    pprint(inspect_record(source, mapping))

    canonical = normalize_record(source, mapping)
    print("\nCanonical inventory:")
    pprint(canonical)

    stored = [dict(canonical, stock_on_hand=300)]
    print("\nRead-only comparison with stored snapshot:")
    pprint(compare_snapshots([canonical], stored, complete_source_snapshot=True))


if __name__ == "__main__":
    main()
