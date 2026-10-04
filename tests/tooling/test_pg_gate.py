"""tests/pg_gate.py: the single Postgres test gate and its anti-skip contract."""

from __future__ import annotations

import warnings

import pytest

from tests.pg_gate import (
    CANONICAL_ENV,
    DEPRECATED_ALIASES,
    GATE_REASON_PREFIX,
    REQUIRE_ENV,
    postgres_marks,
    postgres_url,
    require_postgres_url,
)

URL = "postgresql://primer:primer@localhost:5432/primer_test"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (CANONICAL_ENV, *DEPRECATED_ALIASES, REQUIRE_ENV):
        monkeypatch.delenv(name, raising=False)


def test_gate_is_closed_when_nothing_is_set():
    assert postgres_url() is None


def test_canonical_name_opens_the_gate(monkeypatch):
    monkeypatch.setenv(CANONICAL_ENV, URL)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # the canonical name must not warn
        assert postgres_url() == URL


@pytest.mark.parametrize("alias", DEPRECATED_ALIASES)
def test_deprecated_alias_still_opens_the_gate_but_warns(monkeypatch, alias):
    """Silently ignoring a name a developer already exports would turn their
    gated tests into silent skips, the defect this helper exists to end."""
    monkeypatch.setenv(alias, URL)
    with pytest.warns(DeprecationWarning, match=CANONICAL_ENV):
        assert postgres_url() == URL


def test_alias_agreeing_with_canonical_is_fine(monkeypatch):
    monkeypatch.setenv(CANONICAL_ENV, URL)
    monkeypatch.setenv(DEPRECATED_ALIASES[0], URL)
    assert postgres_url() == URL


def test_alias_disagreeing_with_canonical_raises(monkeypatch):
    monkeypatch.setenv(CANONICAL_ENV, URL)
    monkeypatch.setenv(DEPRECATED_ALIASES[0], URL + "x")
    with pytest.raises(RuntimeError, match="conflicting"):
        postgres_url()


def test_two_aliases_disagreeing_raise(monkeypatch):
    monkeypatch.setenv(DEPRECATED_ALIASES[0], URL)
    monkeypatch.setenv(DEPRECATED_ALIASES[1], URL + "x")
    with pytest.raises(RuntimeError, match="conflicting"):
        postgres_url()


def test_require_url_skips_with_the_gate_reason_when_closed():
    with pytest.raises(pytest.skip.Exception) as exc:
        require_postgres_url("x")
    assert GATE_REASON_PREFIX in str(exc.value)


def test_require_url_FAILS_instead_of_skipping_in_a_required_lane(monkeypatch):
    """A fixture must not be able to quietly turn a required lane into a no-op."""
    monkeypatch.setenv(REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception, match="refusing to skip"):
        require_postgres_url("x")


def test_require_url_returns_the_url_when_open(monkeypatch):
    monkeypatch.setenv(CANONICAL_ENV, URL)
    assert require_postgres_url() == URL


def test_marks_carry_the_postgres_marker_and_a_gate_skipif():
    marks = postgres_marks("x")
    assert [m.name for m in marks] == ["postgres", "skipif"]
    assert marks[1].kwargs["reason"].startswith(GATE_REASON_PREFIX)


E2E_URL = "postgresql://primer:primer@127.0.0.1:5432/primer_e2e"


def test_gate_refuses_the_e2e_servers_own_database(monkeypatch):
    """The gated fixtures DROP tables and DELETE leases in whatever schema
    they are given. primer_e2e's public schema belongs to a live e2e server."""
    monkeypatch.setenv(CANONICAL_ENV, E2E_URL)
    with pytest.raises(RuntimeError, match="primer_e2e"):
        postgres_url()


@pytest.mark.parametrize("alias", DEPRECATED_ALIASES)
def test_an_old_alias_exported_for_the_e2e_server_cannot_open_the_gate(
    monkeypatch, alias,
):
    """PRIMER_TEST_PG_DSN used to be the e2e capability's variable, pointed at
    primer_e2e. Resolving it as a gate alias must not turn that into
    permission to wipe the running server."""
    monkeypatch.setenv(alias, E2E_URL)
    with pytest.raises(RuntimeError, match=alias):
        postgres_url()


def test_e2e_database_is_fine_with_a_private_schema(monkeypatch):
    monkeypatch.setenv(CANONICAL_ENV, E2E_URL + "?schema=t_private")
    assert postgres_url() == E2E_URL + "?schema=t_private"


def test_a_throwaway_database_is_not_refused(monkeypatch):
    monkeypatch.setenv(CANONICAL_ENV, URL)
    assert postgres_url() == URL
