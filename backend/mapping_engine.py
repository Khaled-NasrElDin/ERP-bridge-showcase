
# Mapping configuration for each customer


import json
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("mappings.json")


def load_mappings():

    with CONFIG_PATH.open("r", encoding="utf-8") as file:

        mapping = json.load(file)
        return mapping

CUSTOMER_MAPPINGS = load_mappings()

def normalize_inventory(record, customer, mapping=None):

    if mapping is None:
        mapping = CUSTOMER_MAPPINGS[customer]

    normalized = {}

    for internal_field, external_field in mapping.items():
        normalized[internal_field] = record[external_field]

    return normalized

def inspect_schema(record, customer, mapping=None):

    if mapping is None:
        mapping = CUSTOMER_MAPPINGS[customer]

    expected_fields = set(mapping.values())
    received_fields = set(record.keys())

    missing_fields = expected_fields - received_fields
    unknown_fields = received_fields - expected_fields

    return {
        "customer": customer,
        "missing_fields": sorted(missing_fields),
        "unknown_fields": sorted(unknown_fields),
        "valid": len(missing_fields) == 0
    }

def inspect_batch(records, customer, mapping=None):

    for position, record in enumerate(records, start=1):

        inspection = inspect_schema(
            record,
            customer,
            mapping=mapping
        )

        if not inspection["valid"]:
            return {
                "record_number": position,
                **inspection
            }

    return None

def resolve_mapping(mapping_id, overrides=None):

    # Retrieve the shared mapping template
    mapping = CUSTOMER_MAPPINGS[mapping_id].copy()

    # Apply customer-specific differences
    if overrides:
        for internal_field, external_field in overrides.items():

            if internal_field not in mapping:
                raise ValueError(
                    f"Unknown internal field: {internal_field}"
                )

            mapping[internal_field] = external_field

    return mapping
