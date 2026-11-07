"""The workspace detail header does not advertise unimplemented lifecycle controls."""

from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[2] / "ui" / "components" / "workspaces.jsx"
).read_text(encoding="utf-8")


def test_workspace_detail_omits_pause_resume_placeholders() -> None:
    assert 'data-testid="ws-share"' in SOURCE
    assert ">Run diagnostic</Btn>" in SOURCE
    assert "Workspace pause is reserved" not in SOURCE
    assert "Workspace resume is reserved" not in SOURCE
    assert ">Pause</Btn>" not in SOURCE
    assert ">Resume</Btn>" not in SOURCE
