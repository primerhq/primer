"""Only an allowlisted module reads a park blob's tool_wait batch keys itself (plan 3.3, the lead's ruling C6).

``primer.session.tool_wait_batches.batches_referenced_by_park`` is the one membership test over a ``parked_state``
blob. Seven modules read the batch keys (``pending_tool_waits``, ``outstanding_task_ids``, ``notifying_task_ids``)
from a blob on main; they are frozen in ``ALLOWLIST`` below, each with the reason it reads them. A module outside the
list that reads one fails ``test_no_module_outside_the_allowlist_reads_the_batch_keys``; an allowlisted module that
no longer reads any fails ``test_every_allowlisted_module_still_reads_the_batch_keys``, so the PR that moves a reader
onto the helper also removes its entry and the list only shrinks.

What counts as a read (``blob_key_reads``): the key spelled as a string literal in a subscript load
(``blob["pending_tool_waits"]``), as the first argument of ``.get`` / ``.pop`` / ``.setdefault``, on the left of an
``in`` / ``not in`` test, or as a ``match`` mapping-pattern key. Attribute access is NOT a read: the same names are
attributes of typed in-memory objects that never come from a blob (the ``ToolWaitPark`` exception, the graph
executor's ``_PendingToolWait``), and counting them would flag ``graph/base.py``, ``graph/_node_dispatch.py`` and
``model/yield_.py``, none of which sees a park blob. Writes (a subscript store, a dict-literal key, a constructor
keyword) are not reads either: producers build the blob, the rule is about who interprets it.

Known blind spots of this literal scan, each accepted on purpose:

* ``notifying_results``, a graph entry's notifying key, is not scanned (``test_these_are_not_reads`` pins it): the
  ruling names three keys, and every module that reads it today is already on the list for the other two; a reader
  reaches an entry through ``pending_tool_waits`` or is handed the entries by a module that does.
* a key held in a variable, or spelled inside a collection literal used as a key set (``yield_runtime``'s
  ``{...} - set(data)`` required-keys check);
* an attribute read on a ``ToolWaitParkedState`` rebuilt from a blob by ``from_jsonable``;
* a key inside a SQL or JSON-path string (``"data->'graph_checkpoint'->'pending_tool_waits'"``);
* a storage path tuple naming it, such as ``("parked_state", "graph_checkpoint", "pending_tool_waits")`` handed to
  ``patch_if(set_paths=...)`` or a ``find`` filter;
* ``operator.itemgetter("pending_tool_waits")`` and similar indirection.

The scan walks the files under ``primer/`` on disk, not the files git tracks, so an untracked stray ``.py`` there that
reads a key fails it locally (and nowhere else).
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BLOB_KEYS = frozenset({"pending_tool_waits", "outstanding_task_ids", "notifying_task_ids"})
HELPER = "primer/session/tool_wait_batches.py"
_GET_METHODS = frozenset({"get", "pop", "setdefault"})

# FROZEN at the commit that introduced this test. Remove an entry when its module stops reading the keys (moved onto
# the helper); never add one: a new reader calls batches_referenced_by_park instead.
ALLOWLIST: dict[str, str] = {
    "primer/graph/_checkpoint.py": (
        "restore_state rebuilds the executor's own pending tool_wait entries from its snapshot"
    ),
    "primer/session/dispatch.py": (
        "both graph park arms hand graph_checkpoint['pending_tool_waits'] to the row materializer"
    ),
    "primer/session/persistence.py": (
        "materialize_pending_tool_wait_rows creates each entry's rows and stores its ids qualified"
    ),
    "primer/worker/graph_resume_coordinator.py": (
        "the mixed-park resume hands the entries to the readiness check and the re-park flattens them"
    ),
    "primer/worker/graph_resume.py": (
        "the resume drain materializes the rows of the re-park's own entries"
    ),
    "primer/worker/tool_wait_resume_coordinator.py": (
        "the tool_wait resume reads the entries and resolves each one's readiness from its rows"
    ),
    "primer/worker/yield_runtime.py": (
        "ToolWaitParkedState.from_jsonable is the tool_wait blob's own parser"
    ),
}


def _key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in BLOB_KEYS:
        return node.value
    return None


def blob_key_reads(source: str) -> list[tuple[int, str]]:
    """``(line, key)`` for every read of a batch key in ``source`` (see the module docstring for what is a read)."""
    reads: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        key: str | None = None
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            key = _key(node.slice)
        elif (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in _GET_METHODS and node.args
        ):
            key = _key(node.args[0])
        elif isinstance(node, ast.Compare) and any(isinstance(op, ast.In | ast.NotIn) for op in node.ops):
            key = _key(node.left)
        elif isinstance(node, ast.MatchMapping):
            key = next((k for k in map(_key, node.keys) if k is not None), None)
        if key is not None:
            reads.append((node.lineno, key))
    return sorted(reads)


def readers(root: Path) -> dict[str, list[tuple[int, str]]]:
    """Every module under ``root/primer`` that reads a batch key, by its repo-relative path."""
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted((root / "primer").rglob("*.py")):
        reads = blob_key_reads(path.read_text(encoding="utf-8"))
        if reads:
            found[path.relative_to(root).as_posix()] = reads
    return found


def unexpected_readers(
    found: dict[str, list[tuple[int, str]]], allowlist: dict[str, str],
) -> dict[str, list[tuple[int, str]]]:
    return {path: reads for path, reads in found.items() if path != HELPER and path not in allowlist}


def stale_entries(found: dict[str, list[tuple[int, str]]], allowlist: dict[str, str]) -> list[str]:
    return sorted(path for path in allowlist if path not in found)


# ---------------------------------------------------------------------------
# The repository
# ---------------------------------------------------------------------------


def test_no_module_outside_the_allowlist_reads_the_batch_keys() -> None:
    unexpected = unexpected_readers(readers(ROOT), ALLOWLIST)

    assert not unexpected, (
        "these modules read a park blob's tool_wait batch keys themselves; ask "
        f"primer.session.tool_wait_batches.batches_referenced_by_park instead: {unexpected}"
    )


def test_every_allowlisted_module_still_reads_the_batch_keys() -> None:
    stale = stale_entries(readers(ROOT), ALLOWLIST)

    assert not stale, f"these modules no longer read the batch keys; remove their ALLOWLIST entries: {stale}"


def test_the_helper_is_the_sanctioned_reader_and_is_not_on_the_allowlist() -> None:
    """The exemption names a real module that does read the keys (an exemption for nothing would be stale too)."""
    assert HELPER in readers(ROOT)
    assert HELPER not in ALLOWLIST


def test_every_allowlist_entry_names_an_existing_module_with_a_reason() -> None:
    assert len(ALLOWLIST) == 7
    for path, reason in ALLOWLIST.items():
        assert (ROOT / path).is_file(), path
        assert reason.strip(), path


# ---------------------------------------------------------------------------
# The check itself, over a synthetic package
# ---------------------------------------------------------------------------


def _package(tmp_path: Path, modules: dict[str, str]) -> Path:
    for rel, source in modules.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return tmp_path


def test_a_new_reader_outside_the_allowlist_is_reported(tmp_path: Path) -> None:
    root = _package(tmp_path, {
        "primer/worker/yield_runtime.py": "def f(data):\n    return data['outstanding_task_ids']\n",
        HELPER: "def g(blob):\n    return blob.get('pending_tool_waits')\n",
        "primer/claim/adapters/new_reader.py": (
            "def live(task_id, parked_state):\n"
            "    entries = parked_state['graph_checkpoint'].get('pending_tool_waits') or []\n"
            "    return any(task_id in e['outstanding_task_ids'] for e in entries)\n"
        ),
    })

    unexpected = unexpected_readers(readers(root), ALLOWLIST)

    assert unexpected == {
        "primer/claim/adapters/new_reader.py": [(2, "pending_tool_waits"), (3, "outstanding_task_ids")],
    }


def test_an_allowlisted_module_that_stops_reading_is_reported_stale(tmp_path: Path) -> None:
    modules = {path: "def f(data):\n    return data['outstanding_task_ids']\n" for path in ALLOWLIST}
    modules["primer/session/persistence.py"] = (
        "from primer.session.tool_wait_batches import batches_referenced_by_park\n"
    )
    root = _package(tmp_path, modules)

    assert stale_entries(readers(root), ALLOWLIST) == ["primer/session/persistence.py"]


@pytest.mark.parametrize("source", [
    "blob['pending_tool_waits']",
    "blob['graph_checkpoint']['pending_tool_waits'][0]",
    "entry.get('outstanding_task_ids')",
    "blob.get('notifying_task_ids', [])",
    "blob.pop('pending_tool_waits', None)",
    "blob.setdefault('notifying_task_ids', [])",
    "'pending_tool_waits' in checkpoint",
    "'outstanding_task_ids' not in blob",
    "match blob:\n    case {'pending_tool_waits': entries}:\n        pass",
])
def test_these_are_reads(source: str) -> None:
    assert blob_key_reads(source) != []


@pytest.mark.parametrize("source", [
    "park.outstanding_task_ids",                                  # a typed object's attribute
    "self.outstanding_task_ids = ids",
    "ToolWaitPark(outstanding_task_ids=ids, event_key=k)",        # a constructor keyword
    "blob = {'outstanding_task_ids': ids, 'notifying_task_ids': []}",  # a producer's dict literal
    "pw['outstanding_task_ids'] = qualified",                     # a store
    "'''the blob's outstanding_task_ids / pending_tool_waits'''",  # prose
    "blob['parked_state']",
    "blob.get('notifying_results')",
])
def test_these_are_not_reads(source: str) -> None:
    assert blob_key_reads(source) == []
