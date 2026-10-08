"""Every writer of an approval park's ``original_call`` is settled (design note 01a11cd3-66b0, slice 1 condition (a)).

The Inbox card's allowlist is stamped into the same metadata as ``original_call`` (``resume_metadata["preview"]``). A site that writes ``original_call`` and forgets the stamp
leaves its card on the default rule (which hides all text), so every writer must be on this list with its reason, and a new one fails here until somebody settles it:

* ``approval_resume_metadata`` is THE builder: it takes the stamp as a parameter and every gate in the codebase calls it.
* the graph checkpoint's two blocks rebuild the metadata of a suspended TOOL_CALL node; they pass the stamp on from the pending call.
* the external-tool park is exempt: ``ExternalToolsetProvider`` is invocation-scoped and never gated (``ToolExecutionManager`` skips the external toolset), its park is
  ``external_call``, not ``_approval``, and has no card.
* the two "show all" routes (``list_pending_yields``, ``list_session_pending_yields``) re-emit a parked call whole in their RESPONSE: they write no park, and returning the
  whole call is their job (they are the access-controlled way to read what a card withholds).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# (file, function) -> why it is settled
SETTLED = {
    ("primer/agent/approval.py", "approval_resume_metadata"): "builder",
    ("primer/graph/_checkpoint.py", "_build_pending_park_yield"): "passes the stamp on",
    ("primer/graph/_checkpoint.py", "_toolcall_dispatch_entry"): "passes the stamp on",
    ("primer/agent/external_tools.py", "call"): "exempt: never an _approval park",
    ("primer/api/routers/workspaces.py", "list_pending_yields"): "response: the whole call, by design",
    ("primer/api/routers/workspaces.py", "list_session_pending_yields"): "response: the whole call, by design",
}


def _writers() -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for path in sorted((ROOT / "primer").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "original_call" not in source:
            continue
        tree = ast.parse(source)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

        def function_of(node: ast.AST) -> ast.AST | None:
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return node
            return None

        for node in ast.walk(tree):
            writes = (
                (isinstance(node, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "original_call" for k in node.keys))
                or (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) and isinstance(node.slice, ast.Constant) and node.slice.value == "original_call")
            )
            if not writes:
                continue
            function = function_of(node)
            name = function.name if function is not None else "<module>"
            found[(str(path.relative_to(ROOT)), name)] = ast.get_source_segment(source, function) if function is not None else ""
    return found


def test_every_writer_of_original_call_is_settled() -> None:
    writers = _writers()

    unsettled = sorted(set(writers) - set(SETTLED))
    gone = sorted(set(SETTLED) - set(writers))
    assert not unsettled, f"a new writer of original_call that nobody settled (stamp it or add it to SETTLED with a reason): {unsettled}"
    assert not gone, f"a settled writer is gone; remove it from SETTLED: {gone}"


def test_the_writers_that_must_carry_the_stamp_mention_it() -> None:
    writers = _writers()

    for key, reason in SETTLED.items():
        if reason.startswith(("exempt", "response")):
            continue
        body = writers[key]
        assert '"preview"' in body or "preview=" in body or "preview:" in body, f"{key} writes original_call but never mentions the preview stamp"
