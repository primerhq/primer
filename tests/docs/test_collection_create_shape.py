"""S2 gate: the vector-space fields stay off the collection-create body.

S2 moved ``embedder`` and ``search_provider_id`` off ``POST
/v1/collections`` and onto ``PUT /v1/collections/{id}/search``, where the
latter is spelled ``vector_store_provider_id``. Nothing rejects the old
keys: an unknown key on a create body is dropped, and the create still
answers 201. So a caller written against the old shape gets a collection
with no search config, silently, and every search under it answers 409.

Nineteen call sites across eleven test modules were in exactly that
state. A plain grep cannot police this, because ``search_provider_id``
is still a live field on the internal-collections subsystem config; only
its appearance in a collection-create body is wrong. Hence the AST walk.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Keys S2 removed from the create body. ``search`` is legitimate on the
# model but is not accepted at create either: it is written by the
# search route, which validates the providers being bound.
RETIRED_CREATE_KEYS = frozenset({"embedder", "search_provider_id"})

_CREATE_URL = "/v1/collections"


def _offending_keys(call: ast.Call) -> list[str]:
    """Return retired keys in this call's ``json=`` dict, if any.

    Only ``*.post("/v1/collections", json={...})`` is examined. A URL
    built at runtime, or a body that is not a dict literal, is skipped:
    this gate catches the shape that was actually written, and does not
    try to be a type checker.
    """
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "post":
        return []
    if not call.args:
        return []
    url = call.args[0]
    if not isinstance(url, ast.Constant) or url.value != _CREATE_URL:
        return []
    for kw in call.keywords:
        if kw.arg != "json" or not isinstance(kw.value, ast.Dict):
            continue
        return [
            k.value
            for k in kw.value.keys
            if isinstance(k, ast.Constant) and k.value in RETIRED_CREATE_KEYS
        ]
    return []


def _scan(root: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover -- not this gate's job
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            keys = _offending_keys(node)
            if keys:
                rel = str(path.relative_to(ROOT))
                found.setdefault(f"{rel}:{node.lineno}", []).extend(keys)
    return found


def test_no_retired_keys_on_collection_create() -> None:
    offenders = {}
    for sub in ("tests", "primer", "scripts"):
        root = ROOT / sub
        if root.is_dir():
            offenders.update(_scan(root))
    assert not offenders, (
        "POST /v1/collections no longer accepts these keys; they are "
        "dropped silently and the collection comes out grep-only. Bind "
        "the vector space with PUT /v1/collections/{id}/search instead "
        f"(vector_store_provider_id, not search_provider_id): {offenders}"
    )


def test_the_gate_can_actually_see_an_offender() -> None:
    """A gate that cannot fail is not a gate.

    Parses the exact shape that was wrong at all nineteen sites and
    asserts the walk reports it, so a refactor that quietly stops
    matching (a renamed helper, a changed call form) shows up here
    rather than as a silently passing sweep.
    """
    tree = ast.parse(
        'client.post("/v1/collections", json={\n'
        '    "id": cid,\n'
        '    "embedder": {"provider_id": eid, "model": m},\n'
        '    "search_provider_id": sid,\n'
        "})\n"
    )
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert sorted(_offending_keys(call)) == ["embedder", "search_provider_id"]
