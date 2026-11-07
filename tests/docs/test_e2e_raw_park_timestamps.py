from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RAW_PARK_HELPERS = {
    "tests/e2e/test_yields_with_injected_park.py": "_inject_park",
    "tests/e2e/test_yields_respond_and_cancel_effects.py": "_inject_park",
    "tests/e2e/test_multi_cycle_resume_stability_journey.py": "_inject_approval_park",
    "tests/e2e/test_watcher_predicate_workers.py": "_inject_park",
    "tests/e2e/test_pause_while_parked_resume_cycle_journey.py": "_inject_running_ask_user_park",
    "tests/ui_e2e/test_approvals_journey.py": "_inject_approval_park_async",
}


def _helper(relative: str, name: str) -> ast.AsyncFunctionDef:
    source = (ROOT / relative).read_text(encoding="utf-8")
    assert "'{parked_until}'" in source, f"{relative} must remain a raw park injector"
    matches = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(matches) == 1, f"expected one {name} in {relative}"
    return matches[0]


def test_raw_sql_park_timestamps_use_storage_json_spelling() -> None:
    for relative, helper_name in RAW_PARK_HELPERS.items():
        helper = _helper(relative, helper_name)
        calls = [node for node in ast.walk(helper) if isinstance(node, ast.Call)]

        noncanonical = [
            call
            for call in calls
            if isinstance(call.func, ast.Attribute) and call.func.attr == "isoformat"
        ]
        assert not noncanonical, (
            f"{relative}:{helper_name} must not inject datetime.isoformat(); "
            "use to_jsonable_python so raw SQL matches dump_for_storage"
        )

        canonical_args = {
            ast.unparse(call.args[0])
            for call in calls
            if isinstance(call.func, ast.Name)
            and call.func.id == "to_jsonable_python"
            and len(call.args) == 1
        }
        assert {"now", "parked_until"} <= canonical_args, (
            f"{relative}:{helper_name} must canonicalize parked_at and parked_until"
        )

        started_at_values = [
            value
            for node in ast.walk(helper)
            if isinstance(node, ast.Dict)
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and key.value == "started_at"
        ]
        assert len(started_at_values) == 1
        assert ast.unparse(started_at_values[0]) == "to_jsonable_python(now)", (
            f"{relative}:{helper_name} must canonicalize parked_state.started_at"
        )
