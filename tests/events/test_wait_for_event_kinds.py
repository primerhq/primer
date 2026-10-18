"""What a run whose principal is not an admin may ask ``wait_for_event`` for (pivot P1, the lead's rulings on #764).

``system__wait_for_event`` is role user. A non-admin run is held to three limits, each refused with ``type=forbidden`` before anything is
stored; an admin, and the system principal, keep all three. A context that names no initiator ranks as an ordinary user, as for ``call_tool``.

* The kinds: the event that wakes the wait becomes the run's tool result, so it may wait only on what ``GET /v1/events`` shows a non-admin:
  the lifecycle kinds of ``primer.events.visibility``, the feed's own rule (one copy, read by both). A glob that could match another kind is
  refused too.
* No ``expr``: the dispatcher evaluates the rego module server-side, where ``opa.runtime().env`` reads the process environment (secrets
  included); whether the wait wakes or times out would leak it one bit at a time.
* No ``regex`` field matcher: patterns run in the shared, leader-elected dispatcher loop, where a catastrophic one stalls everyone's
  subscriptions. ``eq`` and ``prefix`` stay open.
"""

from __future__ import annotations

import json

import pytest

from primer.api.routers import events as events_route
from primer.events import visibility
from primer.events.catalog import ACTION_EVENT_TYPES
from primer.model.event import EventSubscription
from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.yield_ import ToolContext, Yielded
from primer.toolset.events_wait import make_wait_for_event_handler

# Re-export: the real (sqlite) storage the neighbouring wait_for_event tests use.
from tests.events.test_wait_for_event_tool import sp  # noqa: F401

ADMIN = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")
USER = PrincipalRef(type="user", id="u-1", display="user", role="user", source="local")

# Every way of naming what the feed shows a non-admin: an exact lifecycle type, a prefix glob, the four exact kinds. The canvas events are
# visible like the session events (pivot P5, the lead's ruling after S2: the canvas is part of every user's chat in the shared install).
LIFECYCLE = [
    "session.ended", "session.*", "session.?arked", "workspace.file_changed", "workspace.*",
    "canvas.updated", "canvas.event", "canvas.*", "canvas.e?ent",
    "tool.called", "turn.started", "trigger.fired", "llm.called",
]
# Kinds the feed hides from a non-admin, and globs that could reach one even when part of what they match is shown.
HIDDEN = [
    "collection.document_pushed", "agent.updated", "approval.requested", "graph.node_entered", "agent.*", "*",
    "tool.*", "turn.*", "llm.called*", "session*", "sess?on.ended", "[sw]ession.ended", "canvas*", "canvas", "[c]anvas.event",
]


def _ctx(who: PrincipalRef | None) -> ToolContext:
    return ToolContext(tool_call_id="call-1", session_id="sess-1", workspace_id="w", initiated_by=who)


async def _subscriptions(sp) -> list[EventSubscription]:  # noqa: F811 - the re-exported fixture's name
    return (await sp.get_storage(EventSubscription).list(OffsetPage(length=50))).items


@pytest.mark.parametrize("who", [ADMIN, PrincipalRef.system()], ids=["admin", "system"])
@pytest.mark.parametrize("glob", HIDDEN + LIFECYCLE)
async def test_an_admin_may_wait_on_any_kind(sp, who, glob):  # noqa: F811
    result = await make_wait_for_event_handler(sp)({"event_types": [glob]}, ctx=_ctx(who))

    assert isinstance(result, Yielded), result
    assert [s.filter.event_types for s in await _subscriptions(sp)] == [[glob]]


@pytest.mark.parametrize("glob", LIFECYCLE)
async def test_a_non_admin_may_wait_on_the_kinds_the_feed_shows_it(sp, glob):  # noqa: F811
    result = await make_wait_for_event_handler(sp)({"event_types": [glob]}, ctx=_ctx(USER))

    assert isinstance(result, Yielded), result
    assert [s.filter.event_types for s in await _subscriptions(sp)] == [[glob]]


@pytest.mark.parametrize("who", [USER, None], ids=["user", "no-initiator"])
@pytest.mark.parametrize("glob", HIDDEN)
async def test_a_non_admin_is_refused_every_other_kind(sp, who, glob):  # noqa: F811
    result = await make_wait_for_event_handler(sp)({"event_types": [glob]}, ctx=_ctx(who))

    assert result.is_error and json.loads(result.output)["type"] == "forbidden", result
    assert await _subscriptions(sp) == [], "a refused wait stores no subscription"


