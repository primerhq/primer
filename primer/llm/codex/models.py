"""The codex provider's model rule (the codex plan, spec section 6.6): only
direct-tool models are used, so Codex's JavaScript ``exec`` and ``wait``
tools are never registered.

A model is direct when Codex will hand it primer's tools as direct function
tools and register no code runtime. Two sources, in order:

* ``CODEX_DIRECT_MODELS``: the models verified direct with the section 6.2
  overrides at the tested Codex version (the builder keeps it current from
  the live checks); and
* the operator's model catalog file (Codex's ``model_catalog_json``): an
  entry marked ``"tool_mode": "direct"``. Primer reads only that
  operator-supplied file, never Codex's own cache.

Any read or parse error, a missing file, a file that is not a regular
file (a FIFO at the path would block the read), a file over the size cap,
or a model absent from the file means not direct (``False``), never an
exception: a broken catalog must refuse the model, not take the provider
down.
"""

from __future__ import annotations

import json
import os
import stat

#: The models verified direct at ``CODEX_MAX_TESTED_VERSION``
#: (``{"gpt-5.5"}`` at ``rust-v0.169.0``, spec 6.6); the live-check task
#: keeps this current from the live checks.
CODEX_DIRECT_MODELS: frozenset[str] = frozenset({"gpt-5.5"})

#: The Codex release the rule was verified against. The live-check task
#: raises it on a host with Codex installed.
MINIMUM_VERSION = "0.169.0"
CODEX_MAX_TESTED_VERSION: str = "0.169.0"

#: The catalog is the operator's own file, read synchronously on the write
#: path; cap it so a huge file cannot cost a profile write the memory of an
#: unbounded read.
MAX_CATALOG_BYTES = 4 * 1024 * 1024


def _catalog_tool_modes(catalog_path: str | None) -> dict[str, str | None]:
    """The operator's catalog as ``slug -> tool_mode`` (the file's ``models``
    list, or the file itself when it is a top-level list); ``{}`` when the
    file is missing or is not a regular file, is over ``MAX_CATALOG_BYTES``,
    or cannot be read or parsed, so the caller sees an empty catalogue
    instead of an exception."""
    if catalog_path is None:
        return {}
    try:
        st = os.stat(catalog_path)
    except OSError:
        return {}
    # A FIFO would block the read and a directory cannot be read; refuse
    # both instead of opening them.
    if not stat.S_ISREG(st.st_mode):
        return {}
    try:
        with open(catalog_path, "rb") as fh:
            raw = fh.read(MAX_CATALOG_BYTES + 1)
    except OSError:
        return {}
    if len(raw) > MAX_CATALOG_BYTES:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        # RecursionError (a RuntimeError) escapes json.loads on a deeply
        # nested file; a truncated one over the cap is a ValueError.
        return {}
    entries = data.get("models") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return {}
    out: dict[str, str | None] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str):
            continue
        tool_mode = entry.get("tool_mode")
        out[slug] = tool_mode if isinstance(tool_mode, str) else None
    return out


def is_direct_model(model: str, *, catalog_path: str | None) -> bool:
    """Whether ``model`` is a direct-tool model for the codex provider.

    True when the model is in ``CODEX_DIRECT_MODELS`` or the entry for it in
    the operator's catalog file (``catalog_path``) carries
    ``"tool_mode": "direct"``. Any read or parse error, a missing file, or a
    model absent from the file means not direct: the rule refuses, it never
    raises.
    """
    if model in CODEX_DIRECT_MODELS:
        return True
    return _catalog_tool_modes(catalog_path).get(model) == "direct"
