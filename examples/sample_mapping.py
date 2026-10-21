"""Public educational sample: explicit ERP field mapping.

Independent, simplified code that demonstrates mapping mechanics. It does not
import or reproduce the private ERP Bridge source code.
"""
from typing import Any, Mapping

CANONICAL_MAPPING = {
    "sku": "itemCode",
    "product_name": "description",
    "stock_on_hand": "availableQty",
    "warehouse": "site",
}


class MappingValidationError(ValueError):
    """A source record cannot satisfy the explicitly configured mapping."""


def build_mapping(
    overrides: Mapping[str, str] | None = None,
    *,
    base: Mapping[str, str] = CANONICAL_MAPPING,
) -> dict[str, str]:
    """Create an isolated mapping and apply only known internal-field overrides."""
    effective = dict(base)
    for internal_field, external_field in (overrides or {}).items():
        if internal_field not in effective:
            raise MappingValidationError(f"Unrecognized canonical field: {internal_field}")
        if not isinstance(external_field, str) or not external_field.strip():
            raise MappingValidationError(f"Invalid source name for: {internal_field}")
        effective[internal_field] = external_field
    return effective


def inspect_record(record: Mapping[str, Any], mapping: Mapping[str, str]) -> dict:
    """Identify missing required fields and any extra source fields."""
    expected = set(mapping.values())
    received = set(record)
    missing = sorted(expected - received)
    unknown = sorted(received - expected)
    return {"valid": not missing, "missing": missing, "unknown": unknown}


def normalize_record(record: Mapping[str, Any], mapping: Mapping[str, str]) -> dict:
    """Translate configured source keys into canonical field names."""
    report = inspect_record(record, mapping)
    if not report["valid"]:
        raise MappingValidationError(f"Missing source fields: {report['missing']}")
    return {internal: record[external] for internal, external in mapping.items()}
