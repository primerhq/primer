"""The guards of a peer message (pivot P3, plan tasks 4 and 5): the rank rule, the hop limit, the rate ledger, the scope and the display helpers.

``message_session`` is the one way an agent drives another session's run, so what it may do is decided here, before anything is written. The rank
rule is the one a trigger's steer uses (``primer.authz.principal_rank``); the tests in this file pin how the peer guards read it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from primer.agent.invoke import MAX_INVOCATION_DEPTH
from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    PeerMessage,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.peer_messages import (
    MAX_PEER_HOPS,
    PARKED_ON_LABELS,
    PEER_RATE_LIMIT,
    PEER_RATE_WINDOW,
    REFUSE_HOPS,
    REFUSE_RATE,
    PeerMessageRefused,
    check_peer_rate,
    next_peer_hops,
    parked_on,
    peer_prefix,
    rank_label,
    record_peer_message,
    resolve_default_workspace_id,
    sender_may_message,
)
from tests.session._peer_fakes import make_world

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _ref(role, type_="user"):
    return PrincipalRef(type=type_, id="x", display="x", role=role, source="local")


def _session(**fields) -> WorkspaceSession:
    sid = fields.pop("sid", "ses-a")
    binding = fields.pop("binding", None) or AgentSessionBinding(agent_id="agent-" + sid)
    return WorkspaceSession(
        id=sid, workspace_id="ws-1", binding=binding, status=fields.pop("status", SessionStatus.WAITING),
        created_at=NOW, **fields,
    )


def _ledger(sender: str, sent_at: datetime, **fields) -> PeerMessage:
    return PeerMessage(
        **{
            "from_session_id": sender, "from_workspace_id": "ws-1", "to_session_id": "ses-z", "to_workspace_id": "ws-1",
            "hops": 1, "delivery": "woken", "sent_at": sent_at, **fields,
        },
    )


async def _ledger_rows(world, sender: str | None = None) -> list[PeerMessage]:
    rows = (await world.sp.get_storage(PeerMessage).find(None, OffsetPage(offset=0, length=200))).items
    return [r for r in rows if sender is None or r.from_session_id == sender]


# ---- the rank rule (task 4) -------------------------------------------------------------------------------------------------------


def test_a_user_run_cannot_message_a_session_an_admin_started():
    user, admin = _ref("user"), _ref("admin")
    assert sender_may_message(user, admin) is False
    assert sender_may_message(admin, user) is True
    assert sender_may_message(user, user) is True
    assert sender_may_message(user, None) is True            # no recorded creator: it runs as an unattributed user
    assert sender_may_message(None, user) is False           # an unknown sender fails closed
    assert sender_may_message(PrincipalRef.system(), admin) is True
    assert sender_may_message(admin, PrincipalRef.system()) is False


@pytest.mark.parametrize(
    ("sender", "target", "allowed"),
    [
        pytest.param(_ref("restricted"), _ref("user"), False, id="restricted to user"),
        pytest.param(_ref("restricted"), _ref("restricted"), True, id="restricted to restricted"),
        pytest.param(_ref("user"), _ref("restricted"), True, id="user to restricted"),
        pytest.param(PrincipalRef.system(), PrincipalRef.system(), True, id="system to system"),
        pytest.param(PrincipalRef.unattributed(), None, True, id="unattributed to no creator"),
        pytest.param(PrincipalRef.unattributed(), _ref("admin"), False, id="unattributed to admin"),
        pytest.param(_ref(None), _ref("user"), False, id="a sender with no role to user"),
        pytest.param(_ref("auditor"), _ref("user"), False, id="an unranked role to user"),
        pytest.param(_ref("restricted"), _ref(None, "trigger"), True, id="restricted to a trigger run with no role"),
        pytest.param(_ref("user", "api_token"), _ref("user"), True, id="a token run ranks by its role"),
    ],
)
def test_the_sender_must_rank_at_least_as_high_as_the_target_creator(sender, target, allowed):
    assert sender_may_message(sender, target) is allowed


def test_the_refusal_carries_its_type_and_text():
    exc = PeerMessageRefused("forbidden", "no")
    assert (exc.error_type, exc.message, str(exc)) == ("forbidden", "no", "no")
    assert rank_label(None) == "user (unattributed)"
    assert rank_label(PrincipalRef.system()) == "system"


def test_the_rank_label_names_the_role_and_never_who_it_is():
    """The refusal text reaches the sending agent: it says at which rank the target runs, not which person or token started it."""
    assert rank_label(_ref("admin")) == "admin"
    assert rank_label(_ref("restricted", "api_token")) == "restricted"


# ---- the hop limit (task 5) -------------------------------------------------------------------------------------------------------


def test_a_person_started_turn_sends_hop_one():
    assert next_peer_hops(_session(peer_hops=0)) == 1
    assert next_peer_hops(_session(peer_hops=MAX_PEER_HOPS - 1)) == MAX_PEER_HOPS


def test_a_turn_started_by_a_message_at_the_hop_limit_cannot_send():
    with pytest.raises(PeerMessageRefused) as info:
        next_peer_hops(_session(peer_hops=MAX_PEER_HOPS))
    assert info.value.error_type == "hop-limit"
    assert f"{MAX_PEER_HOPS} hops" in info.value.message and f"limit is {MAX_PEER_HOPS}" in info.value.message
    assert info.value.message == REFUSE_HOPS.format(hops=MAX_PEER_HOPS, limit=MAX_PEER_HOPS)


@pytest.mark.skipif(MAX_PEER_HOPS != 8, reason="the plan's copy at the default limit of 8 (PRIMER_MAX_INVOCATION_DEPTH is set)")
def test_a_turn_started_by_a_hop_eight_message_cannot_send():
    assert next_peer_hops(_session(peer_hops=7)) == 8
    with pytest.raises(PeerMessageRefused) as info:
        next_peer_hops(_session(peer_hops=8))
    assert info.value.error_type == "hop-limit"
    assert "8 hops" in info.value.message and "limit is 8" in info.value.message


def test_a_count_past_the_limit_is_refused_and_reported_as_it_is():
    """A limit lowered since a long chain began: the session reads 12 hops and the limit is 8."""
    with pytest.raises(PeerMessageRefused) as info:
        next_peer_hops(_session(peer_hops=MAX_PEER_HOPS + 4))
    assert f"already {MAX_PEER_HOPS + 4} hops" in info.value.message


def test_the_hop_limit_is_the_invocation_depth_limit():
    """One loop bound for delegation and for messaging, compared the same way: a sender AT the limit is refused, as ``invocation_depth_guard``
    refuses at depth >= max."""
    assert MAX_PEER_HOPS == MAX_INVOCATION_DEPTH


def test_the_refusal_copy_is_the_plans():
    assert REFUSE_HOPS == (
        "this turn was started by a peer message that is already {hops} hops from a person (the limit is {limit}); "
        "answer without messaging another session"
    )
    assert REFUSE_RATE == "this session sent {limit} messages in the last 10 minutes; retry in {seconds} seconds"
    assert (PEER_RATE_LIMIT, PEER_RATE_WINDOW) == (20, timedelta(minutes=10)), "REFUSE_RATE says ten minutes in words"


# ---- the rate ledger (task 5) -----------------------------------------------------------------------------------------------------


async def test_the_twenty_first_message_in_ten_minutes_is_refused():
    world = make_world()
    for i in range(19):
        await record_peer_message(world.sp, _ledger("ses-a", NOW - timedelta(seconds=20 * i)))
    await check_peer_rate(world.sp, "ses-a", now=NOW)          # 19 in the window: allowed
    await record_peer_message(world.sp, _ledger("ses-a", NOW))
    with pytest.raises(PeerMessageRefused) as info:
        await check_peer_rate(world.sp, "ses-a", now=NOW)
    assert info.value.error_type == "rate-limited"
    assert "sent 20 messages in the last 10 minutes" in info.value.message
    await check_peer_rate(world.sp, "ses-other", now=NOW)      # another sender is not affected


async def test_messages_older_than_ten_minutes_do_not_count():
    world = make_world()
    for i in range(20):
        await record_peer_message(world.sp, _ledger("ses-a", NOW - timedelta(minutes=11, seconds=i)))
    await check_peer_rate(world.sp, "ses-a", now=NOW)


async def test_a_message_exactly_ten_minutes_old_has_left_the_window():
    world = make_world()
    for i in range(19):
        await record_peer_message(world.sp, _ledger("ses-a", NOW - timedelta(seconds=10 * i)))
    await record_peer_message(world.sp, _ledger("ses-a", NOW - PEER_RATE_WINDOW))
    await check_peer_rate(world.sp, "ses-a", now=NOW)


async def test_the_retry_time_is_when_the_oldest_message_leaves_the_window():
    world = make_world()
    for i in range(20):
        await record_peer_message(world.sp, _ledger("ses-a", NOW - timedelta(seconds=30 * i)))   # the oldest is 570 s old
    with pytest.raises(PeerMessageRefused) as info:
        await check_peer_rate(world.sp, "ses-a", now=NOW)
    assert info.value.message == REFUSE_RATE.format(limit=20, seconds=30)


async def test_the_retry_time_is_at_least_one_second():
    world = make_world()
    for i in range(19):
        await record_peer_message(world.sp, _ledger("ses-a", NOW - timedelta(seconds=i)))
    await record_peer_message(world.sp, _ledger("ses-a", NOW - PEER_RATE_WINDOW + timedelta(milliseconds=1)))
    with pytest.raises(PeerMessageRefused) as info:
        await check_peer_rate(world.sp, "ses-a", now=NOW)
    assert info.value.message == REFUSE_RATE.format(limit=20, seconds=1)


async def test_a_stored_time_without_a_zone_is_read_as_utc():
    world = make_world()
    for i in range(20):
        await record_peer_message(world.sp, _ledger("ses-a", (NOW - timedelta(seconds=i)).replace(tzinfo=None)))
    with pytest.raises(PeerMessageRefused):
        await check_peer_rate(world.sp, "ses-a", now=NOW)


async def test_a_time_without_a_zone_is_read_as_utc_on_both_sides():
    world = make_world()
    for i in range(20):
        await record_peer_message(world.sp, _ledger("ses-a", NOW - timedelta(seconds=i)))
    with pytest.raises(PeerMessageRefused):
        await check_peer_rate(world.sp, "ses-a", now=NOW.replace(tzinfo=None))


async def test_the_ledger_keeps_only_the_newest_twenty_per_sender():
    world = make_world()
    for i in range(25):
        await record_peer_message(world.sp, _ledger("ses-a", NOW - timedelta(seconds=i)))
    await record_peer_message(world.sp, _ledger("ses-b", NOW))
    rows = await _ledger_rows(world)
    assert sum(r.from_session_id == "ses-a" for r in rows) == 20
    assert sum(r.from_session_id == "ses-b" for r in rows) == 1
    kept = sorted(r.sent_at for r in await _ledger_rows(world, "ses-a"))
    assert kept == sorted(NOW - timedelta(seconds=i) for i in range(20)), "the OLDEST rows go, never the newest"


async def test_trimming_one_sender_never_touches_another():
    world = make_world()
    for i in range(30):
        await record_peer_message(world.sp, _ledger("ses-a", NOW - timedelta(seconds=i)))
        await record_peer_message(world.sp, _ledger("ses-b", NOW - timedelta(seconds=i)))
    assert len(await _ledger_rows(world, "ses-a")) == len(await _ledger_rows(world, "ses-b")) == 20


def test_a_ledger_row_gets_a_pmsg_id_and_refuses_a_bad_hop_count_or_delivery():
    assert _ledger("ses-a", NOW).id.startswith("pmsg-")
    with pytest.raises(ValidationError):
        _ledger("ses-a", NOW, hops=0)
    with pytest.raises(ValidationError):
        _ledger("ses-a", NOW, delivery="sent")
    with pytest.raises(ValidationError):
        _ledger("", NOW)


# ---- the default workspace (task 5) -----------------------------------------------------------------------------------------------


async def test_the_default_workspace_falls_back_to_the_reserved_id_until_p2_records_one():
    assert await resolve_default_workspace_id(make_world().sp) == "primer"

    class _AfterP2:
        async def get_system_state(self):
            return SimpleNamespace(default_workspace_id="prime")

    assert await resolve_default_workspace_id(_AfterP2()) == "prime"


async def test_an_unset_default_workspace_is_the_reserved_id():
    class _Unset:
        async def get_system_state(self):
            return SimpleNamespace(default_workspace_id=None)

    assert await resolve_default_workspace_id(_Unset()) == "primer"


# ---- what a parked session waits on (task 5) --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("key", "label"), [
    ("ask_user:ses-1:call_1", "ask_user"), ("tool_approval:ses-1:call_1", "approval"),
    ("evwait:ses-1:call_1", "wait_for_event"), ("timer:ses-1:call_1", "sleep"),
    ("watch:ses-1:call_1", "watch_files"), ("trigger:ses-1:t1", "subscribe_to_trigger"),
    ("external_tool:ses-1:c1", "external_tool"), ("mcp_task:srv:t1", "mcp_task"),
    ("tool_wait:ses-1:c1", "tool_call"), ("custom:x", "custom"),
])
def test_parked_on_names_what_the_session_waits_for(key, label):
    assert parked_on(_session(parked_status="parked", parked_event_key=key)) == label
    assert parked_on(_session(parked_status="parked", parked_event_keys=[key])) == label
    assert parked_on(_session(parked_status="resumable", parked_event_key=key)) == label
    assert parked_on(_session()) is None


def test_every_label_has_a_real_key_prefix():
    assert sorted(PARKED_ON_LABELS) == [
        "ask_user", "evwait", "external_tool", "mcp_task", "timer", "tool_approval", "tool_wait", "trigger", "watch",
    ]


def test_a_session_with_several_parks_reports_its_first_key():
    row = _session(parked_status="parked", parked_event_keys=["evwait:ses-1:call_w", "timer:ses-1:call_t"])
    assert parked_on(row) == "wait_for_event"


def test_a_scoped_key_is_named_by_its_prefix():
    """A gate inside a graph node carries the node in its key (``ask_user:{session}:{node}:{call}``)."""
    assert parked_on(_session(parked_status="parked", parked_event_key="ask_user:ses-1:node-a:call_1")) == "ask_user"


def test_an_ended_session_waits_for_nothing_whatever_its_park_columns_say():
    """Cancel and the workspace_lost reconcile end a session and leave its park set; the session is ended, not waiting."""
    row = _session(status=SessionStatus.ENDED, ended_reason="cancelled", parked_status="parked", parked_event_key="ask_user:ses-1:call_1")
    assert parked_on(row) is None


def test_a_park_with_no_key_is_named_unknown_not_blank():
    assert parked_on(_session(parked_status="parked")) == "unknown"


# ---- the prefix (task 5) ----------------------------------------------------------------------------------------------------------


def test_the_prefix_names_the_sender_session_and_its_agent_or_graph():
    assert peer_prefix(_session(sid="ses-a", binding=AgentSessionBinding(agent_id="planner"))) == "[from session ses-a (agent planner)]"
    assert peer_prefix(_session(sid="ses-g", binding=GraphSessionBinding(graph_id="triage"))) == "[from session ses-g (graph triage)]"
