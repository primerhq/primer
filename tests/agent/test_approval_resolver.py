"""Tests for ApprovalResolver lookup + cache + invalidate."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest

from primer.agent.approval import ApprovalContext, ApprovalResolver, effective_approvers, evaluate_approval_gate
from primer.common.toolset_ids import TOOLSET_ID_ALIASES
from primer.model.tool_approval import (
    ApprovalType,
    ApproverSpec,
    PolicyApprovalConfig,
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)


class _FakeStorage:
    def __init__(self, rows: list[ToolApprovalPolicy]) -> None:
        self._rows = rows
        self.find_calls = 0

    async def find(self, predicate, page, *, order_by=None):
        self.find_calls += 1
        class _Resp:
            items = [
                r for r in self._rows
                if r.toolset_id == predicate.left.left.right.value
                and r.tool_name == predicate.left.right.right.value
            ]
        return _Resp()


@pytest.mark.asyncio
async def test_resolver_returns_none_when_missing():
    storage = _FakeStorage([])
    r = ApprovalResolver(storage=storage)
    assert await r.find(toolset_id="system", tool_name="x") is None


@pytest.mark.asyncio
async def test_resolver_returns_match():
    policy = ToolApprovalPolicy(
        id="p", toolset_id="system", tool_name="shell_exec",
        approval=RequiredApprovalConfig(),
    )
    storage = _FakeStorage([policy])
    r = ApprovalResolver(storage=storage)
    hit = await r.find(toolset_id="system", tool_name="shell_exec")
    assert hit is not None and hit.id == "p"


@pytest.mark.asyncio
async def test_resolver_caches_within_ttl():
    # A toolset with no retired spelling: one uncached lookup is one storage read (an aliased one reads each spelling, see below).
    policy = ToolApprovalPolicy(
        id="p", toolset_id="mcp-1", tool_name="shell_exec",
        approval=RequiredApprovalConfig(),
    )
    storage = _FakeStorage([policy])
    r = ApprovalResolver(storage=storage, cache_ttl_seconds=60.0)
    await r.find(toolset_id="mcp-1", tool_name="shell_exec")
    await r.find(toolset_id="mcp-1", tool_name="shell_exec")
    assert storage.find_calls == 1


@pytest.mark.asyncio
async def test_resolver_invalidate_clears_cache():
    policy = ToolApprovalPolicy(
        id="p", toolset_id="mcp-1", tool_name="shell_exec",
        approval=RequiredApprovalConfig(),
    )
    storage = _FakeStorage([policy])
    r = ApprovalResolver(storage=storage, cache_ttl_seconds=60.0)
    await r.find(toolset_id="mcp-1", tool_name="shell_exec")
    r.invalidate()
    await r.find(toolset_id="mcp-1", tool_name="shell_exec")
    assert storage.find_calls == 2


# ---- duplicate enabled policies for one (toolset_id, tool_name) (ticket 01a11354) ----------------------------------------------
#
# Uniqueness is refused at write time (REST and the system tools share ``check_policy_unique``) but is not a storage constraint, so a
# raced pair of creates or a row that predates the check can leave two ENABLED policies for one tool. The resolver took the first row
# of an unordered ``find(length=1)``: which policy gated the call depended on the backend's row order, a weaker duplicate could shadow
# a strict one, and the winner could change with no edit (an update or a vacuum moves rows). Now every enabled candidate is read and
# the choice depends on the SET, never the order: an unconditional gate wins; two conditional policies cannot be compared without the
# call, so the resolver gates unconditionally (fail closed) and logs the ids until the operator deletes one.


def _policy(pid: str, approval, **fields) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id=pid, toolset_id="system", tool_name="shell_exec", approval=approval, **fields)


_ALLOWS = "package primer.tool_approval\ndefault required := false\n"
_REQUIRES = "package primer.tool_approval\ndefault required := true\n"


def _rego(source: str = _ALLOWS) -> PolicyApprovalConfig:
    return PolicyApprovalConfig(policy=source)


# `roles` with no roles: ApproverSpec.allows admits only an admin (admins are always admitted, whatever the kind), the narrowest spec there is.
_ADMIN_ONLY = ApproverSpec(kind="roles", roles=[])
_ROUTES_TO_ALICE = (
    "package primer.tool_approval\n"
    "default required := true\n"
    "approvers := {\"kind\": \"users\", \"users\": [\"alice\"]}\n"
)

_CONTEXT = ApprovalContext(
    tool_name="shell_exec", toolset_id="system", arguments={}, agent_id=None, session_id=None, chat_id=None,
    requested_at=datetime.now(UTC),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True], ids=["weak-first", "strict-first"])
async def test_an_unconditional_duplicate_is_not_shadowed_by_a_conditional_one_in_either_order(reverse):
    weak = _policy("p-a-weak", _rego())
    strict = _policy("p-b-strict", RequiredApprovalConfig())
    rows = [strict, weak] if reverse else [weak, strict]
    resolver = ApprovalResolver(storage=_FakeStorage(rows))

    chosen = await resolver.find(toolset_id="system", tool_name="shell_exec")

    assert chosen is not None and chosen.id == "p-b-strict" and chosen.approval.type is ApprovalType.REQUIRED, (
        f"a weaker duplicate shadowed the unconditional gate: {chosen}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True], ids=["a-first", "b-first"])
async def test_two_conditional_duplicates_gate_unconditionally_whatever_the_order(reverse):
    allows = _policy("p-a", _rego(_ALLOWS))
    requires = _policy("p-b", _rego(_REQUIRES))
    rows = [requires, allows] if reverse else [allows, requires]
    resolver = ApprovalResolver(storage=_FakeStorage(rows))

    chosen = await resolver.find(toolset_id="system", tool_name="shell_exec")

    assert chosen is not None
    verdict = await evaluate_approval_gate(policy=chosen, context=_CONTEXT, provider_registry=None)
    assert verdict.required, "two conditional duplicates cannot be compared, so the gate must trip, whichever row came first"
    assert chosen.id == "p-a", "the stored row it stands for is the same in either order (the lowest id)"


@pytest.mark.asyncio
async def test_the_gate_of_a_duplicate_keeps_the_identity_and_timeout_of_a_real_row_and_is_decided_by_an_admin_only():
    """Failing closed changes WHETHER the call is gated and WHO may decide it, not its identity or timeout: those come from a stored row.
    (An earlier version of this test expected the lowest-id row's approvers to carry over, which let a duplicate WIDEN who may decide.)"""
    a = _policy("p-a", _rego(), timeout_seconds=42.0, approvers=ApproverSpec(kind="roles", roles=["ops"]))
    b = _policy("p-b", _rego(), timeout_seconds=7.0)
    resolver = ApprovalResolver(storage=_FakeStorage([b, a]))

    chosen = await resolver.find(toolset_id="system", tool_name="shell_exec")

    assert chosen.id == "p-a" and chosen.timeout_seconds == 42.0
    verdict = await evaluate_approval_gate(policy=chosen, context=_CONTEXT, provider_registry=None)
    assert effective_approvers(chosen, verdict) == _ADMIN_ONLY


@pytest.mark.asyncio
async def test_a_duplicate_is_logged_with_the_ids_so_the_operator_can_delete_one(caplog):
    resolver = ApprovalResolver(storage=_FakeStorage([_policy("p-a", _rego()), _policy("p-b", RequiredApprovalConfig())]))

    with caplog.at_level(logging.WARNING, logger="primer.agent.approval"):
        await resolver.find(toolset_id="system", tool_name="shell_exec")

    text = " ".join(r.getMessage() for r in caplog.records)
    assert "p-a" in text and "p-b" in text and "shell_exec" in text, text


@pytest.mark.asyncio
async def test_a_single_conditional_policy_is_returned_as_stored(caplog):
    """The control: nothing changes for the normal case of one policy per tool, and nothing is logged."""
    only = _policy("p-only", _rego())
    resolver = ApprovalResolver(storage=_FakeStorage([only]))

    with caplog.at_level(logging.WARNING, logger="primer.agent.approval"):
        chosen = await resolver.find(toolset_id="system", tool_name="shell_exec")

    assert chosen is only and chosen.approval.type is ApprovalType.POLICY
    assert not [r for r in caplog.records if r.name == "primer.agent.approval"]


@pytest.mark.asyncio
@pytest.mark.parametrize("weak_first", [True, False], ids=["weak-created-first", "strict-created-first"])
async def test_on_real_storage_a_raced_duplicate_never_weakens_the_gate(tmp_path, weak_first):
    """Through the real predicate (``enabled`` included): two enabled rows written straight to storage, as a race or a legacy row
    leaves them, in both insertion orders; a DISABLED strict duplicate is not a candidate."""
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    try:
        storage = sp.get_storage(ToolApprovalPolicy)
        # The weak row has the LOWEST id on purpose: sqlite's default order is by id, so whichever way the rows were inserted the old
        # first-row pick returns the weak one (Postgres' heap order would vary with updates and vacuum instead).
        weak, strict = _policy("p-a-weak", _rego()), _policy("p-b-strict", RequiredApprovalConfig())
        for row in ((weak, strict) if weak_first else (strict, weak)):
            await storage.create(row)
        # Disabled, unconditional and with the LOWEST id of all: were the `enabled` term dropped from the lookup it would win.
        await storage.create(_policy("p-0-disabled", RequiredApprovalConfig(), enabled=False))

        chosen = await ApprovalResolver(storage=storage).find(toolset_id="system", tool_name="shell_exec")

        assert chosen is not None and chosen.id == "p-b-strict" and chosen.approval.type is ApprovalType.REQUIRED
    finally:
        await sp.aclose()


# ---- who may decide: a duplicate must not widen it (review of #531) -------------------------------------------------------------
#
# A conditional policy can route a call to specific approvers PER CALL (the verdict's `approvers`), and a row has its own `approvers`.
# Failing closed on the gate by replacing the approval with `required` discards the per-call routing, and a duplicate with
# approvers=None would let ANY user decide what a single row restricted to alice. Whenever the choice is made among duplicates and
# they are not all unconditional with one identical approver spec, only an admin (always admitted, whatever the spec) may decide.


async def _may_decide(chosen, username: str, role: str) -> bool:
    verdict = await evaluate_approval_gate(policy=chosen, context=_CONTEXT, provider_registry=None)
    return (effective_approvers(chosen, verdict) or ApproverSpec()).allows(username=username, role=role)


@pytest.mark.asyncio
async def test_a_single_row_that_routes_to_alice_is_decided_by_alice_or_an_admin_only():
    """The baseline the duplicate cases are measured against."""
    only = _policy("p-only", _rego(_ROUTES_TO_ALICE))
    chosen = await ApprovalResolver(storage=_FakeStorage([only])).find(toolset_id="system", tool_name="shell_exec")

    assert await _may_decide(chosen, "alice", "user") and await _may_decide(chosen, "root", "admin")
    assert not await _may_decide(chosen, "bob", "user")


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True], ids=["a-first", "b-first"])
async def test_two_conditional_duplicates_that_route_to_alice_are_decided_by_an_admin_only(reverse):
    rows = [_policy("p-a", _rego(_ROUTES_TO_ALICE)), _policy("p-b", _rego(_ROUTES_TO_ALICE))]
    chosen = await ApprovalResolver(storage=_FakeStorage(rows[::-1] if reverse else rows)).find(
        toolset_id="system", tool_name="shell_exec",
    )

    assert not await _may_decide(chosen, "bob", "user"), "a duplicate let any user decide what each row restricts to alice"
    assert not await _may_decide(chosen, "carol", "user")
    # Each row routes to alice, yet the stand-in gate is admin-only: a duplicate is decided by an admin until the extra row is deleted.
    assert not await _may_decide(chosen, "alice", "user"), "alice, the routed approver of each duplicate, decided a gate that stands for both"
    assert await _may_decide(chosen, "root", "admin")


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True], ids=["conditional-first", "unconditional-first"])
async def test_an_unconditional_duplicate_without_approvers_does_not_widen_a_conditional_rows_routing(reverse):
    conditional = _policy("p-a", _rego(_ROUTES_TO_ALICE), approvers=ApproverSpec(kind="users", users=["alice"]))
    unconditional = _policy("p-b", RequiredApprovalConfig())  # approvers=None: anyone
    rows = [unconditional, conditional] if reverse else [conditional, unconditional]

    chosen = await ApprovalResolver(storage=_FakeStorage(rows)).find(toolset_id="system", tool_name="shell_exec")

    assert chosen.id == "p-b" and chosen.approval.type is ApprovalType.REQUIRED, "the unconditional gate still wins"
    assert not await _may_decide(chosen, "bob", "user"), "the unconditional row's empty approvers widened who may decide"
    assert await _may_decide(chosen, "root", "admin")


