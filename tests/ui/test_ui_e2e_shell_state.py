"""A ui_e2e failure to open the provider catalog explains itself (ticket 01a11b72, the lead's ruling: instrument it, no retry).

``tests/ui_e2e/test_provider_catalog_journey.py`` failed about one run in three on a long-lived scratch SQLite instance with
``nv-overlay-body`` never visible after ``open_provider_catalog`` assigned the URL hash, and not once in ~140 attempts on a fresh one (a delay sweep after
DOMContentLoaded, a sweep after ``nv-root`` first attached with a log of every ``history`` write, and 66 real runs, some under CPU load). The only way to see
the same symptom was an instance whose setup wizard was showing instead of the shell, which is a different thing. The cause is NOT known.

The ruling is to record what the page looked like instead of guessing, and NOT to re-assign the hash on a miss: a retry would turn a real "the first hash does
not open the overlay" bug into a pass. So:

* the helper waits for ``nv-root`` before it assigns the hash (a real precondition: the shell listens for ``hashchange`` only once it has mounted);
* a miss raises an ``AssertionError`` that carries ``shell_state(page)``: the url, the hash, how many ``nv-root`` / ``nv-overlay-body`` / setup-wizard elements the
  page has, so the next occurrence says which of "the shell never mounted", "the setup wizard was shown" and "the hash was ignored" it was.

``shell_state`` is pure over a page-like object, so it runs here with a stand-in; the helper's order (nv-root first, no second assignment) is a source check, like
the other ui_e2e helpers, because ``expect`` needs a real Playwright locator.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("playwright")

ROOT = Path(__file__).resolve().parents[2]
STUDIO = (ROOT / "tests" / "ui_e2e" / "_studio_helpers.py").read_text(encoding="utf-8")


class _Locator:
    def __init__(self, n: int) -> None:
        self._n = n

    def count(self) -> int:
        return self._n


class _Page:
    """The few members ``shell_state`` reads."""

    def __init__(self, url: str, hash_: str, counts: dict[str, int], wizard: int = 0, fail_evaluate: bool = False) -> None:
        self.url = url
        self._hash = hash_
        self._counts = counts
        self._wizard = wizard
        self._fail = fail_evaluate

    def evaluate(self, script: str):
        if self._fail:
            raise RuntimeError("Target page, context or browser has been closed")
        return self._hash

    def get_by_test_id(self, test_id: str) -> _Locator:
        return _Locator(self._counts.get(test_id, 0))

    def get_by_text(self, text: str) -> _Locator:
        return _Locator(self._wizard if text == "Configure this install" else 0)


def _state(page) -> str:
    from tests.ui_e2e._shell_helpers import shell_state

    return shell_state(page)


def test_the_state_names_the_url_the_hash_and_what_is_mounted() -> None:
    text = _state(_Page("http://127.0.0.1:8798/console/#/w/primer?overlay=providers", "#/w/primer?overlay=providers", {"nv-root": 1}))

    assert "url='http://127.0.0.1:8798/console/#/w/primer?overlay=providers'" in text
    assert "hash='#/w/primer?overlay=providers'" in text
    assert "nv-root=1" in text and "nv-overlay-body=0" in text and "setup-wizard=0" in text


def test_the_setup_wizard_being_shown_instead_of_the_shell_is_visible_in_the_state() -> None:
    text = _state(_Page("http://127.0.0.1:8798/console/", "", {}, wizard=1))

    assert "nv-root=0" in text and "setup-wizard=1" in text


def test_a_hash_that_was_lost_is_visible_in_the_state() -> None:
    """The case the retry would have hidden: the shell is mounted, the overlay is not, and the hash is no longer the one that was assigned."""
    text = _state(_Page("http://127.0.0.1:8798/console/#/w/primer", "#/w/primer", {"nv-root": 1}))

    assert "nv-root=1" in text and "nv-overlay-body=0" in text and "hash='#/w/primer'" in text


def test_the_state_never_raises_even_when_the_page_is_gone() -> None:
    """It runs while a test is already failing; a second exception would bury the first."""
    text = _state(_Page("about:blank", "", {}, fail_evaluate=True))

    assert "hash=<unreadable" in text and "url='about:blank'" in text


# ---- the helper ------------------------------------------------------------------------------------------------------------------------------------------


def _open_provider_catalog_source() -> str:
    start = STUDIO.index("def open_provider_catalog(")
    end = STUDIO.index("\ndef ", start + 1) if "\ndef " in STUDIO[start + 1:] else len(STUDIO)
    return STUDIO[start:end]


def test_the_catalog_helper_waits_for_the_shell_before_it_assigns_the_hash() -> None:
    src = _open_provider_catalog_source()

    wait = src.index('get_by_test_id("nv-root")')
    assign = src.index("window.location.hash = h;")
    assert wait < assign, "the hash is assigned before the shell is known to be mounted"


def test_the_catalog_helper_assigns_the_hash_once_and_does_not_retry() -> None:
    src = _open_provider_catalog_source()

    assert src.count("window.location.hash = h;") == 1, "a second assignment is a retry: it would hide a real 'first hash is ignored' bug"
    assert not re.search(r"\bfor attempt\b|\bretry\b|\bretries\b", src.replace("no retry", "").replace("not retry", ""), re.I), "the helper must not retry"


def test_a_miss_carries_the_shell_state() -> None:
    src = _open_provider_catalog_source()

    assert "shell_state(page)" in src and "raise AssertionError(" in src, "a miss must raise with the state of the page"
