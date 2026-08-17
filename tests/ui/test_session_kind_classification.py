"""Session kind and autonomy classification in the studio (S1 P5 T30).

Static-source checks, matching the tests/ui convention.

Once a binding is mutable, agent_id stops being a reliable classifier:
it can still carry the historical "graph:<id>" slot sentinel from how a
session STARTED. The row's binding is sole truth and must be consulted
first.
"""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_kind_reads_the_binding_before_the_legacy_sentinel() -> None:
    """A session switched graph -> agent keeps a "graph:" agent_id on
    disk; reading that first would misclassify it forever."""
    src = _read("components/studio-sidebar.jsx")
    # The function body ends at the first line that is exactly "}".
    after = src.split("function ST_sessionKind(")[1]
    body = after.split("\n}")[0]
    binding_at = body.index("session.binding.kind")
    sentinel_at = body.index('indexOf("graph:")')
    assert binding_at < sentinel_at, (
        "binding.kind must be checked before the agent_id sentinel"
    )


def test_the_sentinel_survives_only_as_a_fallback() -> None:
    """Rows served before SessionInfo.binding existed still classify."""
    src = _read("components/studio-sidebar.jsx")
    assert 'indexOf("graph:")' in src


def test_autonomy_mirrors_the_backend_precedence() -> None:
    """ST_isAutonomous is a byte-mirror of session_is_autonomous: an
    explicit flag wins, else derive from the binding kind.

    Branching on raw binding.kind instead would send an
    explicit-override session (an agent-bound self-driving loop) to the
    graph panel, which is why the override is deliberate rather than a
    contradiction to delete.
    """
    src = _read("components/studio-center.jsx")
    body = src.split("function ST_isAutonomous(")[1].split("\n}")[0]
    assert "session.autonomous != null" in body
    assert 'kind === "graph"' in body
    explicit_at = body.index("session.autonomous != null")
    derive_at = body.index('kind === "graph"')
    assert explicit_at < derive_at, "the explicit flag must win"


def test_panel_routing_goes_through_the_mirror() -> None:
    src = _read("components/studio-center.jsx")
    assert "if (ST_isAutonomous(session)) {" in src
