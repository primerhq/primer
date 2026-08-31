"""A session's kind comes from its binding, never from a legacy sentinel.

Before S1 the console guessed a session's kind by sniffing a "graph:"
prefix on ``agent_id``. A session switched graph -> agent keeps that
prefix on disk, so the sniff misclassified it forever. S1 made
``SessionInfo.binding`` authoritative and the shell reads it directly:
there is no sentinel left to lose a race with.

The Studio-era companions of this test (the autonomy mirror and the
autonomous/interactive panel switch) went with the studio panels in S8:
the shell renders one session document for every binding kind, so there
is no panel to route to.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_the_binding_is_what_the_chip_reads() -> None:
    src = _read("components/console/nv-session-doc.jsx")
    body = src.split("function NV_BindingChip(")[1].split("\n}")[0]
    assert 'binding.kind === "graph"' in body
    assert "binding.graph_id" in body
    assert "binding.agent_id" in body


def test_no_legacy_prefix_sniff_survives_in_the_shell() -> None:
    """The sentinel is what the binding replaced; reading it again would
    reintroduce the misclassification S1 fixed."""
    for p in sorted((UI / "components" / "shell").glob("*.jsx")):
        assert 'indexOf("graph:")' not in p.read_text(encoding="utf-8"), (
            f"{p.name} sniffs the retired agent_id sentinel"
        )
