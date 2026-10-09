"""The ui-e2e lane collects: a name a ui_e2e module imports from another ui_e2e module exists there, and it is not a private name of a test module (review of #668, round 3, B1'').

A ui_e2e module that fails to import is a COLLECTION error, and one collection error stops the whole lane: nothing in it ran in CI, the sweep included. The cause was a helper that one test
module defined, other test modules imported (``from tests.ui_e2e.test_x import _seed_session``) and a refactor deleted. A test module is not a library: the shared helper lives in a module of its
own (``_session_seed.py``), and this file reads every ``from tests.ui_e2e... import name`` in the lane with ``ast`` and fails on a name the target does not define and on a private name taken from
another ``test_*.py``. (The lane is also collected whole in the PR's own run, which a unit file cannot do without Playwright.)

The first rule has no exceptions. The second has a BASELINE: thirteen imports of that shape were already on ``main`` (journeys that borrow another journey's ``_seed``), and moving them is a change to
eight journeys of other authors; the baseline can only shrink (an entry that is no longer a violation fails, so it is removed) and nothing new can join it.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LANE = ROOT / "tests" / "ui_e2e"


def _defined(tree: ast.Module) -> set[str] | None:
    """The names a module binds at its top level, or ``None`` when it re-exports with ``import *`` (nothing can be said)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.If | ast.Try):
            inner = ast.Module(body=[*node.body, *getattr(node, "orelse", [])], type_ignores=[])
            more = _defined(inner)
            if more:
                names |= more
    return names


PRIVATE = "private"
MISSING = "missing"


def problems_in(lane: Path, package: str = "tests.ui_e2e") -> list[tuple[str, str, int, str, str]]:
    """``(kind, file, line, name, target)`` for every import in ``lane``'s modules of a name from a sibling module that the sibling does not define (``missing``), of a module that is not in the
    lane (``missing``), or of a private name from a ``test_*.py`` (``private``)."""
    out: list[tuple[str, str, int, str, str]] = []
    for path in sorted(lane.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level or not (node.module or "").startswith(package):
                continue
            target_name = node.module[len(package):].lstrip(".")
            if not target_name:                                   # ``from tests.ui_e2e import _a11y`` imports sibling MODULES
                for alias in node.names:
                    if not (lane / f"{alias.name}.py").exists() and not (lane / alias.name).is_dir():
                        out.append((MISSING, path.name, node.lineno, alias.name, "(the lane)"))
                continue
            target = lane / f"{target_name}.py"
            if not target.exists():
                out.append((MISSING, path.name, node.lineno, "*", node.module))
                continue
            defined = _defined(ast.parse(target.read_text(encoding="utf-8")))
            for alias in node.names:
                if defined is not None and alias.name != "*" and alias.name not in defined:
                    out.append((MISSING, path.name, node.lineno, alias.name, target.name))
                elif target.name.startswith("test_") and alias.name.startswith("_"):
                    out.append((PRIVATE, path.name, node.lineno, alias.name, target.name))
    return out


# imports of a private name from another journey that were on main when this check was added (see the docstring): this set can only shrink
BASELINE = {
    ("test_mobile_session_rename_journey.py", "_seed", "test_failed_session_wording_journey.py"),
    ("test_mobile_session_rename_journey.py", "_start", "test_failed_session_wording_journey.py"),
    ("test_mobile_spaces_list_journey.py", "_seed", "test_trace_sidebar_journey.py"),
    ("test_mobile_tap_targets_journey.py", "_seed", "test_trace_sidebar_journey.py"),
    ("test_mobile_tap_targets_journey.py", "_wait_for_turn_to_settle", "test_trace_sidebar_journey.py"),
    ("test_rested_failure_note_journey.py", "_failed_session", "test_retry_failed_turn_journey.py"),
    ("test_retry_failed_turn_journey.py", "_seed", "test_trace_sidebar_journey.py"),
    ("test_setup_wizard_finish_checks_journey.py", "_FakeSetupApi", "test_setup_wizard_resume_journey.py"),
    ("test_setup_wizard_finish_checks_journey.py", "_connect_step_one", "test_setup_wizard_resume_journey.py"),
    ("test_setup_wizard_finish_checks_journey.py", "_open_wizard", "test_setup_wizard_resume_journey.py"),
    ("test_tab_groups_survive_reload_journey.py", "_seed", "test_trace_sidebar_journey.py"),
    ("test_trace_labels_journey.py", "_seed", "test_trace_sidebar_journey.py"),
    ("test_trace_labels_journey.py", "_wait_for_turn_to_settle", "test_trace_sidebar_journey.py"),
}


def test_every_import_between_ui_e2e_modules_names_something_that_exists() -> None:
    """The failure that stopped the lane: a name that another module no longer defines. No exceptions."""
    assert [p for p in problems_in(LANE) if p[0] == MISSING] == []


def test_no_new_ui_e2e_module_borrows_a_private_name_from_a_test_module_and_the_old_ones_only_go_away() -> None:
    found = {(file, name, target) for kind, file, _line, name, target in problems_in(LANE) if kind == PRIVATE}
    assert found - BASELINE == set(), "move the helper to a module of its own (tests/ui_e2e/_session_seed.py is one) instead of importing it from a test"
    assert BASELINE - found == set(), "these are no longer violations: remove them from BASELINE"


def test_the_check_itself_fails_on_the_shapes_that_broke_the_lane(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("def _helper():\n    pass\n\ndef public():\n    pass\n", encoding="utf-8")
    (tmp_path / "helper.py").write_text("def shared():\n    pass\nVALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_b.py").write_text(
        "from tests.ui_e2e.test_a import _helper\n"
        "from tests.ui_e2e.test_a import public\n"
        "from tests.ui_e2e.helper import shared, VALUE, gone\n"
        "from tests.ui_e2e.nowhere import anything\n"
        "from tests.ui_e2e import helper, missing_module\n",
        encoding="utf-8")
    found = problems_in(tmp_path)
    assert (PRIVATE, "test_b.py", 1, "_helper", "test_a.py") in found
    assert not any(p[2] == 2 for p in found), "a public name of a test module is not this check's business"
    assert (MISSING, "test_b.py", 3, "gone", "helper.py") in found
    assert not any(p[3] in ("shared", "VALUE") for p in found)
    assert (MISSING, "test_b.py", 4, "*", "tests.ui_e2e.nowhere") in found
    assert (MISSING, "test_b.py", 5, "missing_module", "(the lane)") in found
    assert len(found) == 4, found


def test_a_name_bound_by_an_import_or_a_conditional_counts_as_defined(tmp_path: Path) -> None:
    (tmp_path / "helper.py").write_text("import json\nfrom pathlib import Path as P\nif True:\n    LATE = 1\n", encoding="utf-8")
    (tmp_path / "test_c.py").write_text("from tests.ui_e2e.helper import json, P, LATE\n", encoding="utf-8")
    assert problems_in(tmp_path) == []
