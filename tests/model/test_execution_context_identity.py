"""ctx.identity projection on ExecutionContext (Layer 3, §8.4)."""

from __future__ import annotations

from primer.agent.prompt_render import render_system_prompt
from primer.model.graph import build_execution_context
from primer.model.principal import PrincipalRef


def test_identity_defaults_none() -> None:
    ctx = build_execution_context(surface="chat")
    assert ctx.identity is None


def test_identity_populated_and_frozen() -> None:
    ref = PrincipalRef(
        type="user", id="user-1", display="alice", role="admin", source="local",
    )
    ctx = build_execution_context(surface="workspace", session_id="s", identity=ref)
    assert ctx.identity is ref
    assert ctx.identity.display == "alice"


def test_identity_renders_in_system_prompt() -> None:
    ref = PrincipalRef(
        type="trigger", id="t-1", display="nightly", role=None, source="internal",
    )
    ctx = build_execution_context(surface="workspace", session_id="s", identity=ref)
    out = render_system_prompt(
        ["Actor: {{ ctx.identity.type }}/{{ ctx.identity.display }}"], ctx
    )
    assert out == "Actor: trigger/nightly"
