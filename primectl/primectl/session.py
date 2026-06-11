"""Per-invocation session: resolved target + lazily-built registry.

``make_client`` is the single seam tests monkeypatch to inject an
``httpx.MockTransport``-backed client. ``Session`` lazily fetches + caches the
spec and builds the registry on first use.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from primectl.client import ApiClient
from primectl.config import Target
from primectl.discovery import load_spec
from primectl.registry import ResourceRegistry, build_registry


def make_client(target: Target, *, verbose: bool = False) -> ApiClient:
    """Construct the API client for a target. Test seam: monkeypatch this."""
    return ApiClient(target.server, token=target.token, verbose=verbose)


@dataclass
class Session:
    target: Target
    output: str = "table"
    refresh: bool = False
    verbose: bool = False
    _client: ApiClient | None = field(default=None, init=False, repr=False, compare=False)
    _registry: ResourceRegistry | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @property
    def client(self) -> ApiClient:
        if self._client is None:
            self._client = make_client(self.target, verbose=self.verbose)
        return self._client

    @property
    def registry(self) -> ResourceRegistry:
        if self._registry is None:
            spec = load_spec(
                self.client,
                context_name=self.target.context_name or "default",
                refresh=self.refresh,
            )
            self._registry = build_registry(spec)
        return self._registry
