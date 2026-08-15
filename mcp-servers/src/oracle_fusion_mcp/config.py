"""Runtime configuration, read from the environment.

Every setting can be given either per-server (`ORACLE_FUSION_SCM_HOST`) or
globally (`ORACLE_FUSION_HOST`), so one pod's credentials can be shared across
all three servers while any individual server overrides what it needs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .specs import SpecDef

#: Requests exceeding this take longer than any Claude tool call should wait.
DEFAULT_TIMEOUT_SECONDS = 60.0


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or contradictory."""


def _lookup(definition: SpecDef, suffix: str) -> str | None:
    """Read `ORACLE_FUSION_<KEY>_<SUFFIX>`, falling back to `ORACLE_FUSION_<SUFFIX>`."""
    specific = os.environ.get(f"{definition.env_prefix}_{suffix}")
    if specific:
        return specific.strip() or None
    shared = os.environ.get(f"ORACLE_FUSION_{suffix}")
    return shared.strip() if shared else None


def _flag(definition: SpecDef, suffix: str, default: bool = False) -> bool:
    raw = _lookup(definition, suffix)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Everything one server instance needs at runtime."""

    definition: SpecDef
    index_path: Path
    host: str | None
    base_path: str
    username: str | None
    password: str | None
    token: str | None
    timeout: float
    verify_tls: bool
    max_response_chars: int

    @property
    def configured(self) -> bool:
        """True when the server has enough config to make live API calls.

        The catalog tools work without credentials, so a server with no host
        still starts and stays useful for exploring the API surface.
        """
        return bool(self.host) and (bool(self.token) or bool(self.username and self.password))

    def missing(self) -> list[str]:
        """Human-readable list of what still needs setting."""
        prefix = self.definition.env_prefix
        gaps: list[str] = []
        if not self.host:
            gaps.append(f"{prefix}_HOST (or ORACLE_FUSION_HOST)")
        if not self.token and not (self.username and self.password):
            gaps.append(
                f"{prefix}_TOKEN (or ORACLE_FUSION_TOKEN), "
                f"or both {prefix}_USERNAME and {prefix}_PASSWORD"
            )
        return gaps

    def base_url(self) -> str:
        """Scheme + host, with no trailing slash."""
        host = (self.host or "").rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = f"https://{host}"
        return host


def index_dir() -> Path:
    """Where compiled indexes live. Overridable for tests and custom layouts."""
    override = os.environ.get("ORACLE_FUSION_INDEX_DIR")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "indexes"


def load(definition: SpecDef) -> Config:
    """Assemble the config for one server from the environment."""
    base_path = _lookup(definition, "BASE_PATH") or definition.default_base_path

    timeout_raw = _lookup(definition, "TIMEOUT")
    try:
        timeout = float(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError as error:
        raise ConfigError(
            f"{definition.env_prefix}_TIMEOUT must be a number, got {timeout_raw!r}"
        ) from error

    max_chars_raw = _lookup(definition, "MAX_RESPONSE_CHARS")
    try:
        max_response_chars = int(max_chars_raw) if max_chars_raw else 40_000
    except ValueError as error:
        raise ConfigError(
            f"{definition.env_prefix}_MAX_RESPONSE_CHARS must be an integer, "
            f"got {max_chars_raw!r}"
        ) from error

    return Config(
        definition=definition,
        index_path=index_dir() / definition.index_filename,
        host=_lookup(definition, "HOST"),
        base_path="/" + base_path.strip("/"),
        username=_lookup(definition, "USERNAME"),
        password=_lookup(definition, "PASSWORD"),
        token=_lookup(definition, "TOKEN"),
        timeout=timeout,
        # Fusion pods use public certificates; disabling verification is opt-in
        # and only sensible against a sandbox with a self-signed cert.
        verify_tls=not _flag(definition, "INSECURE_SKIP_TLS_VERIFY"),
        max_response_chars=max_response_chars,
    )
