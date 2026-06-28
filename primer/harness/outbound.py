"""Outbound harness build: assemble a renderable bundle from tracked entities.

Given a Harness with ``direction == outbound`` and a list of
``TrackedEntity`` rows, walk the live entities out of storage, strip
system fields, apply the user-picked override mappings (replacing each
mapped JSON-pointer location with a ``{{ overrides.<path> }}`` token),
and YAML-serialize each into ``templates/<template_name>.yaml``.

Also produces the surrounding ``harness.yaml`` metadata file, the
composed ``overrides.schema.json`` (derived from every mapping's
current value), and a stable ``bundle_hash`` over the whole set.

The result is a :class:`BuildResult` that the dispatch layer hands to
``primer.harness.git.push_bundle``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import yaml

from primer.harness.templatize import (
    _resolve_pointer,
    apply_override_mappings,
    compose_overrides_schema_from_mappings,
)
from primer.int.storage_provider import StorageProvider
from primer.model.harness import Harness, OverrideMapping


class OutboundBuildError(Exception):
    """Raised by :func:`build_outbound` with a structured error code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        template_name: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.template_name = template_name


@dataclass
class OutboundFile:
    """One file in the rendered outbound bundle."""

    template_path: str
    rendered_text: str
    source_bytes: bytes
    # For documents using content_path:
    content_path: str | None = None
    content_bytes: bytes | None = None


@dataclass
class BuildResult:
    """Output of :func:`build_outbound`."""

    files: list[OutboundFile]
    bundle_hash: str
    overrides_schema: dict[str, Any]


_KIND_MODELS: dict[str, type] | None = None


def _kind_models() -> dict[str, type]:
    """Lazy mapping from TrackedEntity.kind to its Pydantic model class.

    Lazy because importing the model modules eagerly at module-load
    time pulls in a chunk of the application; we only need them on
    actual outbound builds.
    """
    global _KIND_MODELS
    if _KIND_MODELS is not None:
        return _KIND_MODELS
    from primer.model.agent import Agent
    from primer.model.collection import Collection, Document
    from primer.model.graph import Graph
    from primer.model.provider import Toolset

    _KIND_MODELS = {
        "agent": Agent,
        "graph": Graph,
        "collection": Collection,
        "document": Document,
        "toolset": Toolset,
    }
    return _KIND_MODELS


async def build_outbound(
    harness: Harness,
    *,
    storage_provider: StorageProvider,
) -> BuildResult:
    """Render every tracked entity into a YAML template + overrides schema.

    See module docstring for the high-level flow. Errors are surfaced
    as :class:`OutboundBuildError` with a stable ``code`` field that
    the API layer maps to HTTP status codes.
    """
    if not harness.tracked_entities:
        raise OutboundBuildError(
            "outbound_no_entities",
            "harness has no tracked entities to build",
        )

    seen_template_names: set[str] = set()
    template_files: list[OutboundFile] = []
    all_mappings: list[OverrideMapping] = []
    all_values: dict[str, Any] = {}

    for te in harness.tracked_entities:
        if te.template_name in seen_template_names:
            raise OutboundBuildError(
                "outbound_template_name_collision",
                f"two tracked entities share template_name {te.template_name!r}",
                template_name=te.template_name,
            )
        seen_template_names.add(te.template_name)

        model_cls = _kind_models()[te.kind]
        storage = storage_provider.get_storage(model_cls)
        entity = await storage.get(te.source_id)
        if entity is None:
            raise OutboundBuildError(
                "outbound_entity_missing",
                f"{te.kind} {te.source_id!r} not found",
                template_name=te.template_name,
            )
        if getattr(entity, "harness_id", None) is not None:
            raise OutboundBuildError(
                "outbound_entity_managed",
                (
                    f"{te.kind} {te.source_id!r} is managed by harness "
                    f"{entity.harness_id!r}"
                ),
                template_name=te.template_name,
            )

        payload = entity.model_dump(mode="json")
        # Strip system fields that don't belong in a template's spec
        for sysf in ("id", "harness_id", "created_at", "updated_at"):
            payload.pop(sysf, None)

        # Resolve current values for schema inference. Each mapping's
        # field_path must resolve against the stripped payload — that's
        # what the template author was pointing at in the UI.
        for m in te.overrides:
            try:
                all_values[m.field_path] = _resolve_pointer(payload, m.field_path)
            except KeyError as exc:
                raise OutboundBuildError(
                    "outbound_field_path_invalid",
                    (
                        f"field_path {m.field_path!r} does not resolve in "
                        f"{te.kind} {te.source_id!r}"
                    ),
                    template_name=te.template_name,
                ) from exc

        templated = apply_override_mappings(payload, te.overrides)
        all_mappings.extend(te.overrides)

        template_body = {
            "kind": te.kind,
            "name": te.template_name,
            "spec": templated,
        }
        # A Document's body lives in the content store, out-of-band of the
        # entity model (the spec has no content field). Export it as
        # ``content_inline`` so the inbound install restores + indexes it;
        # without this a tracked document ships as an empty shell.
        if te.kind == "document":
            body = await storage_provider.get_content_store().get(te.source_id)
            if body is not None:
                template_body["content_inline"] = body
        text = yaml.safe_dump(template_body, sort_keys=True)
        template_files.append(
            OutboundFile(
                template_path=f"templates/{te.template_name}.yaml",
                rendered_text=text,
                source_bytes=text.encode("utf-8"),
            )
        )

    overrides_schema = compose_overrides_schema_from_mappings(
        all_mappings, all_values,
    )

    harness_yaml = {
        "apiVersion": "primer/v1",
        "kind": "Harness",
        "metadata": {
            "name": harness.name,
            "description": harness.description or "",
            "version": "1.0.0",
        },
    }
    harness_yaml_text = yaml.safe_dump(harness_yaml, sort_keys=True)
    schema_text = json.dumps(overrides_schema, indent=2, sort_keys=True)

    files: list[OutboundFile] = [
        OutboundFile(
            template_path="harness.yaml",
            rendered_text=harness_yaml_text,
            source_bytes=harness_yaml_text.encode("utf-8"),
        ),
        OutboundFile(
            template_path="overrides.schema.json",
            rendered_text=schema_text,
            source_bytes=schema_text.encode("utf-8"),
        ),
        *template_files,
    ]

    h = hashlib.sha256()
    for f in sorted(files, key=lambda x: x.template_path):
        h.update(f.template_path.encode("utf-8") + b"\0" + f.source_bytes + b"\0")
    bundle_hash = h.hexdigest()

    return BuildResult(
        files=files,
        bundle_hash=bundle_hash,
        overrides_schema=overrides_schema,
    )


__all__ = [
    "BuildResult",
    "OutboundBuildError",
    "OutboundFile",
    "build_outbound",
]
