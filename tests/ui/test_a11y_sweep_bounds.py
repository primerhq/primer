"""The standing a11y sweep is bounded from INSIDE, and it sees the populated toolbars (review of #668, round 4, B2 and N8).

``@pytest.mark.timeout(N, method="signal")`` turned a timeout into a 100% CPU hang under Playwright's sync API: the SIGALRM's ``Failed`` is raised inside the dispatcher greenlet, which dies, and every later
sync call spins; the sweep's own ``finally`` and the page fixture's teardown hang with it and no later journey runs. So the sweep carries a wall-clock deadline in its ``Budget``, counted from the start
of the test (past it no wait is handed out: ``wait_ms`` and ``check`` raise an ordinary exception, because Playwright treats ``timeout=0`` as no timeout at all, so the ``finally`` runs with a live
browser), and pytest-timeout stays only as the THREAD-method last resort above that deadline.

N8: the toolbar of the Semantic search page (a backend filter, a register menu) is drawn only when a provider exists; a fresh install has the reserved one, a scratch database may have none, so the sweep
seeds one of its own and the populated toolbar is always swept.
"""

from __future__ import annotations

import re
from pathlib import Path

SWEEP = (Path(__file__).resolve().parents[2] / "tests" / "ui_e2e" / "test_console_controls_have_names_sweep.py").read_text(encoding="utf-8")


def test_the_sweep_has_no_signal_timeout_and_a_thread_timeout_above_its_own_deadline() -> None:
    assert 'method="signal"' not in SWEEP and "method='signal'" not in SWEEP
    timeout = re.search(r'@pytest\.mark\.timeout\((\d+), method="thread"\)', SWEEP)
    assert timeout, "pytest-timeout is kept as the thread-method last resort"
    deadline = re.search(r"deadline_s=(\d+)", SWEEP)
    assert deadline, "the sweep's Budget carries a wall-clock deadline"
    assert int(deadline.group(1)) + 120 <= int(timeout.group(1)), "the last resort is above the deadline, with room for the sweep's own finally"


def test_the_sweep_stops_between_surfaces_with_its_cleanup_and_report_still_to_run() -> None:
    """The deadline is an ordinary exception: the test catches it with the other sweep errors and judges what was found."""
    assert "Budget(" in SWEEP and "Sweep(page, budget=" in SWEEP
    assert "except Exception as exc:  # noqa: BLE001 - reported below" in SWEEP


def test_the_sweep_seeds_a_semantic_search_provider_of_its_own() -> None:
    assert re.search(r'c\.post\("/v1/ssp", json=\{"id": ssp_id, "provider": "lance"', SWEEP)
    assert 'made.append(f"/v1/ssp/{ssp_id}")' in SWEEP


UI_E2E = Path(__file__).resolve().parents[2] / "tests" / "ui_e2e"
SIGNAL_TIMEOUT = re.compile(r"""\bmethod\s*=\s*['"]signal['"]|\btimeout\s*\([^)]*['"]signal['"]""")      # a signal timeout, as pytest-timeout is asked for it (keyword or positional)


def test_no_ui_e2e_file_uses_a_signal_timeout() -> None:
    """Round 5, N7: the hang is the lane's, not the sweep's: any journey under sync Playwright that asks pytest-timeout for ``method="signal"`` can hang the same way."""
    offenders = [path.name for path in sorted(UI_E2E.glob("*.py")) if SIGNAL_TIMEOUT.search(path.read_text(encoding="utf-8"))]
    assert offenders == [], offenders


def test_the_deadline_starts_with_the_test_not_with_its_first_wait() -> None:
    """N3: the seeds and the first page loads took time the 900 s thread timeout counts, and the deadline did not. ``check`` starts the clock, so it is the first thing the test does."""
    body = SWEEP[SWEEP.index("def test_no_visible_control"):]
    assert body.index('sweep.budget.check("seeding")') < body.index("seed_session("), "the clock must start before the seeds"


def test_every_navigation_of_the_sweep_is_bounded_by_its_budget() -> None:
    """N3: ``open_legacy_route`` alone waits 45 s twice; a stuck install multiplied that by 100 pages. Each helper that waits is handed what the budget has left."""
    calls = re.findall(r"\b(open_shell|open_overlay|open_legacy_route|open_view|open_session_in_studio)\(([^()]*(?:\([^()]*\)[^()]*)*)\)", SWEEP)
    assert calls, "the sweep opens its surfaces through these helpers"
    unbudgeted = [name for name, args in calls if "timeout=sweep.budget.wait_ms(" not in args]
    assert unbudgeted == [], unbudgeted


def test_no_page_is_excused_from_its_register_menu_any_more() -> None:
    """Round 6: the dead-menu check existed for the artifact storage page only, and went with the exemption once the page served its kinds."""
    assert "_assert_register_menu_is_dead" not in SWEEP and "DEAD_REGISTER_MENUS" not in SWEEP and "DEAD_MENU_TEXT" not in SWEEP


def test_no_wait_of_the_sweep_has_a_fixed_timeout() -> None:
    """Round 6, N3/N4: every click, ``expect`` and ``goto`` of the sweep takes what the budget has left (``timeout=sweep.budget.wait_ms(...)``); a fixed number is a wait the deadline does not cap. (The httpx
    clients' ``timeout=30.0`` are floats and are the seeds' own.)"""
    fixed = re.findall(r"\btimeout=\d+(?![\d.])", SWEEP)
    assert fixed == [], fixed


def test_a_page_that_cannot_be_opened_is_a_note_for_every_navigation_of_the_sweep_that_can_time_out() -> None:
    """N2/N4: the legacy pages and their forms (``_sweep_page``, ``_sweep_forms``), the three create overlays, the graph builder and the phone are opened through ``Sweep.reach``, so one that never
    comes up is a note and an empty look, and the sweep goes on."""
    assert 'sweep.reach(f"{kind} {name}", reopen)' in SWEEP, "_sweep_page"
    assert "sweep.reach(surface, reopen)" in SWEEP, "_sweep_forms"
    assert 'sweep.reach(f"overlay {name}"' in SWEEP
    assert 'sweep.reach("graph builder"' in SWEEP
    assert 'sweep.reach(f"phone / ' in SWEEP


def test_the_signal_timeout_pin_reads_the_positional_form_too() -> None:
    """N5: ``timeout(900, 'signal')`` is the same hang as ``timeout(900, method="signal")``."""
    for source in ('@pytest.mark.timeout(900, method="signal")', "@pytest.mark.timeout(900, method='signal')", "@pytest.mark.timeout(900, 'signal')", '@pytest.mark.timeout(900, "signal")', 'pytest.mark.timeout(method = "signal")'):
        assert SIGNAL_TIMEOUT.search(source), source
    for source in ('@pytest.mark.timeout(900, method="thread")', "# a signal arrives", 'assert "signal" in text'):
        assert not SIGNAL_TIMEOUT.search(source), source