@pytest.mark.asyncio
async def test_unconditional_duplicates_with_the_same_approvers_keep_them():
    """Nothing to widen: every candidate says the same, so the spec carries over unchanged (not narrowed to admin-only)."""
    spec = ApproverSpec(kind="roles", roles=["ops"])
    rows = [_policy("p-a", RequiredApprovalConfig(), approvers=spec), _policy("p-b", RequiredApprovalConfig(), approvers=spec)]

    chosen = await ApprovalResolver(storage=_FakeStorage(rows)).find(toolset_id="system", tool_name="shell_exec")

    assert chosen.id == "p-a" and chosen.approvers == spec
    assert await _may_decide(chosen, "olive", "ops") and not await _may_decide(chosen, "bob", "user")


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True], ids=["a-first", "b-first"])
async def test_unconditional_duplicates_with_different_approvers_are_decided_by_an_admin_only(reverse):
    rows = [
        _policy("p-a", RequiredApprovalConfig(), approvers=ApproverSpec(kind="roles", roles=["ops"])),
        _policy("p-b", RequiredApprovalConfig()),
    ]

    chosen = await ApprovalResolver(storage=_FakeStorage(rows[::-1] if reverse else rows)).find(
        toolset_id="system", tool_name="shell_exec",
    )

    assert not await _may_decide(chosen, "olive", "ops") and not await _may_decide(chosen, "bob", "user")
    assert await _may_decide(chosen, "root", "admin")


