"""G10-2 (audit): the catalog's per-provider profile counts read every model
profile with a catch that resolves a FAILED read to a bare empty list, so
every LLM provider card silently renders "0 profiles" and the profiles note
(``PR_PagedNote``) stays silent - it only speaks for a read cut short at the
page cap, which the ``PR_fetchAllPages`` envelope already carries. The class
reads one line above keep the same failure (``PR_partialFailure``) and say it
in the class's error; the profiles read must keep its error the same way, and
the profiles note must render the failure instead of the silent 0.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CATALOG = (UI / "components" / "provider-catalog.jsx").read_text(encoding="utf-8")
API = (UI / "foundation" / "api.js").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    i = src.index(f"function {name}(")
    brace = src.index("{", i)
    depth = 0
    for k in range(brace, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_the_profiles_read_keeps_its_error() -> None:
    """The /model_profiles read's catch must keep the error on the result it hands the
    page (the class reads' shape), not swallow it into an empty list."""
    i = CATALOG.index('PR_fetchAllPages("/model_profiles"')
    region = CATALOG[i:i + 400]
    m = re.search(r"\.catch\(\s*(?:\(\s*(\w*)\s*\)|\w+)\s*=>\s*\(\{([^}]*)\}\)\)", region)
    assert m, "the profiles read has no .catch keeping a result shape"
    assert "error" in m.group(2), f"the catch swallows the failure: {m.group(0)}"


def test_the_profiles_note_renders_a_failed_read() -> None:
    """The real ``PR_PagedNote`` handed a failed profiles read must name the failure
    (MiniRacer over the extracted note chain, scalar return)."""
    from tests.ui._mini_react import mini_react_context

    code = "\n;\n".join([_fn(API, "PR_nounFor"), _fn(API, "PR_pagedNote"), _fn(API, "PR_PagedNote")])
    prelude = (
        """
function textOf(node) {
  var out = [];
  (function collect(n) {
    if (n == null || typeof n === "boolean") return;
    if (typeof n === "string" || typeof n === "number") { out.push(String(n)); return; }
    if (Array.isArray(n)) { n.forEach(collect); return; }
    if (n.__el) collect(n.children);
  })(node);
  return out.join("");
}
"""
    )
    ctx = mini_react_context(code, prelude)
    ctx.eval(
        'MR.mount(PR_PagedNote, { list: { items: [] }, error: new Error("boom"), '
        'noun: "LLM profiles", testid: "pc-truncated:profiles" });'
    )
    out = json.loads(
        ctx.eval('JSON.stringify(MR.find("pc-truncated:profiles") ? textOf(MR.find("pc-truncated:profiles")) : "")')
    )
    ctx.close()
    assert "boom" in out, f"the note rendered {out!r} for a failed read"
