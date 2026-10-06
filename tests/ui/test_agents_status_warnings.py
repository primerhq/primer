"""The agent page shows the status endpoint's ``warnings`` (01a10c6b item 2).

``GET /v1/agents/{id}/status`` carries a ``warnings`` list next to ``issues``: things that may work but probably do not
(an output cap that fills the model's window). The status panel lists them under the verdict, in the warning colour, and
they are never part of the verdict itself: ``ok`` is the server's, and the panel's headline still counts ``issues`` only.
Static-source checks plus the bundle build, the convention for the agents page (``test_agents_model_profile_field.py``).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS = (ROOT / "ui" / "components" / "agents.jsx").read_text(encoding="utf-8")


def _status_panel() -> str:
    start = AGENTS.index("function AG_StatusPanel(")
    return AGENTS[start:AGENTS.index("\n}\n", start) + 3]


def test_the_status_panel_lists_the_servers_warnings_under_the_verdict() -> None:
    panel = _status_panel()

    assert "status.data?.warnings" in panel
    assert 'data-testid="ag-status-warnings"' in panel
    assert "var(--amber)" in panel


def test_the_headline_still_counts_issues_only() -> None:
    panel = _status_panel()

    assert "issues.length} issue" in panel, "the headline wording changed"
    assert "warnings.length} issue" not in panel
    assert "const ok = status.data?.ok === true;" in panel, "ok must stay the server's verdict"


def test_the_bundle_still_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(ROOT / "ui")

    assert etag and b"ag-status-warnings" in body
