"""Public educational sample: compare canonical ERP and stored inventory.

This is a self-contained snapshot comparison, *not* the private product's
implemented reconciliation engine. Only a COMPLETE source snapshot can prove
that destination-only rows are extra.
"""
from collections.abc import Iterable, Mapping
from typing import Any


def _index(records: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], Mapping[str, Any]]:
    index = {}
    for record in records:
        key = (str(record["sku"]), str(record["warehouse"]))
        if key in index:
            raise ValueError(f"Duplicate inventory identity: {key}")
        index[key] = record
    return index


def compare_snapshots(
    source: Iterable[Mapping[str, Any]],
    stored: Iterable[Mapping[str, Any]],
    *,
    complete_source_snapshot: bool = False,
) -> dict:
    """Identify missing, quantity-mismatched, and (optionally) extra records."""
    incoming = _index(source)
    destination = _index(stored)

    missing_in_db = sorted(set(incoming) - set(destination))
    quantity_mismatch = [
        {"sku": sku, "warehouse": warehouse,
         "source": incoming[(sku, warehouse)]["stock_on_hand"],
         "database": destination[(sku, warehouse)]["stock_on_hand"]}
        for sku, warehouse in sorted(set(incoming) & set(destination))
        if incoming[(sku, warehouse)]["stock_on_hand"]
        != destination[(sku, warehouse)]["stock_on_hand"]
    ]
    return {
        "missing_in_database": missing_in_db,
        "quantity_mismatches": quantity_mismatch,
        "extra_in_database": (
            sorted(set(destination) - set(incoming)) if complete_source_snapshot else None
        ),
    }
