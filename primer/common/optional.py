"""Single source of truth for optional-extra capability checks.

Every missing-extra guard, the bootstrap skips, the /v1/capabilities
endpoint, and the console hint text derive from the EXTRA_MODULES map
here, so the extra -> marker-module wiring lives in exactly one place.
"""

from __future__ import annotations

from importlib.util import find_spec

from primer.model.except_ import ConfigError

# Monkeypatch seam for tests; production code always goes through this.
_find_spec = find_spec

# Extra name (as published in pyproject [project.optional-dependencies])
# -> import-marker module(s). "channels" is installed when ALL three
# platform SDKs import; per-platform detail comes from channel_platforms().
EXTRA_MODULES: dict[str, tuple[str, ...]] = {
    "huggingface": ("sentence_transformers",),
    "docling": ("docling",),
    "lance": ("lancedb",),
    "kubernetes": ("kubernetes_asyncio",),
    "docker": ("aiodocker",),
    "channels": ("slack_bolt", "telegram", "discord"),
}

CHANNEL_PLATFORM_MODULES: dict[str, str] = {
    "slack": "slack_bolt",
    "telegram": "telegram",
    "discord": "discord",
}


def has_extra(extra: str) -> bool:
    """True when every marker module of ``extra`` is importable."""
    return all(_find_spec(mod) is not None for mod in EXTRA_MODULES[extra])


def channel_platforms() -> dict[str, bool]:
    """Per-platform installed map for the 'channels' extra."""
    return {
        platform: _find_spec(mod) is not None
        for platform, mod in CHANNEL_PLATFORM_MODULES.items()
    }


def install_hint(extra: str) -> str:
    """The standard operator-facing enable instruction for ``extra``."""
    return (
        f"install it with: pip install 'primer-ai[{extra}]' "
        "(or 'primer-ai[full]' for everything), then restart the server"
    )


def require_extra(extra: str, capability: str) -> None:
    """Raise the standard ConfigError when ``extra`` is not installed."""
    if not has_extra(extra):
        raise ConfigError(
            f"{capability} needs the optional '{extra}' extra; "
            f"{install_hint(extra)}"
        )


__all__ = [
    "CHANNEL_PLATFORM_MODULES",
    "EXTRA_MODULES",
    "channel_platforms",
    "has_extra",
    "install_hint",
    "require_extra",
]
