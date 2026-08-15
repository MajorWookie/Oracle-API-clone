"""HTTP client for a Fusion pod.

Handles auth, URL assembly, and turning Oracle's error responses into messages a
model can act on. Oracle returns RFC 7807 problem documents for most failures,
which carry far more useful detail than the status line alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config

#: Oracle requires this header on PATCH against Fusion REST resources.
MERGE_PATCH = "application/vnd.oracle.adf.resourceitem+json"

#: Sent on every request. The framework version selects Oracle's response shape;
#: 3 is the current default for the 11.13.18.05 resource version.
DEFAULT_HEADERS = {
    "Accept": "application/json",
    "REST-Framework-Version": "3",
}


class ApiError(RuntimeError):
    """A request that reached Oracle and came back unsuccessful."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class ApiResponse:
    status: int
    headers: dict[str, str]
    body: Any
    truncated: bool = False


def _describe_oracle_error(status: int, payload: Any) -> str:
    """Extract the useful sentence from an Oracle error body."""
    if isinstance(payload, dict):
        # RFC 7807 problem document, which is what Fusion returns for most errors.
        parts = [payload.get("title"), payload.get("detail")]
        message = " — ".join(p for p in parts if p)
        # Nested per-attribute failures live under o:errorDetails.
        details = payload.get("o:errorDetails")
        if isinstance(details, list) and details:
            extra = "; ".join(
                str(d.get("detail") or d.get("title"))
                for d in details[:5]
                if isinstance(d, dict)
            )
            if extra:
                message = f"{message} Details: {extra}" if message else extra
        if message:
            return message
    if isinstance(payload, str) and payload.strip():
        return payload.strip()[:500]
    return f"HTTP {status}"


class FusionClient:
    """Thin async wrapper over httpx for one configured pod."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    def _auth(self) -> tuple[httpx.Auth | None, dict[str, str]]:
        """Return the httpx auth object and any extra headers.

        A bearer token wins over basic credentials when both are supplied — an
        explicitly issued OAuth token is the more deliberate choice.
        """
        config = self._config
        if config.token:
            return None, {"Authorization": f"Bearer {config.token}"}
        if config.username and config.password:
            return httpx.BasicAuth(config.username, config.password), {}
        return None, {}

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            auth, headers = self._auth()
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url(),
                auth=auth,
                headers={**DEFAULT_HEADERS, **headers},
                timeout=self._config.timeout,
                verify=self._config.verify_tls,
                follow_redirects=True,
            )
        return self._client

    def build_url(self, path: str) -> str:
        """Map an indexed path onto the configured base path.

        The index compiles every path with the spec's *default* base path baked
        in. When `..._BASE_PATH` overrides it — which is how a caller follows
        Oracle bumping the `11.13.18.05` resource version — that prefix is
        swapped here. Paths carrying a different API root of their own (the
        Common Features spec's `/ess`, `/api` and `/bpm` endpoints) are left
        alone, since the override applies only to the spec's own base.
        """
        from .paths import has_api_root

        path = "/" + path.lstrip("/")
        default = self._config.definition.default_base_path.rstrip("/")
        configured = self._config.base_path.rstrip("/")

        if configured != default and default and path.startswith(f"{default}/"):
            return f"{configured}{path[len(default):]}"
        if configured and not has_api_root(path):
            return f"{configured}{path}"
        return path

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> ApiResponse:
        """Issue one request and return the parsed response.

        Raises `ApiError` for 4xx/5xx so tool handlers can surface a clean message.
        """
        if not self._config.configured:
            raise ApiError(
                "This server is not configured to reach a Fusion pod. Missing: "
                + ", ".join(self._config.missing())
            )

        client = await self._ensure_client()
        url = self.build_url(path)

        request_headers = dict(headers or {})
        if body is not None and method.upper() == "PATCH":
            request_headers.setdefault("Content-Type", MERGE_PATCH)
        elif body is not None:
            request_headers.setdefault("Content-Type", "application/json")

        # Drop unset query params rather than sending `?foo=None`.
        params = {k: v for k, v in (query or {}).items() if v is not None}

        try:
            response = await client.request(
                method.upper(),
                url,
                params=params or None,
                json=body if body is not None else None,
                headers=request_headers,
            )
        except httpx.TimeoutException as error:
            raise ApiError(
                f"Request timed out after {self._config.timeout}s: {method.upper()} {url}. "
                f"Narrow the query (use `limit` and `q`) or raise "
                f"{self._config.definition.env_prefix}_TIMEOUT."
            ) from error
        except httpx.RequestError as error:
            raise ApiError(
                f"Could not reach {self._config.base_url()}: {error}. "
                f"Check {self._config.definition.env_prefix}_HOST and network access."
            ) from error

        payload = self._parse(response)

        if response.status_code >= 400:
            detail = _describe_oracle_error(response.status_code, payload)
            hint = self._hint_for(response.status_code)
            raise ApiError(
                f"HTTP {response.status_code} from {method.upper()} {url}: {detail}{hint}",
                status=response.status_code,
                body=payload,
            )

        return ApiResponse(
            status=response.status_code,
            headers=dict(response.headers),
            body=payload,
        )

    def _hint_for(self, status: int) -> str:
        """Add a recovery hint for the statuses that have an obvious next step."""
        prefix = self._config.definition.env_prefix
        return {
            401: f" Check {prefix}_USERNAME/{prefix}_PASSWORD or {prefix}_TOKEN.",
            403: " The authenticated user lacks the required Fusion role or data-security privilege.",
            404: " Verify the resource path and any record ids — use search_operations to confirm the endpoint.",
            405: " That method is not allowed on this resource; use describe_operation to see supported methods.",
            412: " Precondition failed — the record changed since it was read; re-read it and retry.",
        }.get(status, "")

    def _parse(self, response: httpx.Response) -> Any:
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            try:
                return response.json()
            except json.JSONDecodeError:
                return response.text
        if not response.content:
            return None
        if content_type.startswith(("text/", "application/xml")):
            return response.text
        return (
            f"<{len(response.content)} bytes of {content_type or 'binary data'} "
            f"omitted — not a text or JSON response>"
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
