"""Run a component's refusal extractor (``MC_extractError`` and its siblings) on the REAL ``ApiError`` of ``ui/foundation/api.js``, in MiniRacer.

The extractors are one-line delegations to ``window.primerApi.readRefusal`` (ticket 01a11cd1-7aaf); a page test that wants to know what one returns for an
envelope runs the real foundation and the real function, not a substring of the source.
"""

from __future__ import annotations

import json
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _function_source(path: str, name: str) -> str:
    text = (UI / path).read_text(encoding="utf-8")
    start = text.index(f"function {name}(")
    depth, i = 0, text.index("{", start)
    while True:
        depth += text[i] == "{"
        depth -= text[i] == "}"
        i += 1
        if depth == 0:
            return text[start:i]


def call_extractor(path: str, name: str, envelope: dict) -> dict:
    """``{code, message}`` the component function ``name`` in ``ui/<path>`` returns for a thrown ``ApiError`` built from ``envelope``."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    try:
        ctx.eval("var window = {};")
        ctx.eval((UI / "foundation" / "api.js").read_text(encoding="utf-8"))
        ctx.eval(_function_source(path, name))
        return json.loads(ctx.eval(f"JSON.stringify({name}(new window.primerApi.ApiError({json.dumps(envelope)})))"))
    finally:
        ctx.close()
