"""A side-effect-clean live probe shared by artifact-storage routes and tools."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from primer.artifact.checks import prepare_artifact_provider
from primer.artifact.factory import build_artifact_storage
from primer.common.log import redact_credentials
from primer.int.artifact_storage import ArtifactStorage
from primer.int.storage_provider import StorageProvider
from primer.model.provider import ArtifactStorageProvider


logger = logging.getLogger(__name__)

PROBE_DATA = b"primer artifact storage probe"
PROBE_MIME = "text/plain"
PROBE_FILENAME = "primer-probe.txt"

ProbeStep = Literal["prepare", "build", "initialize", "put", "get", "delete"]


@dataclass(frozen=True)
class ArtifactProbeResult:
    ok: bool
    step: ProbeStep | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True}
        return {"ok": False, "step": self.step, "error": self.error}


def _plain_secret(value: object) -> str | None:
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    plain = getter() if callable(getter) else value
    return plain if isinstance(plain, str) else None


def probe_failure_text(exc: BaseException, config: object) -> str:
    """Return a useful failure without leaking the provider's configured keys."""
    text = f"{type(exc).__name__}: {exc}"
    for name in ("access_key", "secret_key"):
        secret = _plain_secret(getattr(config, name, None))
        if secret is not None and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    return redact_credentials(text)


async def probe_artifact_provider(
    row: ArtifactStorageProvider,
    *,
    storage_provider: StorageProvider,
    build: Callable[[ArtifactStorageProvider, StorageProvider], ArtifactStorage]
    | None = None,
    prepare: bool = False,
) -> ArtifactProbeResult:
    """Build, initialize and round-trip a temporary object, leaving no bytes.

    With ``prepare`` the row's location is prepared first (step ``prepare``: a filesystem root is created and given its marker), as a create
    would. The draft Test passes it; the saved-row Test and the active-store selection do not, so they check what every process will find.
    """
    builder = build or build_artifact_storage
    store: ArtifactStorage | None = None
    artifact_id: str | None = None
    deleted = False
    step: ProbeStep = "build"
    try:
        if prepare:
            step = "prepare"
            await prepare_artifact_provider(row)
            step = "build"
        store = builder(row, storage_provider=storage_provider)
        step = "initialize"
        await store.initialize()
        step = "put"
        artifact_id = await store.put(
            data=PROBE_DATA,
            mime_type=PROBE_MIME,
            filename=PROBE_FILENAME,
        )
        step = "get"
        blob = await store.get(artifact_id)
        if blob is None:
            raise RuntimeError("probe object was not found after it was written")
        if (
            blob.data != PROBE_DATA
            or blob.mime_type != PROBE_MIME
            or blob.filename != PROBE_FILENAME
        ):
            raise RuntimeError("probe object read-back did not match what was written")
        step = "delete"
        await store.delete(artifact_id)
        deleted = True
        if await store.get(artifact_id) is not None:
            raise RuntimeError("probe object still exists after delete")
        return ArtifactProbeResult(ok=True)
    except Exception as exc:  # noqa: BLE001 - a probe reports every backend failure
        return ArtifactProbeResult(
            ok=False,
            step=step,
            error=probe_failure_text(exc, row.config),
        )
    finally:
        if store is not None and artifact_id is not None and not deleted:
            try:
                await store.delete(artifact_id)
            except Exception:  # noqa: BLE001 - best-effort cleanup after the reported failure
                logger.warning(
                    "artifact probe cleanup failed for provider %r",
                    row.id,
                    exc_info=True,
                )
        if store is not None:
            try:
                await store.aclose()
            except Exception:  # noqa: BLE001 - the probe result is already decided
                logger.warning(
                    "artifact probe close failed for provider %r",
                    row.id,
                    exc_info=True,
                )


__all__ = [
    "ArtifactProbeResult",
    "PROBE_DATA",
    "PROBE_FILENAME",
    "PROBE_MIME",
    "ProbeStep",
    "probe_artifact_provider",
    "probe_failure_text",
]
