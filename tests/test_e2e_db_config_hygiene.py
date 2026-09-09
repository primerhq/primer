"""Guard against tests/e2e/*.py re-introducing hardcoded pgvector DB config.

2026-09-09: 14 tests/e2e/*.py modules each independently hardcoded
hostname=localhost/port=5432/database=primer_e2e/username=primer/
password=primer for their SemanticSearchProvider POST body, with no
PRIMER_DB_PORT override anywhere - silently wrong on any host where 5432
isn't this bringup's own postgres, which is exactly the case on the
primary dev host (5432 there is an unrelated pre-existing service). One
occurrence additionally named the wrong database entirely
("primer_dogfood", which no bringup script creates). All 14 are now
migrated onto tests/e2e/conftest.py's pgvector_ssp_config()/
pgvector_ssp_body().

This test is the guard against the class quietly returning: four
independent hardcoded-DB-config recurrences surfaced across this repo in
one day (this class, plus tests/ui_e2e/test_approvals_journey.py's DB
helper and tests/vector/test_halfvec_e2e.py, both fixed separately) -
fixing occurrences one at a time was losing to the rate at which they
reappear, so this closes the door specifically for tests/e2e.

Deliberately scoped to tests/e2e ONLY - do NOT widen this to tests/
broadly. The same 2026-09-09 sweep found plenty of LEGITIMATE literal-5432
and hardcoded-credential occurrences elsewhere (tests/api's `_FakeStorage
Provider`-backed CRUD round-trips that never dial postgres, tests/model's
deliberately-unreachable "db.invalid" coercion test, tests/knowledge's
SQLite-backed fixture) that a repo-wide rule would wrongly flag as
hazards. tests/e2e specifically is the one directory where every test
runs against a REAL, bringup.sh-provisioned postgres, which is exactly
why a hardcoded literal there is never inert.

Lives outside tests/e2e/ itself (not tests/e2e/test_*.py) so it is not
caught by that directory's own collect_ignore_glob, which skips every
test_*.py module there unless PRIMER_RUN_E2E=1 is set - this is pure
source-text scanning and needs neither a live server nor that flag.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
E2E_DIR = ROOT / "tests" / "e2e"

# conftest.py is the shared helper itself - it legitimately contains the
# literal defaults (PRIMER_DB_PORT's own "5432" fallback, "primer_e2e" as
# the database pgvector_ssp_config() defaults to).
_ALLOWLISTED_FILES = {"conftest.py"}

# The hazard shape: a PORT KEY assigned the literal 5432 directly, not an
# env-var-driven resolution. Does NOT match
# `os.environ.get("PRIMER_DB_PORT", "5432")` - there "5432" is a quoted
# string default argument, never adjacent to a `port`/`"port"` key.
_PORT_LITERAL = re.compile(r'"port"\s*:\s*5432\b|(?<!_)\bport\s*=\s*5432\b')

# "primer_e2e" itself is NOT flagged: it's the one hardcoded value that IS
# correct everywhere, because bringup.sh always creates a database by
# that literal name and never makes it configurable - the clean
# PRIMER_DB_PORT-only files (e.g. test_multi_cycle_resume_stability_
# journey.py's own _pg()) hardcode it too, deliberately. "primer_dogfood"
# is different: no bringup script anywhere creates a database by that
# name, so its only prior appearance (test_builtin_toolsets.py) was a
# second, independent bug layered on top of the port one - a hardcoded
# database name that isn't even the RIGHT one.
_HARDCODED_DB_NAME = re.compile(r'"primer_dogfood"')


def test_no_hardcoded_pgvector_config_outside_the_shared_helper() -> None:
    offenders = []
    for path in sorted(E2E_DIR.glob("*.py")):
        if path.name in _ALLOWLISTED_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        hit_port = _PORT_LITERAL.search(text)
        hit_db = _HARDCODED_DB_NAME.search(text)
        if hit_port or hit_db:
            reason = "port=5432 literal" if hit_port else "hardcoded database name"
            offenders.append(f"{path.name} ({reason})")
    assert not offenders, (
        "tests/e2e/*.py file(s) hardcode pgvector DB config instead of "
        "using tests/e2e/conftest.py's pgvector_ssp_body()/"
        "pgvector_ssp_config(): "
        f"{offenders}. Import the shared helper instead - see "
        "conftest.py's own docstring for why a literal here is a "
        "silent-wrong-database hazard, not just a portability nit."
    )