@pytest.mark.asyncio
async def test_the_gate_reason_names_the_duplicate_fallback():
    """The approval card and the record show the reason: without it a gate that appears out of nowhere reads as the policy's own."""
    rows = [_policy("p-a", _rego()), _policy("p-b", _rego())]
    chosen = await ApprovalResolver(storage=_FakeStorage(rows)).find(toolset_id="system", tool_name="shell_exec")

    verdict = await evaluate_approval_gate(policy=chosen, context=_CONTEXT, provider_registry=None)

    assert verdict.required and verdict.reason, "the fallback gate carries no reason"
    assert "duplicate" in verdict.reason and "p-a" in verdict.reason and "p-b" in verdict.reason, verdict.reason
    assert "admin" in verdict.reason, "it should say who may decide it"


# ---- one toolset, several spellings (security: the approval-gate bypass through a toolset-id alias) ---------------------------------
#
# The registry runs the SAME provider for ``_system`` and ``system``, so a policy has to gate a call whichever way either is spelled. The lookup keyed on the id as
# written: a policy on ``system::list_llm_providers`` did not gate ``_system::list_llm_providers`` (a bypass), and a policy written for ``_system`` (the model
# accepts any string, and its field description names the underscore ids) did not gate ``system`` (the same bypass the other way). The resolver now reads every spelling
# of the toolset it is asked for and keys its cache on the current id.

