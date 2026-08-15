"""Shared fixtures: a miniature spec compiled through the real build pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from oracle_fusion_mcp.build_index import build
from oracle_fusion_mcp.config import Config
from oracle_fusion_mcp.index import SpecIndex
from oracle_fusion_mcp.specs import SpecDef

BASE_PATH = "/fscmRestApi/resources/11.13.18.05"

MINI_SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Test Fusion API", "version": "1.0"},
    "paths": {
        "/purchaseOrders": {
            # A path-item-level parameter, shared by both operations below.
            "parameters": [{"name": "REST-Framework-Version", "in": "header", "schema": {"type": "string"}}],
            "get": {
                "operationId": "getall_purchaseOrders",
                "summary": "Get all purchase orders",
                "description": "Retrieve a list of purchase orders held in procurement.",
                "tags": ["Procurement/Purchase Orders"],
                "parameters": [
                    {"name": "q", "in": "query", "schema": {"type": "string"}, "description": "Filter expression."},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
            },
            "post": {
                "operationId": "create_purchaseOrder",
                "summary": "Create a purchase order",
                "description": "Create one purchase order.",
                "tags": ["Procurement/Purchase Orders"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/PurchaseOrder"}}
                    },
                },
            },
        },
        "/purchaseOrders/{OrderId}": {
            "get": {
                "operationId": "get_purchaseOrder",
                "summary": "Get one purchase order",
                "description": "Retrieve a single purchase order by id.",
                "tags": ["Procurement/Purchase Orders"],
                "parameters": [
                    {"name": "OrderId", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/PurchaseOrder"}}
                        }
                    }
                },
            },
            "patch": {
                "operationId": "update_purchaseOrder",
                "summary": "Update a purchase order",
                "tags": ["Procurement/Purchase Orders"],
                "parameters": [
                    {"name": "OrderId", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
            },
            "delete": {
                "operationId": "delete_purchaseOrder",
                "summary": "Delete a purchase order",
                "tags": ["Procurement/Purchase Orders"],
                "parameters": [
                    {"name": "OrderId", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
            },
        },
        "/inventoryItems": {
            "get": {
                "operationId": "getall_inventoryItems",
                "summary": "Get all inventory items",
                "description": "Retrieve inventory item records.",
                "tags": ["Inventory Management/Items"],
            }
        },
        # No operationId anywhere — exercises the derived-id fallback.
        "/legacyThing": {"get": {"summary": "Legacy read", "tags": ["Legacy"]}},
    },
    "components": {
        "schemas": {
            "PurchaseOrder": {
                "type": "object",
                "required": ["OrderNumber"],
                "properties": {
                    "OrderNumber": {"type": "string", "description": "The order number."},
                    "Supplier": {"type": "string"},
                    "Lines": {"$ref": "#/components/schemas/OrderLine"},
                },
            },
            "OrderLine": {
                "type": "object",
                "properties": {
                    "LineNumber": {"type": "integer"},
                    "Parent": {"$ref": "#/components/schemas/PurchaseOrder"},
                },
            },
        }
    },
}


@pytest.fixture(scope="session")
def mini_definition() -> SpecDef:
    return SpecDef(
        key="test",
        server_name="oracle-fusion-test",
        spec_filename="test-spec.json",
        default_base_path=BASE_PATH,
        normalize_paths=False,
        blurb="Test spec.",
    )


@pytest.fixture(scope="session")
def mini_index_path(tmp_path_factory: pytest.TempPathFactory, mini_definition: SpecDef) -> Path:
    """Compile MINI_SPEC with the production build pipeline."""
    root = tmp_path_factory.mktemp("spec")
    (root / "test-spec.json").write_text(json.dumps(MINI_SPEC), encoding="utf-8")
    return build(mini_definition, root, root / "indexes")


@pytest.fixture
def mini_index(mini_index_path: Path) -> SpecIndex:
    return SpecIndex(mini_index_path)


@pytest.fixture
def mini_config(mini_definition: SpecDef, mini_index_path: Path) -> Config:
    """A fully configured server pointed at a pod that tests never really call."""
    return Config(
        definition=mini_definition,
        index_path=mini_index_path,
        host="fusion.example.com",
        base_path=BASE_PATH,
        username="tester",
        password="secret",
        token=None,
        timeout=10.0,
        verify_tls=True,
        max_response_chars=2000,
    )


@pytest.fixture
def unconfigured_config(mini_config: Config) -> Config:
    """Same index, but no credentials — catalog tools must still work."""
    return Config(
        definition=mini_config.definition,
        index_path=mini_config.index_path,
        host=None,
        base_path=BASE_PATH,
        username=None,
        password=None,
        token=None,
        timeout=10.0,
        verify_tls=True,
        max_response_chars=2000,
    )
