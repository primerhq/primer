"""Migration 8: embedding provider rows the provider-keyed validator refuses are repaired in place.

``EmbeddingProvider.config`` is a plain union of three config classes. Until the config was read as the class its ``provider`` names, pydantic's smart mode picked the member
that validated and set the most fields, whatever ``provider`` said, so two shapes were stored that the provider-keyed validator now refuses:

* ``openai`` with a missing or invalid url: it failed ``OpenAIConfig`` and was left with the member that set the most fields (``GoogleConfig`` when it had a key, so the url was dropped:
  ``{"api_key": ...}``; a ``HuggingFaceConfig`` shape when it had a token, ``{"token": ...}``, which was an ``openai`` row with only a token).

A third shape main stored, ``huggingface`` with only a url (an ``OpenAIConfig``: ``{"url": ..., "api_key": ..., "flavor": ...}``), is NOT broken any more: the HuggingFace token is optional
(a public local model needs none, as for ``HuggingFaceCrossEncoderConfig``), so the live model reads it and ignores the stray keys. An earlier version of this migration gave such a row
``{"token": ""}``; it is left as it is, since a migration should not rewrite a row that reads fine.

``Storage._from_row`` validates uncaught, so ONE such row answered 500 on the whole ``GET /v1/embedding_providers`` and on get, put and delete by id (the operator could not even
delete it), and each 500 logged pydantic's ``input_value`` with most of the stored key. A ``gemini`` row cannot be refused (``GoogleConfig`` takes any dict), so nothing is done for it.

What the repair does, per row, only when the live config class refuses it:

* ``openai``: the config becomes ``{"url": PLACEHOLDER_URL}`` plus the ``api_key`` and ``flavor`` the row had. There is no endpoint to restore. The row is KEPT, not deleted: its id
  may be named by a collection, and removing an operator's row at boot is not ours to do. The placeholder is ``https://`` on the reserved ``.invalid`` domain (RFC 6761), so it says what
  to fix where the console shows the Base URL and a use of it fails loudly. It is https because the row keeps its stored key and a use sends ``Authorization: Bearer <key>``:
  over plain http that would go to whatever resolves the name (a k8s search list, an NXDOMAIN-hijacking resolver), while no CA can issue a certificate for ``.invalid``, so over
  https the TLS handshake cannot succeed and fails before any header is sent. A HuggingFace token is not an OpenAI key, so it is not carried over.

**Why this module redefines EmbeddingProvider.** The live model refuses these rows (that is the point), so they cannot be read through it; the shadow class below reads
``provider`` and ``config`` untyped and keeps every other field (``models``, ``limits``) as it is (the table is selected by the class NAME, as in m007). The config is a plain dict
in storage, secrets in the clear, and is written back as one.

The log names the row id and the provider and never the config. Idempotent: a repaired row validates, so a second run finds nothing.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.providers.embedding import OpenAIConfig, OpenAIEmbeddingFlavor
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)

_PAGE = 200

#: The base URL an ``openai`` row with no usable endpoint gets: on the reserved ``.invalid`` domain, and https. The row keeps its stored key, and a use of it sends
#: ``Authorization: Bearer <key>``: over http that would go to whatever resolves the name (a search list, an NXDOMAIN-hijacking resolver), while no CA can issue a
#: certificate for ``.invalid``, so over https the TLS handshake fails before any header is sent.
PLACEHOLDER_URL = "https://base-url-not-set.invalid/"


class EmbeddingProvider(Identifiable):  # noqa: N801 - the class name selects the storage table
    """Migration-local view of a row: ``provider`` and ``config`` are read untyped, every other field is kept as it is."""

    model_config = ConfigDict(extra="allow")

    provider: str | None = Field(default=None)
    config: dict[str, Any] | None = Field(default=None)


def _accepts(config_cls: type[BaseModel], config: dict[str, Any]) -> bool:
    """Whether the live config class takes ``config``. The error is dropped on purpose: pydantic's text carries the input, and the input is a stored key."""
    try:
        config_cls.model_validate(config)
    except ValidationError:
        return False
    return True


def _repaired(provider: str | None, config: dict[str, Any] | None) -> tuple[dict[str, Any], str] | None:
    """``(new config, what was wrong)`` for a row the live model refuses, else ``None``."""
    if not isinstance(config, dict):
        return None
    if provider == "openai" and not _accepts(OpenAIConfig, config):
        fixed: dict[str, Any] = {"url": PLACEHOLDER_URL}
        if isinstance(config.get("api_key"), str):
            fixed["api_key"] = config["api_key"]
        if config.get("flavor") in {flavor.value for flavor in OpenAIEmbeddingFlavor}:
            fixed["flavor"] = config["flavor"]
        return fixed, "openai config without a usable url"
    return None


class M008EmbeddingProviderRepair:
    """Make every stored embedding provider row readable by the provider-keyed validator."""

    version = 8
    description = "embedding provider rows the provider-keyed validator refuses are repaired"

    async def apply(self, sp: StorageProvider) -> None:
        storage = sp.get_storage(EmbeddingProvider)
        rows: list[EmbeddingProvider] = []
        offset = 0
        while True:
            page = await storage.list(OffsetPage(offset=offset, length=_PAGE))
            rows.extend(page.items)
            if len(page.items) < _PAGE:
                break
            offset += _PAGE
        for row in rows:
            repair = _repaired(row.provider, row.config)
            if repair is None:
                continue
            new_config, reason = repair
            await storage.update(row.model_copy(update={"config": new_config}))
            logger.warning(
                "repaired an embedding provider row the provider-keyed validator refuses",
                extra={"provider_id": row.id, "provider": row.provider, "repair": reason},
            )


__all__ = ["M008EmbeddingProviderRepair", "PLACEHOLDER_URL"]
