"""Path normalization — the messiest part of the Common Features spec."""

from __future__ import annotations

import pytest

from oracle_fusion_mcp.paths import (
    fill_path_params,
    has_api_root,
    normalize_path,
    path_placeholders,
    strip_host,
)

BASE = "/fscmRestApi/resources/11.13.18.05"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/hcmRestApi/scim/Users", "/hcmRestApi/scim/Users"),
        ("http://<servername>/fscmRestApi/resources/11.13.18.05/currenciesLOV", f"{BASE}/currenciesLOV"),
        ("https://<servername>/api/boss/data/objects/v1/x", "/api/boss/data/objects/v1/x"),
        ("http://servername/fscmRestApi/resources/11.13.18.05/x", f"{BASE}/x"),
        ("servername/fscmRestApi/resources/11.13.18.05/genericLookups", f"{BASE}/genericLookups"),
        ("ess/rest/scheduler/v1", "/ess/rest/scheduler/v1"),
    ],
)
def test_absolute_and_placeholder_forms_reduce_to_a_clean_path(raw: str, expected: str) -> None:
    assert normalize_path(raw, BASE, enabled=True) == expected


def test_bare_resources_receive_the_default_base_path() -> None:
    assert normalize_path("/announcements", BASE, enabled=True) == f"{BASE}/announcements"
    assert normalize_path("/atkThemes", BASE, enabled=True) == f"{BASE}/atkThemes"


def test_unfamiliar_api_roots_are_not_double_prefixed() -> None:
    """`fndSetupApi` is not in the known-roots list but must still be recognized."""
    result = normalize_path("<servername>/fndSetupApi/resources/11.13.18.05/timezonesLOV", BASE)
    assert result == "/fndSetupApi/resources/11.13.18.05/timezonesLOV"
    assert result.count("/resources/") == 1


def test_disabled_normalization_only_prefixes() -> None:
    """SCM and CX paths are already clean; they just need the base path."""
    assert normalize_path("/assetSystemOptions", BASE, enabled=False) == f"{BASE}/assetSystemOptions"


def test_strip_host_collapses_duplicate_slashes() -> None:
    assert strip_host("https://<servername>//api//v1") == "/api/v1"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/fscmRestApi/x", True),
        ("/fndSetupApi/x", True),
        ("/fscmUI/x", True),
        ("/ess/rest", True),
        ("/announcements", False),
        ("/persons", False),
    ],
)
def test_api_root_detection(path: str, expected: bool) -> None:
    assert has_api_root(path) is expected


def test_fill_path_params_substitutes_and_reports_gaps() -> None:
    path = "/x/{ItemId}/child/{LineId}"
    filled, missing = fill_path_params(path, {"ItemId": "100"})
    assert missing == {"LineId"}
    assert filled == "/x/100/child/{LineId}"

    filled, missing = fill_path_params(path, {"ItemId": "100", "LineId": 7})
    assert not missing
    assert filled == "/x/100/child/7"


def test_fill_path_params_encodes_reserved_characters() -> None:
    """A key containing a slash must not create a new path segment."""
    filled, _ = fill_path_params("/items/{Id}", {"Id": "AS/54888 X"})
    assert filled == "/items/AS%2F54888%20X"


def test_path_placeholders_reports_names_in_order() -> None:
    assert path_placeholders("/a/{One}/b/{Two}") == ["One", "Two"]
