"""The Postgres lane and its single gate must stay wired.

The live-Postgres suites skip without a database. They ran nowhere for a
long time because no CI job configured one and three different env names
guarded them, so a claim engine that could not pass its own tests and a
hanging LISTEN teardown went unseen. These tests pin the three things that
keep that from recurring.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

_OLD_NAMES = ("PRIMER_TEST_PG_DSN", "PRIMER_PG_TEST_DSN")


def test_no_test_reads_a_gate_name_directly():
    """tests/pg_gate.py is the only reader; the old names exist there only
    as deprecated aliases."""
    allowed = {"tests/pg_gate.py", "tests/tooling/test_pg_gate.py",
               "tests/docs/test_postgres_lane.py"}
    # A quoted literal of any gate name is a read waiting to happen (as an
    # os.environ key, a constant, a skipif); prose in a docstring is fine.
    pattern = re.compile(
        r"""["'](?:PRIMER_TEST_POSTGRES_URL|PRIMER_TEST_PG_DSN|PRIMER_PG_TEST_DSN)["']"""
    )
    # ...and a read through a constant that merely aliases the name
    # (`os.environ[_DSN_ENV]`) is the same thing one step removed. Another
    # branch added exactly that to the scheduler tests while this one was open.
    via_constant = re.compile(
        r"os\.(?:environ|getenv)(?:\.get)?\W{1,3}"
        r"(?:_DSN_ENV|_URL_ENV|_POSTGRES_URL_ENV|CANONICAL_ENV)\b"
    )
    offenders = []
    for path in (ROOT / "tests").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in allowed:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line) or via_constant.search(line):
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        f"read the Postgres gate through tests.pg_gate, not os.environ: {offenders}"
    )


def test_deprecated_names_are_gone_from_workflows_and_config():
    """Docs may name the deprecated aliases (to say they are deprecated);
    anything that configures a run must use the canonical name."""
    stale = []
    for rel in (".github/workflows/ci.yml", ".github/workflows/release.yml",
                "Makefile", "pyproject.toml"):
        text = (ROOT / rel).read_text()
        stale += [f"{rel}: {n}" for n in _OLD_NAMES if n in text]
    assert not stale, f"point these at PRIMER_TEST_POSTGRES_URL: {stale}"


def test_ci_has_a_postgres_lane_that_cannot_skip_silently():
    from tests.pg_gate import LANE_DIRS

    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = ci["jobs"]["postgres"]
    assert job["services"]["postgres"]["image"].startswith("pgvector/pgvector")
    assert job["env"]["PRIMER_TEST_POSTGRES_URL"].startswith("postgresql://")
    # The guard: without this the lane would go green while skipping.
    assert str(job["env"]["PRIMER_REQUIRE_POSTGRES_TESTS"]) == "1"
    assert job["timeout-minutes"] <= 30

    pytest_steps = [
        st for st in job["steps"] if "pytest" in st.get("run", "")
    ]
    # One pytest process per suite: the thread-method timeout kills the whole
    # process, so a hang in one suite must not blank the others.
    covered = []
    for st in pytest_steps:
        run = st["run"]
        dirs = [d for d in LANE_DIRS if d in run]
        assert len(dirs) == 1, (
            f"step {st['name']!r} runs {dirs}: each lane suite needs its own "
            "pytest process so one hang cannot erase the others' results"
        )
        covered += dirs
        # ...and one suite failing or hanging must not stop the next from running.
        assert "!cancelled()" in st.get("if", ""), st["name"]
        assert "--timeout=" in run and "--timeout-method=thread" in run, st["name"]
        # -v so the last node id before a timeout dump names the hung test.
        assert re.search(r"\s-v\b", run), st["name"]
    assert sorted(covered) == sorted(LANE_DIRS), (
        f"lane steps cover {sorted(covered)}, expected {sorted(LANE_DIRS)}"
    )


# Files where a port default is the BRINGUP's own contract, not a guess:
# the e2e helpers talk to the database scripts/e2e/bringup.sh provisioned,
# which publishes on ${PRIMER_DB_PORT:-5432}; tests/distributed builds its URL
# from a container it starts itself (suite switched off everywhere). Every entry
# matches at least one file (a test pins that, so a dead entry cannot linger).
_PORT_DEFAULT_ALLOWED = (
    "tests/e2e/",
    "tests/ui_e2e/test_approvals_journey.py",
    "tests/distributed/",
)

# The default SHAPES: `... or 5432` on a parsed URL, and a quoted "5432" as the
# default argument of an env lookup. A literal in a config payload that never
# dials out ({"port": 5432}) is a different thing and is not flagged.
_PORT_DEFAULT = re.compile(
    r"""\bor\s+5432\b|,\s*["']5432["']\s*\)|PRIMER_DB_PORT["']?\s*,"""
)


def _port_default_allowed(rel: str) -> bool:
    return any(
        rel == allowed or (allowed.endswith("/") and rel.startswith(allowed))
        for allowed in _PORT_DEFAULT_ALLOWED
    )


def _port_default_hits() -> dict[str, list[str]]:
    """Every test file that spells a port default, with the offending lines."""
    hits: dict[str, list[str]] = {}
    for path in sorted((ROOT / "tests").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _PORT_DEFAULT.search(line):
                hits.setdefault(rel, []).append(f"{rel}:{lineno}: {line.strip()}")
    return hits


def test_no_test_defaults_a_postgres_port():
    """A live-connect path must use the gate URL (which must name its port) or
    skip: 5432 on a developer host is often their own database, and the gated
    fixtures drop tables. tests/vector/test_halfvec_e2e.py used to default to
    PRIMER_DB_PORT / 5432 and connect, so every local sweep probed it."""
    offenders = [
        line for rel, lines in _port_default_hits().items()
        if not _port_default_allowed(rel) for line in lines
    ]
    assert not offenders, (
        "a test defaults a Postgres port; take it from tests.pg_gate "
        f"(explicit_port) or skip: {offenders}"
    )


def test_every_port_default_allowlist_entry_matches_a_file():
    """An allowlist entry that matches nothing is dead weight that would quietly
    excuse a future offender, so the list stays minimal."""
    hits = _port_default_hits()
    dead = [
        allowed for allowed in _PORT_DEFAULT_ALLOWED
        if not any(
            rel == allowed or (allowed.endswith("/") and rel.startswith(allowed))
            for rel in hits
        )
    ]
    assert not dead, f"allowlist entries that match no file: {dead}"
