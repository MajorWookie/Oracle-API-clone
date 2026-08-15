"""Registry of the Oracle Fusion OpenAPI specs this package wraps.

None of the specs ship a `servers` block, so the base path for each one is
recorded here instead. The values are derived from the `/<root>RestApi/resources/<version>`
URLs embedded in each spec's own `components` section:

    SCM     fscmRestApi  6144 occurrences  (crmRestApi 3, hcmRestApi 2)
    CX      crmRestApi   4819 occurrences  (fscmRestApi 784, hcmRestApi 1)
    Common  fscmRestApi   204 occurrences

Oracle bumps the `11.13.18.05` version segment between releases, so every base
path is overridable per-server with an environment variable.
"""

from __future__ import annotations

from dataclasses import dataclass

# Oracle's resource version segment, shared by all three specs at time of writing.
RESOURCE_VERSION = "11.13.18.05"

# First path segments that already carry their own API root, so the normalizer
# passes them through instead of prepending `default_base_path`.
#
# Segments matching `*Api` or `*UI` (fscmRestApi, fndSetupApi, fscmUI, ...) are
# recognized by pattern in `paths.has_api_root`; this set covers the roots that
# follow no pattern. Both were derived by enumerating the distinct first segments
# of the Common Features spec — the only spec with absolute paths.
#
# Deliberately excluded, because they are bare Fusion resources that DO need the
# default base path: announcements, atkThemes, atkPopupItems, atkhelpcentertopics,
# persons.
KNOWN_API_ROOTS = frozenset(
    {
        "api",  # /api/boss/data/objects/... — BOSS object REST
        "bpm",  # /bpm/api/... — workflow tasks
        "ess",  # /ess/rest/scheduler/... — scheduled processes
        "oam",  # /oam/services/rest/access/... — sign-in audit
        "orchestrator",  # /orchestrator/agent/... — Fusion AI agents
        "soa-infra",
        "xmlpserver",
        "bi",
    }
)


@dataclass(frozen=True)
class SpecDef:
    """One OpenAPI spec and the server built from it."""

    key: str
    """Short identifier, used for the index filename and env var prefix."""

    server_name: str
    """Name advertised over MCP."""

    spec_filename: str
    """File in the repo root. `.json.gz` is read without decompressing to disk."""

    default_base_path: str
    """Prepended to spec paths that lack an API root of their own."""

    normalize_paths: bool
    """True for specs whose paths contain absolute URLs / `<servername>` placeholders."""

    blurb: str
    """One-line summary used in the server instructions."""

    swagger2: bool = False
    """True for Swagger 2.0 documents, which are upconverted before indexing."""

    mcp_server: bool = True
    """False for specs that are indexed but deliberately not exposed over MCP."""

    @property
    def env_prefix(self) -> str:
        return f"ORACLE_FUSION_{self.key.upper()}"

    @property
    def index_filename(self) -> str:
        return f"{self.key}.db"


SCM = SpecDef(
    key="scm",
    server_name="oracle-fusion-scm",
    spec_filename="REST API for Oracle Fusion Cloud SCM.json.gz",
    default_base_path=f"/fscmRestApi/resources/{RESOURCE_VERSION}",
    normalize_paths=False,
    blurb=(
        "Oracle Fusion Cloud Supply Chain Management — inventory, manufacturing, "
        "order management, procurement, product lifecycle, maintenance and logistics."
    ),
)

CX = SpecDef(
    key="cx",
    server_name="oracle-fusion-cx",
    spec_filename=(
        "REST API for Sales and Fusion Service in Oracle Fusion Cloud "
        "Customer Experience.json.gz"
    ),
    default_base_path=f"/crmRestApi/resources/{RESOURCE_VERSION}",
    normalize_paths=False,
    blurb=(
        "Oracle Fusion Cloud Customer Experience — Sales and Fusion Service: "
        "accounts, contacts, opportunities, leads, service requests, contracts "
        "and subscriptions."
    ),
)

COMMON = SpecDef(
    key="common",
    server_name="oracle-fusion-common",
    spec_filename="REST API for Common Features in Oracle Fusion Cloud Applications.json",
    default_base_path=f"/fscmRestApi/resources/{RESOURCE_VERSION}",
    normalize_paths=True,
    blurb=(
        "Oracle Fusion Cloud Applications common features — cross-pillar services "
        "including SCIM users, scheduled processes (ESS), BPM workflow tasks, "
        "attachments, profile options, announcements and help topics."
    ),
)

CPQ = SpecDef(
    key="cpq",
    server_name="oracle-cpq",
    spec_filename="REST API Services for Oracle CPQ.json",
    # CPQ's path keys already carry their own root (`/rest/v19/salesUsers`), so the
    # base path is recorded for resource derivation but never prepended.
    default_base_path="/rest/v19",
    normalize_paths=False,
    swagger2=True,
    # Indexed for Postman generation only. The MCP servers deliberately do not
    # wrap CPQ; it is a different product with its own authentication model.
    mcp_server=False,
    blurb=(
        "Oracle CPQ — commerce transactions, configuration, pricing setup, "
        "parts and sales company administration."
    ),
)

#: Specs exposed as MCP servers, one per `entry.py` console script.
MCP_SPECS: tuple[SpecDef, ...] = (SCM, CX, COMMON)

#: Every spec in the repo, including the ones only compiled for Postman export.
ALL_SPECS: tuple[SpecDef, ...] = (SCM, CX, COMMON, CPQ)
SPECS_BY_KEY: dict[str, SpecDef] = {s.key: s for s in ALL_SPECS}
