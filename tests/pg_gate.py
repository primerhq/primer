"""The ONE place the Postgres test gate is read.

The live-Postgres suites (claim engine, scheduler, storage, coordinator)
need a real database and are gated on an env var. There used to be three
independent names for it (PRIMER_TEST_POSTGRES_URL, PRIMER_TEST_PG_DSN,
PRIMER_PG_TEST_DSN) read in ~20 places, and no CI job set any of them, so
those suites were skipped everywhere and a broken claim engine, a missing
purge_dead_workers coverage and a hanging LISTEN teardown all went unseen.

Canonical name: ``PRIMER_TEST_POSTGRES_URL``, a libpq-style URL
(``postgresql://user:pw@host:5432/db[?schema=name]``).

The two old names still work as DEPRECATED ALIASES, resolved here and nowhere
else, because silently ignoring a name a developer already exports would turn
their gated tests into silent skips, the defect this module exists to end. An
alias warns; an alias that disagrees with the canonical name raises.

Anti-silent-skip: with ``PRIMER_REQUIRE_POSTGRES_TESTS=1`` (set by the CI
Postgres lane) a Postgres-gated test that SKIPS fails instead, and so does a
run in which no gated test passed at all. See ``tests/conftest.py``.
"""

from __future__ import annotations

import os
import warnings

import pytest

CANONICAL_ENV = "PRIMER_TEST_POSTGRES_URL"
DEPRECATED_ALIASES = ("PRIMER_TEST_PG_DSN", "PRIMER_PG_TEST_DSN")
REQUIRE_ENV = "PRIMER_REQUIRE_POSTGRES_TESTS"

# Every skip this module produces starts with this, so the guard can tell a
# Postgres-gate skip from any other skip without parsing free text.
GATE_REASON_PREFIX = "postgres-gate:"

# The suites the CI Postgres lane runs (one pytest process each). The guard in
# tests/conftest.py treats a collection-time skip anywhere under these as a
# failure, and the lane workflow runs exactly these.
LANE_DIRS = (
    "tests/claim",
    "tests/scheduler",
    "tests/storage",
    "tests/coordinator",
    "tests/vector",
)

# The e2e server's own database. Its bringup script creates it and a live
# server uses its public schema. The gated fixtures are destructive (they DROP
# and DELETE from the tables of whatever schema they are pointed at), so the
# gate refuses to open on it: an old alias exported for the e2e capability
# (PRIMER_TEST_PG_DSN used to be exactly that) must not silently become
# permission to wipe a running server.
_SHARED_E2E_DATABASE = "primer_e2e"


def _refuse_shared_database(url: str, source: str) -> None:
    from urllib.parse import parse_qs, urlparse

    parts = urlparse(url)
    database = (parts.path or "").lstrip("/")
    schema = parse_qs(parts.query).get("schema", ["public"])[0]
    if database == _SHARED_E2E_DATABASE and schema == "public":
        raise RuntimeError(
            f"{source} points the Postgres test gate at {_SHARED_E2E_DATABASE!r} "
            "(public schema), the e2e server's own database: the gated "
            "fixtures DROP tables and DELETE leases there. Use a throwaway "
            "database, or add ?schema=<name> for a private schema. If this "
            "variable is meant for the e2e server's Postgres capability, "
            "rename it (tests/testconfig.example.yaml uses "
            "PRIMER_TEST_E2E_POSTGRES_DSN)."
        )


def postgres_url() -> str | None:
    """The configured test database URL, or None when the gate is closed."""
    url = _resolve_postgres_url()
    return url


def _resolve_postgres_url() -> str | None:
    canonical = os.environ.get(CANONICAL_ENV) or None
    aliases = {
        name: os.environ[name]
        for name in DEPRECATED_ALIASES
        if os.environ.get(name)
    }
    values = set(aliases.values()) | ({canonical} if canonical else set())
    if len(values) > 1:
        raise RuntimeError(
            f"conflicting Postgres test URLs: {CANONICAL_ENV} and the "
            f"deprecated alias(es) {sorted(aliases)} are set to different "
            f"values; set only {CANONICAL_ENV}"
        )
    if canonical:
        _refuse_shared_database(canonical, CANONICAL_ENV)
        return canonical
    if aliases:
        name = next(iter(aliases))
        _refuse_shared_database(aliases[name], name)
        warnings.warn(
            f"{name} is a deprecated alias; set {CANONICAL_ENV} instead",
            DeprecationWarning,
            stacklevel=3,
        )
        return aliases[name]
    return None


def postgres_required() -> bool:
    """True in a lane that must run the Postgres tests (never skip them)."""
    return os.environ.get(REQUIRE_ENV) == "1"


def gate_reason(what: str = "the live Postgres tests") -> str:
    return f"{GATE_REASON_PREFIX} set {CANONICAL_ENV} to run {what}"


def require_postgres_url(what: str = "the live Postgres tests") -> str:
    """For fixtures: the URL, or skip - or FAIL in a lane that requires them.

    Failing rather than skipping when ``PRIMER_REQUIRE_POSTGRES_TESTS=1``
    means a fixture cannot quietly turn a required lane into a no-op.
    """
    url = postgres_url()
    if url is None:
        if postgres_required():
            pytest.fail(
                f"{REQUIRE_ENV}=1 but {CANONICAL_ENV} is not set: refusing "
                f"to skip {what}",
                pytrace=False,
            )
        pytest.skip(gate_reason(what))
    return url


def postgres_marks(what: str = "the live Postgres tests") -> list:
    """Marks for a gated module (``pytestmark = postgres_marks(...)``) or a
    gated parameter (``pytest.param(..., marks=postgres_marks())``)."""
    return [
        pytest.mark.postgres,
        pytest.mark.skipif(postgres_url() is None, reason=gate_reason(what)),
    ]


def needs_postgres(what: str = "the live Postgres tests"):
    """Decorator form of :func:`postgres_marks`, for a single test/class."""
    marks = postgres_marks(what)

    def decorate(obj):
        for mark in marks:
            obj = mark(obj)
        return obj

    return decorate