ALIASES = sorted(TOOLSET_ID_ALIASES.items())


def _on(toolset_id: str, pid: str = "p", approval=None, **fields) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(
        id=pid, toolset_id=toolset_id, tool_name="shell_exec", approval=approval if approval is not None else RequiredApprovalConfig(), **fields,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("alias", "current"), ALIASES)
@pytest.mark.parametrize("stored", ["current", "alias"])
@pytest.mark.parametrize("asked", ["current", "alias"])
async def test_a_policy_is_found_under_every_spelling_of_its_toolset(alias, current, stored, asked):
    spelled = {"current": current, "alias": alias}
    resolver = ApprovalResolver(storage=_FakeStorage([_on(spelled[stored])]))

    hit = await resolver.find(toolset_id=spelled[asked], tool_name="shell_exec")

    assert hit is not None and hit.id == "p", f"the policy stored under {spelled[stored]!r} did not gate a lookup for {spelled[asked]!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("alias", "current"), ALIASES)
async def test_another_toolsets_policy_for_a_tool_of_the_same_name_gates_neither_spelling(alias, current):
    resolver = ApprovalResolver(storage=_FakeStorage([_on("crud")]))

    assert await resolver.find(toolset_id=alias, tool_name="shell_exec") is None
    assert await resolver.find(toolset_id=current, tool_name="shell_exec") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("alias", "current"), ALIASES)
