"""The apply/create manifest envelope: ``{kind: <resource>, spec: <body>}``.

The envelope keeps dispatch metadata (``kind``) separate from the entity body
(``spec``) so it can never collide with a real entity field, and lets
``get -o yaml`` round-trip back into ``apply``.
"""

from __future__ import annotations

import json
from typing import Any

import yaml


class ManifestError(Exception):
    """Raised on a malformed manifest document."""


def parse_manifests(text: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for doc in yaml.safe_load_all(text):
        if doc is None:
            continue
        if not isinstance(doc, dict):
            raise ManifestError("manifest document must be a mapping")
        if "kind" not in doc:
            raise ManifestError("manifest document missing 'kind'")
        if "spec" not in doc or not isinstance(doc["spec"], dict):
            raise ManifestError("manifest document missing a 'spec' mapping")
        out.append((str(doc["kind"]), dict(doc["spec"])))
    if not out:
        raise ManifestError("no manifest documents found")
    return out


def dump_envelope(kind: str, body: dict[str, Any], *, fmt: str) -> str:
    env = {"kind": kind, "spec": body}
    if fmt == "json":
        return json.dumps(env, indent=2, ensure_ascii=False)
    return yaml.safe_dump(env, sort_keys=False)
