"""A subdirectory conftest's fixtures reach its tests whatever the order of the file arguments.

Under pytest 9.1.x a file argument directly in ``tests/`` that sits between two arguments under a subpackage
(``tests/api/a.py tests/test_provider.py tests/api/b.py``) made pytest re-collect ``tests/`` into fresh ``Package``
nodes, and the subpackage conftest's fixtures stayed bound to the first ones: ``b.py`` failed at setup with
"fixture 'client' not found", and an autouse fixture (tests/llm's ``no_real_network`` guard) was silently dropped.
tests/conftest.py carries the fix; this runs pytest in a subprocess on exactly that order, with ``--setup-plan`` so
it resolves every fixture closure without running a test body.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Each subpackage file comes after a root-level module that follows an earlier file of the same subpackage.
ARGS = [
    "tests/api/test_artifact_storage.py",
    "tests/llm/test_anthropic_rate_limiter.py",
    "tests/test_provider.py",
    "tests/api/test_provider_secret_masking.py",
    "tests/llm/test_adapters_no_local_semaphore.py",
]

_PLANNED = re.compile(r"^\s*(tests/\S+?\.py::.+?) \(fixtures used: ([^)]*)\)$", re.MULTILINE)
_NOT_FOUND = re.compile(r"fixture '[^']+' not found")


def _setup_plan(args: list[str]) -> tuple[int, str, dict[str, set[str]]]:
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("PYTEST_", "COV_CORE_")) and k != "PRIMER_REQUIRE_POSTGRES_TESTS"
    }
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "-p", "no:cacheprovider", "--setup-plan", "-q", *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    out = done.stdout + done.stderr
    planned = {m.group(1): {f.strip() for f in m.group(2).split(",")} for m in _PLANNED.finditer(done.stdout)}
    return done.returncode, out, planned


def test_subpackage_conftest_fixtures_survive_an_interleaved_root_module():
    for arg in ARGS:
        assert (REPO_ROOT / arg).is_file(), f"{arg} moved: pick another file of the same directory for this order"

    code, out, planned = _setup_plan(ARGS)

    brief = "\n".join(line for line in out.splitlines() if _NOT_FOUND.search(line) or line.startswith("ERROR "))
    assert not _NOT_FOUND.search(out), f"a conftest fixture was not found:\n{brief}"
    assert code == 0, f"exit {code}:\n{brief or out[-4000:]}"

    def tests_in(path: str) -> dict[str, set[str]]:
        found = {nodeid: names for nodeid, names in planned.items() if nodeid.startswith(f"{path}::")}
        assert found, f"no planned test from {path} in the output:\n{out[-4000:]}"
        return found

    # tests/api/conftest.py: requested fixtures, in the file collected after the root module.
    for nodeid, names in tests_in("tests/api/test_provider_secret_masking.py").items():
        assert {"client", "app"} <= names, f"{nodeid} lost tests/api/conftest.py fixtures: {sorted(names)}"
    # tests/llm/conftest.py: an autouse guard, which goes missing silently rather than erroring.
    for nodeid, names in tests_in("tests/llm/test_adapters_no_local_semaphore.py").items():
        assert "no_real_network" in names, f"{nodeid} ran without the tests/llm autouse network guard: {sorted(names)}"
    # ...and the fix does not make a subpackage's fixtures global: the root module still sees neither.
    for nodeid, names in tests_in("tests/test_provider.py").items():
        assert not names & {"client", "app", "no_real_network"}, f"{nodeid} sees a subpackage fixture: {sorted(names)}"
