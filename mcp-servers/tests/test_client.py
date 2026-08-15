"""URL assembly and the base-path override."""

from __future__ import annotations

from dataclasses import replace

from oracle_fusion_mcp.client import FusionClient
from oracle_fusion_mcp.config import Config

DEFAULT = "/fscmRestApi/resources/11.13.18.05"


def test_indexed_paths_pass_through_unchanged(mini_config: Config) -> None:
    client = FusionClient(mini_config)
    assert client.build_url(f"{DEFAULT}/workOrders") == f"{DEFAULT}/workOrders"


def test_base_path_override_replaces_the_compiled_prefix(mini_config: Config) -> None:
    """Following an Oracle resource-version bump must actually change the URL.

    Index paths are compiled with the default base path, so the override has to
    be applied at request time or it would silently do nothing.
    """
    bumped = "/fscmRestApi/resources/11.13.20.01"
    client = FusionClient(replace(mini_config, base_path=bumped))
    assert client.build_url(f"{DEFAULT}/workOrders") == f"{bumped}/workOrders"


def test_override_leaves_other_api_roots_alone(mini_config: Config) -> None:
    """Common Features serves /ess, /api and /bpm outside the spec's own base."""
    client = FusionClient(replace(mini_config, base_path="/fscmRestApi/resources/11.13.20.01"))
    for path in ("/ess/rest/scheduler/v1/requests", "/api/boss/data/objects/v1/x", "/bpm/api/v1/tasks"):
        assert client.build_url(path) == path


def test_unrooted_paths_receive_the_configured_base(mini_config: Config) -> None:
    client = FusionClient(mini_config)
    assert client.build_url("/announcements") == f"{DEFAULT}/announcements"


def test_host_gains_a_scheme_when_none_is_given(mini_config: Config) -> None:
    assert mini_config.base_url() == "https://fusion.example.com"
    assert replace(mini_config, host="http://pod.local").base_url() == "http://pod.local"
    assert replace(mini_config, host="https://pod.local/").base_url() == "https://pod.local"


def test_bearer_token_takes_precedence_over_basic(mini_config: Config) -> None:
    client = FusionClient(replace(mini_config, token="abc123"))
    auth, headers = client._auth()
    assert auth is None
    assert headers["Authorization"] == "Bearer abc123"


def test_basic_auth_is_used_when_no_token_is_set(mini_config: Config) -> None:
    auth, headers = FusionClient(mini_config)._auth()
    assert auth is not None
    assert "Authorization" not in headers


def test_missing_reports_both_credential_routes(unconfigured_config: Config) -> None:
    gaps = " ".join(unconfigured_config.missing())
    assert "HOST" in gaps
    assert "TOKEN" in gaps and "USERNAME" in gaps
    assert unconfigured_config.configured is False


def test_host_without_credentials_is_still_unconfigured(mini_config: Config) -> None:
    partial = replace(mini_config, username=None, password=None, token=None)
    assert partial.configured is False
