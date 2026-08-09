"""Static checks for the Studio2 agent entity document (plan task 10)."""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_agent_kind_and_crud() -> None:
    src = _read("components/studio2/s2-doc-agent.jsx")
    assert 'registerKind("agent"' in src
    for needle in ['"/agents', '"/model_profiles', '"PUT"', '"POST"']:
        assert needle in src


def test_dirty_and_save_command() -> None:
    src = _read("components/studio2/s2-doc-agent.jsx")
    assert "setDirty" in src
    assert '"doc:save"' in src
    assert "s2-kbd" in src  # the shortcut renders as a kbd chip


def test_managed_banner_and_disabled_fields() -> None:
    src = _read("components/studio2/s2-doc-agent.jsx")
    assert "harness_id" in src and "managed" in src
    assert "disabled={managed}" in src


def test_new_agent_command_and_draft_flow() -> None:
    src = _read("components/studio2/s2-doc-agent.jsx")
    assert '"new:agent"' in src and "__new__" in src


def test_422_field_mapping() -> None:
    src = _read("components/studio2/s2-doc-agent.jsx")
    assert "envelope" in src and "loc" in src
    assert "s2-field-error" in src


def test_matches_mains_agent_shape() -> None:
    # Post-cutover shape: the agent references one model.profile_id.
    src = _read("components/studio2/s2-doc-agent.jsx")
    assert "profile_id" in src
    assert "provider_id" not in src  # the old pair must not linger
    assert "system_prompt" in src
