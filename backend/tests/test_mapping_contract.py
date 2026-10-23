"""Focused contract checks against the REAL mapping engine, using fictional data."""
import pytest
from mapping_engine import inspect_schema, normalize_inventory, resolve_mapping


def test_canonical_mapping_customer_a():
    data = {"product_id": "SKU-1", "name": "Sample", "quantity": 7, "warehouse": "Boston"}
    result = normalize_inventory(data, "customer_a")
    assert result == {"sku": "SKU-1", "product_name": "Sample", "stock_on_hand": 7, "warehouse": "Boston"}


def test_overrides_are_tenant_specific():
    mapping = resolve_mapping("erp_itemcode_v1", {"warehouse": "depot"})
    source = {"itemCode": "X-2", "description": "Tea", "availableQty": 8, "depot": "New York"}
    result = normalize_inventory(source, "erp_itemcode_v1", mapping=mapping)
    assert result["sku"] == "X-2"
    assert result["warehouse"] == "New York"


def test_schema_inspection_reports_missing_fields():
    report = inspect_schema({"product_id": "X"}, "customer_a")
    assert report["valid"] is False
    assert "name" in report["missing_fields"]