async def test_one_hidden_kind_refuses_the_whole_wait_and_the_message_names_it(sp):  # noqa: F811
    result = await make_wait_for_event_handler(sp)({"event_types": ["session.ended", "agent.*"]}, ctx=_ctx(USER))

    assert json.loads(result.output) == {
        "type": "forbidden",
        "message": (
            "this run's principal is not an admin, so it may wait only on the event kinds the event feed shows a non-admin "
            "(session.*, workspace.*, canvas.*, llm.called, tool.called, trigger.fired, turn.started); refused: 'agent.*'"
        ),
    }
    assert await _subscriptions(sp) == []


def test_the_rule_is_the_event_feeds_own():
    """No second list: the REST feed filters with the same function, and a literal kind is judged exactly as the feed judges it."""
    assert events_route.user_safe is visibility.user_safe
    kinds = sorted(ACTION_EVENT_TYPES) + [f"{kind}.{verb}" for kind in ("agent", "graph", "collection", "document", "toolset") for verb in ("created", "updated", "deleted")]
    assert {kind: visibility.user_safe_glob(kind) for kind in kinds} == {kind: visibility.user_safe(kind) for kind in kinds}
    assert any(visibility.user_safe(kind) for kind in kinds) and not all(visibility.user_safe(kind) for kind in kinds)


# ---- expr and regex: admin runs only ------------------------------------------------------------------------------------------------------------------

EXPR = 'package primer.event_filter\nmatch := input.payload.collection_id == "kb"\n'
REGEX = [{"path": "payload.path", "op": "regex", "value": "^guides/"}]
EQ_AND_PREFIX = [{"path": "payload.workspace_id", "op": "eq", "value": "w"}, {"path": "payload.path", "op": "prefix", "value": "src/"}]


@pytest.mark.parametrize("who", [USER, None], ids=["user", "no-initiator"])
async def test_a_non_admin_may_not_pass_an_expr(sp, who):  # noqa: F811
    result = await make_wait_for_event_handler(sp)({"event_types": ["session.ended"], "expr": EXPR}, ctx=_ctx(who))

    assert json.loads(result.output) == {
        "type": "forbidden",
        "message": (
            "this run's principal is not an admin, so it may not pass an expr (a rego module the platform evaluates); "
            "filter with fields matchers instead"
        ),
    }
    assert await _subscriptions(sp) == [], "a refused wait stores no subscription, so the dispatcher never evaluates the expr"


@pytest.mark.parametrize("who", [USER, None], ids=["user", "no-initiator"])
async def test_a_non_admin_fields_matcher_may_not_use_regex(sp, who):  # noqa: F811
    fields = EQ_AND_PREFIX + REGEX
    result = await make_wait_for_event_handler(sp)({"event_types": ["workspace.file_changed"], "fields": fields}, ctx=_ctx(who))

    assert json.loads(result.output) == {
        "type": "forbidden",
        "message": (
            "this run's principal is not an admin, so its fields matchers may use only the eq and prefix ops; "
            "refused regex on: 'payload.path'"
        ),
    }
    assert await _subscriptions(sp) == []


async def test_a_non_admin_may_use_eq_and_prefix_matchers(sp):  # noqa: F811
    result = await make_wait_for_event_handler(sp)(
        {"event_types": ["workspace.file_changed"], "fields": EQ_AND_PREFIX}, ctx=_ctx(USER),
    )

    assert isinstance(result, Yielded), result
    [stored] = await _subscriptions(sp)
    assert [(m.path, m.op) for m in stored.filter.fields] == [("payload.workspace_id", "eq"), ("payload.path", "prefix")]


@pytest.mark.parametrize("who", [ADMIN, PrincipalRef.system()], ids=["admin", "system"])
async def test_an_admin_keeps_expr_and_regex(sp, who):  # noqa: F811
    result = await make_wait_for_event_handler(sp)(
        {"event_types": ["collection.document_pushed"], "expr": EXPR, "fields": REGEX}, ctx=_ctx(who),
    )

    assert isinstance(result, Yielded), result
    [stored] = await _subscriptions(sp)
    assert stored.filter.expr == EXPR and [(m.op, m.value) for m in stored.filter.fields] == [("regex", "^guides/")]