async def test_the_spellings_share_one_cache_entry(alias, current):
    """Both spellings are read once on a miss; after that a lookup under EITHER is a hit, and ``invalidate`` drops it for both."""
    storage = _FakeStorage([_on(current)])
    resolver = ApprovalResolver(storage=storage, cache_ttl_seconds=60.0)

    await resolver.find(toolset_id=alias, tool_name="shell_exec")
    assert storage.find_calls == 2, "one read for each spelling of the toolset"
    await resolver.find(toolset_id=current, tool_name="shell_exec")
    await resolver.find(toolset_id=alias, tool_name="shell_exec")
    assert storage.find_calls == 2, "a lookup under either spelling is answered from the one cache entry"

    resolver.invalidate()
    await resolver.find(toolset_id=current, tool_name="shell_exec")
    assert storage.find_calls == 4


@pytest.mark.asyncio
async def test_a_toolset_with_no_retired_spelling_is_still_read_once():
    storage = _FakeStorage([_on("crud")])

    assert (await ApprovalResolver(storage=storage).find(toolset_id="crud", tool_name="shell_exec")).id == "p"

    assert storage.find_calls == 1


class _IgnoresThePredicate:
    """A storage whose ``find`` returns every row whatever it was asked (as the system toolset's test storage does)."""

    def __init__(self, rows: list[ToolApprovalPolicy]) -> None:
        self._rows = rows

    async def find(self, predicate, page, *, order_by=None):
        class _Resp:
            items = list(self._rows)

        return _Resp()


@pytest.mark.asyncio
async def test_a_row_read_for_both_spellings_is_one_policy_not_a_duplicate_of_itself():
    only = _on("system", pid="p-only")

    chosen = await ApprovalResolver(storage=_IgnoresThePredicate([only])).find(toolset_id="_system", tool_name="shell_exec")

    assert chosen is only, "the same stored row, read twice, was treated as two policies"


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True], ids=["alias-first", "current-first"])
async def test_policies_under_both_spellings_are_duplicates_and_fail_closed(reverse, caplog):
    """Uniqueness is refused per toolset at write time, but a row written before it (or by a race) can leave one policy for each spelling. They are the SAME tool's
    policies, so the choice is the duplicates' (an unconditional gate wins, an admin alone decides), in either order, and the log names both rows."""
    weak, strict = _on("_system", pid="p-a-weak", approval=_rego()), _on("system", pid="p-b-strict")
    rows = [strict, weak] if reverse else [weak, strict]

    with caplog.at_level(logging.WARNING, logger="primer.agent.approval"):
        chosen = await ApprovalResolver(storage=_FakeStorage(rows)).find(toolset_id="system", tool_name="shell_exec")

    assert chosen is not None and chosen.approval.type is ApprovalType.REQUIRED, "the weaker policy of the other spelling shadowed the unconditional gate"
    assert not await _may_decide(chosen, "bob", "user") and await _may_decide(chosen, "root", "admin")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "p-a-weak" in text and "p-b-strict" in text and "'system'" in text, text


@pytest.mark.asyncio
async def test_on_real_storage_a_policy_under_either_spelling_gates_both_and_a_disabled_one_gates_neither(tmp_path):
    """Through the real predicate (``enabled`` and the tool name included): the alias row and the current row are each found from the other's id."""
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    try:
        storage = sp.get_storage(ToolApprovalPolicy)
        await storage.create(ToolApprovalPolicy(
            id="p-alias", toolset_id="_misc", tool_name="get_datetime", approval=RequiredApprovalConfig(),
        ))
        await storage.create(ToolApprovalPolicy(
            id="p-off", toolset_id="system", tool_name="get_datetime", approval=RequiredApprovalConfig(), enabled=False,
        ))
        await storage.create(ToolApprovalPolicy(
            id="p-other-tool", toolset_id="misc", tool_name="uuid_v4", approval=RequiredApprovalConfig(),
        ))
        resolver = ApprovalResolver(storage=storage)

        for asked in ("misc", "_misc"):
            hit = await resolver.find(toolset_id=asked, tool_name="get_datetime")
            assert hit is not None and hit.id == "p-alias", asked
        for asked in ("system", "_system"):
            assert await resolver.find(toolset_id=asked, tool_name="get_datetime") is None, f"a disabled policy gated {asked!r}"
    finally:
        await sp.aclose()
