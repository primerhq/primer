"""The operator's identity block (B1 of wiki-first M1): both branches render, neither degrades.

The block is a guarded Jinja section rendered per run by the existing
mechanism (``render_system_prompt_or_raw`` against the ``ExecutionContext``).
The renderer is StrictUndefined and falls back to the RAW joined prompt on
any render error, so an unguarded ``ctx.identity`` reference in a run whose
identity is None (a system or unattributed run) would degrade the whole
operator prompt to raw template text. Both branches are pinned here:

* with identity (a user initiator, admin role): the block states who
  (person id), the role, and what the admin role may do;
* without identity: the no-identity branch renders;
* neither branch leaves Jinja markers in the output (a degraded raw
  fallback would).

``ctx.identity`` is the session initiator's persisted projection
(``PrincipalRef``, sourced from the session's ``initiated_by``): the block
documents it as initiator-not-speaker; per-turn speaker tracking is an
extension, not M1.
"""

from __future__ import annotations

from primer.agent.prompt_render import render_system_prompt_or_raw
from primer.bootstrap.operator_defaults import OPERATOR_PROMPT
from primer.model.graph import build_execution_context
from primer.model.principal import PrincipalRef


def _operator_prompt() -> list[str]:
    return list(OPERATOR_PROMPT)


def test_the_identity_block_renders_the_person_and_the_admin_role():
    ctx = build_execution_context(
        surface="chat",
        session_id="s",
        identity=PrincipalRef(type="user", id="person-1", display="Ada", role="admin", source="local"),
    )
    rendered = render_system_prompt_or_raw(_operator_prompt(), ctx)
    assert "Ada (person id person-1), role admin" in rendered
    assert "An admin may configure the platform through you" in rendered
    assert "A normal user runs the normal workflow" not in rendered
    assert "No identity is recorded" not in rendered
    # a degraded raw fallback leaves the Jinja markers in the prompt
    assert "{%" not in rendered and "{{" not in rendered


def test_the_identity_block_renders_the_normal_user_role():
    ctx = build_execution_context(
        surface="chat",
        session_id="s",
        identity=PrincipalRef(type="user", id="person-2", display="Bo", role="user", source="local"),
    )
    rendered = render_system_prompt_or_raw(_operator_prompt(), ctx)
    assert "Bo (person id person-2), role user" in rendered
    assert "A normal user runs the normal workflow" in rendered
    assert "An admin may configure the platform through you" not in rendered
    assert "{%" not in rendered and "{{" not in rendered


def test_the_identity_block_renders_the_no_identity_branch():
    ctx = build_execution_context(surface="chat", session_id="s")  # identity=None
    rendered = render_system_prompt_or_raw(_operator_prompt(), ctx)
    assert "No identity is recorded for this run" in rendered
    assert "An admin may configure the platform through you" not in rendered
    assert "A normal user runs the normal workflow" not in rendered
    # the raw fallback is what this branch prevents: no template markers survive
    assert "{%" not in rendered and "{{" not in rendered


def test_the_identity_block_renders_a_non_user_initiator():
    """A trigger or token initiator is not a person: the block says so."""
    ctx = build_execution_context(
        surface="workspace",
        session_id="s",
        identity=PrincipalRef(type="trigger", id="trg-9", display="nightly", role=None, source="internal"),
    )
    rendered = render_system_prompt_or_raw(_operator_prompt(), ctx)
    assert "initiated by a trigger named nightly (id trg-9)" in rendered
    assert "No identity is recorded" not in rendered
    # a non-user initiator gets no role statement at all
    assert "A normal user runs the normal workflow" not in rendered
    assert "An admin may configure the platform through you" not in rendered
    assert "{%" not in rendered and "{{" not in rendered


def test_the_rest_of_the_operator_prompt_is_untouched_by_the_block():
    """The block adds its section; the rest of the prompt renders through."""
    ctx = build_execution_context(
        surface="chat",
        session_id="s",
        identity=PrincipalRef(type="user", id="person-1", display="Ada", role="admin", source="local"),
    )
    rendered = render_system_prompt_or_raw(_operator_prompt(), ctx)
    assert "You are the operator of this primer install" in rendered
    assert "purpose-map" in rendered
    assert "raw REST API" in rendered


def test_an_unguarded_identity_reference_degrades_to_the_raw_prompt():
    """The regression this block prevents: StrictUndefined miss -> raw fallback.

    An unguarded ``{{ ctx.identity.id }}`` in a no-identity run raises a
    render error; ``render_system_prompt_or_raw`` then returns the RAW joined
    prompt (Jinja markers intact) instead of the rendered text.
    """
    unguarded = ["{{ ctx.identity.id }} is prompting"]
    ctx = build_execution_context(surface="chat", session_id="s")  # identity=None
    rendered = render_system_prompt_or_raw(unguarded, ctx)
    assert rendered == unguarded[0]  # the raw fallback, unrendered
