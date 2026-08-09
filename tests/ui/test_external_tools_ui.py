"""Statics for the external-tools console surfaces."""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_banner_component_defined_and_bundled() -> None:
    src = _read("components/external-tools.jsx")
    assert "window.ExternalPendingBanner" in src
    assert "external_tools/pending" in src
    assert "yields/" in src  # session-side cancel wiring
    assert 'src="components/external-tools.jsx"' in _read("index.html")


def test_banner_mounted_on_session_and_chat_detail() -> None:
    assert "ExternalPendingBanner" in _read("components/studio-center.jsx")
    assert "ExternalPendingBanner" in _read("components/chats.jsx")


def test_agents_editor_has_flag_toggle() -> None:
    src = _read("components/agents.jsx")
    assert "allow_external_tools" in src
    assert "allowExternalTools" in src
    assert "na-allow-external-tools" in src


def test_s2_agent_doc_has_flag_field() -> None:
    src = _read("components/studio2/s2-doc-agent.jsx")
    assert "allow_external_tools" in src
    assert "s2-agent-allow-external-tools" in src
