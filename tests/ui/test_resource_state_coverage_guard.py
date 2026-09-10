"""Guard: the named high-value liveness surfaces must consume `degraded`.

2026-09-10 ruling (after the app-wide useResource survey found 0/192 call
sites reading `degraded`): migrating all 192 was rejected as a large-
blast-radius refactor for low per-site value. Instead, a DEFINED subset -
the surfaces an operator actually stares at waiting for liveness truth -
were migrated onto ui/foundation/use-resource.js's resourceState()
helper, and this guard keeps them migrated:

  - ui/components/console/nv-system.jsx: NV_HealthCards (scheduler/worker
    pool/sessions active/attention tiles), NV_AttentionEverywhere (the
    cross-workspace "needs a human" table), NV_WorkerFleet (the worker
    list) - the System dashboard IS the liveness page.
  - ui/components/health.jsx: the standalone health page - the strongest
    existing pattern before this round (a real three-way split), upgraded
    from raw `.error` (flips on a single blip) to `.degraded` (debounced).

Deliberately scoped to these two files, not tests/e2e's directory-wide
guard shape: most useResource consumers app-wide are ordinary CRUD lists
where "still loading" indefinitely on a fetch that never resolves is a
real but much lower-stakes gap, out of scope for this round (see task
01a08a8b's own counts for the long tail, left as a follow-up). Widening
this guard to every useResource call site would be the same mistake in
reverse - see the tests/e2e guard's own docstring on the same principle
in the other direction.

A file entering this list is a deliberate per-file decision (edit
_GUARDED_FILES below and say why in the commit), not something this test
infers on its own.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"

# path relative to ui/ -> the useResource result variable names this file
# is expected to have paired with a resourceState() call. A file/variable
# entering here is what "migrated" means for the purposes of this guard.
_GUARDED_FILES = {
    "components/console/nv-system.jsx": {
        "health", "activeSessions", "attention", "pending", "workers",
    },
    "components/health.jsx": {"health"},
}

_ASSIGN_RE_TEMPLATE = r'(?:var|const|let)\s+({names})\s*=\s*(?:window\.primerApi\.)?useResource\('


def test_named_liveness_surfaces_consume_resource_state() -> None:
    missing = []
    for rel_path, expected_vars in _GUARDED_FILES.items():
        path = UI / rel_path
        text = path.read_text(encoding="utf-8")

        assign_re = re.compile(
            _ASSIGN_RE_TEMPLATE.format(names="|".join(re.escape(v) for v in expected_vars))
        )
        found_vars = set(assign_re.findall(text))
        unaccounted = expected_vars - found_vars
        if unaccounted:
            missing.append(
                f"{rel_path}: expected useResource call(s) for {sorted(unaccounted)} "
                "not found at all - _GUARDED_FILES is out of date, fix the list"
            )
            continue

        for var in expected_vars:
            if not re.search(rf'resourceState\(\s*{re.escape(var)}\s*\)', text):
                missing.append(
                    f"{rel_path}: `{var}` is fetched via useResource but never passed "
                    "to resourceState() anywhere in the file - this surface is expected "
                    "to distinguish loading/stuck/ready (see this test's own docstring), "
                    "not just data == null"
                )

    assert not missing, (
        "One or more named liveness surfaces stopped consuming resourceState():\n  "
        + "\n  ".join(missing)
    )


def test_guarded_files_still_exist_and_use_useresource_at_all() -> None:
    # If a guarded file's useResource calls are removed entirely (the
    # component was rewritten, the resource renamed), the regex above
    # would silently report 0 unaccounted vars for the WRONG reason -
    # catch that degenerate case explicitly.
    for rel_path in _GUARDED_FILES:
        path = UI / rel_path
        assert path.is_file(), f"{rel_path} no longer exists - update _GUARDED_FILES"
        assert "useResource(" in path.read_text(encoding="utf-8"), (
            f"{rel_path} no longer calls useResource at all - "
            "update _GUARDED_FILES if this surface was rewritten"
        )
