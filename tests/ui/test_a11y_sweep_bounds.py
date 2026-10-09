"""The standing a11y sweep is bounded from INSIDE, and it sees the populated toolbars (review of #668, round 4, B2 and N8).

``@pytest.mark.timeout(N, method="signal")`` turned a timeout into a 100% CPU hang under Playwright's sync API: the SIGALRM's ``Failed`` is raised inside the dispatcher greenlet, which dies, and every later
sync call spins; the sweep's own ``finally`` and the page fixture's teardown hang with it and no later journey runs. So the sweep carries a wall-clock deadline in its ``Budget`` (past it every wait is 0 and
the next surface raises an ordinary exception, so the ``finally`` runs with a live browser), and pytest-timeout stays only as the THREAD-method last resort above that deadline.

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
