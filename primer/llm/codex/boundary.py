"""Admission before model transport construction, not a post-tool filter.

This bridge does not embed codex-core or accept a stock app-server executable.
The only trusted extension points are the host's PolicyAuthority and model-only
transport factory. Neither is supplied by JSON/RPC/provider configuration. An
untrusted runtime's self-reported capability list is not an admission proof.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from primer.model.except_ import ConfigError

PROTOCOL_VERSION = 1
UPSTREAM_REFERENCE = "ccf9be8cc458bd6b8336fe84d68d8893f2691a5e"  # Codex 0.171.0
# No upstream binary currently satisfies the reviewed pre-construction contract.
# Adding a hash here is NOT sufficient: an auth/policy transport needs review too.
APPROVED_APP_SERVER_BUILDS: frozenset[str] = frozenset()


class BoundaryError(ConfigError):
    """An admission or protocol violation; messages never include raw inputs."""


@dataclass(frozen=True)
class PolicySnapshot:
    """Result of a TRUSTED administrator-policy resolver, never client claims.

    Account identifiers are opaque bindings, not email or tokens. A future live
    resolver must load every applicable managed/cloud requirement, authenticate
    its provenance, and reject unresolved requirements. Tests use a named fake.
    """

    account: str
    revision: str
    expires_at: float
    models: frozenset[str]
    required_native_effects: frozenset[str] = frozenset()
    suspended: bool = False

    def __post_init__(self) -> None:
        import math

        if (type(self.account) is not str or type(self.revision) is not str
                or type(self.suspended) is not bool or type(self.models) is not frozenset
                or type(self.required_native_effects) is not frozenset
                or any(type(x) is not str or not x for x in self.models | self.required_native_effects)
                or type(self.expires_at) not in {float, int} or not math.isfinite(self.expires_at)):
            raise BoundaryError("Malformed Codex policy snapshot")

    def admit(self, account: str, model: str, *, now: float | None = None) -> None:
        if (not self.account or not self.revision or self.account != account
                or self.suspended or not self.expires_at > (time.time() if now is None else now)
                or model not in self.models or self.required_native_effects):
            raise BoundaryError("Codex model-only policy admission refused")


class PolicyAuthority(Protocol):
    async def current(self) -> PolicySnapshot:
        """Resolve authoritative current policy; failure must propagate closed."""


class UnavailablePolicyAuthority:
    async def current(self) -> PolicySnapshot:
        raise BoundaryError("No verified Codex administrator-policy resolver installed")


@dataclass(frozen=True)
class BridgeConfig:
    """Closed configuration: no CLI args, paths, endpoints, hooks or MCP map."""

    protocol: int = PROTOCOL_VERSION
    max_frame_bytes: int = 1_048_576
    max_output_bytes: int = 8_388_608
    max_calls: int = 128
    max_active_steps: int = 8
    deadline_seconds: float = 300.0
    cleanup_seconds: float = 5.0

    def __post_init__(self) -> None:
        import math

        if type(self.protocol) is not int or self.protocol != PROTOCOL_VERSION:
            raise BoundaryError("Unsupported Codex bridge protocol")
        for value, ceiling in ((self.max_frame_bytes, 4_194_304),
                               (self.max_output_bytes, 33_554_432),
                               (self.max_calls, 1024), (self.max_active_steps, 128)):
            if type(value) is not int or not 0 < value <= ceiling:
                raise BoundaryError("Invalid Codex bridge resource bound")
        for value in (self.deadline_seconds, self.cleanup_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 3600:
                raise BoundaryError("Invalid Codex bridge deadline")

    @classmethod
    def from_mapping(cls, value: dict) -> BridgeConfig:
        if type(value) is not dict or set(value) - set(cls.__dataclass_fields__):
            raise BoundaryError("Unsupported Codex bridge configuration")
        return cls(**value)


def open_app_server(*args: object, **kwargs: object) -> None:
    """No process fallback, even for a known upstream version or safe flags."""
    raise BoundaryError("Stock Codex app-server construction is not approved")
