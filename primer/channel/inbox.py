"""Inbound side of the channels subsystem."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from primer.channel.adapter import ResponseEnvelope
from primer.model.except_ import BadRequestError
from primer.session.gate_token import StaleGateError, count_gate_token


if TYPE_CHECKING:
    from primer.int.event_bus import EventBus
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)

# env.kind -> the pending-entry tool_name(s) that answer it. A "tool_approval" reply resolves an approval gate (tool_name "_approval").
# A nameless entry is deliberately NOT an approval gate: the gate resolver (primer.session.pending_gates) never reads one, so a name
# match here would publish a reply that no approver check had judged.
_KIND_TOOL_NAMES: dict[str, frozenset[str]] = {
    "ask_user": frozenset({"ask_user"}),
    "tool_approval": frozenset({"_approval"}),
}


def _single_park_tool_call_id(blob: dict[str, Any], yielded: dict[str, Any]) -> str | None:
    """Where a single (non-graph) park's tool_call_id lives.

    Mirrors ``primer.api.routers.yields._tool_call_id_for``: the worker
    writes it at the top level of ``parked_state`` (M3+); older parks may
    have only had it inside ``yielded.resume_metadata``.
    """
    tcid = blob.get("tool_call_id")
    if tcid:
        return tcid
    metadata = yielded.get("resume_metadata") or {}
    return metadata.get("tool_call_id")


def _matching_event_keys(row: Any, env: ResponseEnvelope) -> list[str]:
    """Every pending entry on ``row`` that ``env`` could be answering.

    01a0518f: the row's OWN stored ``event_key`` is the source of truth -
    a graph park's event_key is node-qualified since the contextvar-based
    approval-gate fix, which a bare ``f"{kind}:{session_id}:{tcid}"``
    reconstruction can no longer reproduce. Searches every surface a
    pending item can live on: the single-park blob
    (``parked_state['yielded']``) and, for a graph park, the checkpoint's
    ``pending_agent_yields`` + ``pending_dispatch`` (mirrors
    ``primer.worker.yield_runtime.merge_pending_dispatch``'s combined
    view). Returns event_keys in no particular priority order; the caller
    logs + picks the first on an actual collision (two fan-out siblings
    sharing a raw tool_call_id are still ambiguous from a channel reply
    alone - the wire format has no other field to disambiguate, same as
    the REST respond routes).
    """
    wanted_names = _KIND_TOOL_NAMES.get(env.kind, frozenset())
    blob = getattr(row, "parked_state", None) or {}
    keys: list[str] = []

    yielded = blob.get("yielded") or {}
    if (
        yielded.get("tool_name") in wanted_names
        and _single_park_tool_call_id(blob, yielded) == env.tool_call_id
        and yielded.get("event_key")
    ):
        keys.append(yielded["event_key"])

    checkpoint = blob.get("graph_checkpoint") or {}
    for entry in checkpoint.get("pending_agent_yields") or []:
        if (
            entry.get("tool_name") in wanted_names
            and entry.get("tool_call_id") == env.tool_call_id
            and entry.get("event_key")
        ):
            keys.append(entry["event_key"])
    for entry in checkpoint.get("pending_dispatch") or []:
        if (
            entry.get("kind") in wanted_names
            and entry.get("tool_call_id") == env.tool_call_id
        ):
            # pending_dispatch entries (primer.graph._checkpoint's
            # _toolcall_dispatch_entry) don't carry their own event_key -
            # the graph-native ToolCall node's park key is always the
            # unscoped tool_approval:<session_id>:<tool_call_id> shape
            # (its tool_call_id is a fresh uuid4, never provider-raw, so
            # it was never collision-prone - see primer.graph.
            # workspace_executor._dispatch_toolcall). Reconstruct exactly
            # that shape, not the generic fallback (which would use
            # env.kind's literal string, "tool_approval", correctly here
            # since _approval is the only kind pending_dispatch models).
            keys.append(f"tool_approval:{env.session_id}:{env.tool_call_id}")
    return keys


class ChannelInbox:
    """Single fan-in point for every adapter's inbound responses."""

    def __init__(
        self,
        *,
        event_bus: "EventBus",
        storage_provider: "StorageProvider | None" = None,
    ) -> None:
        self._event_bus = event_bus
        # Optional (01a0518f): enables the stored-event_key LOOKUP path
        # below instead of reconstructing it. None (e.g. a lightweight
        # test app) degrades to the pre-existing reconstruct-only
        # behaviour - never a hard dependency.
        self._storage_provider = storage_provider

    async def handle_response(self, env: ResponseEnvelope) -> None:
        if env.kind not in ("ask_user", "tool_approval"):
            raise BadRequestError(
                f"unknown ResponseEnvelope kind {env.kind!r}"
            )
        # An approval reply is judged against ONE pending gate, resolved ONCE here: the same entry supplies the approver spec the reply
        # is checked against, the event_key it is published to and the data of the audit record. (The check and the publish used to
        # read the row separately through two matchers, so they could land on different entries when two gates share a tool_call_id.)
        captured = (
            await self._resolve_approval_gate(env)
            if env.kind == "tool_approval" else None
        )
        asked = await self._fence_ask_user(env) if env.kind == "ask_user" else None
        if captured is not None:
            self._enforce_approvers(env, captured["gate"])
            # Counted after the approver check, as the REST route does: a refused decision is not counted as one that named the right gate.
            count_gate_token(kind="approval", session_id=env.session_id, token=env.gate_id)
        event_key = (
            captured["gate"].get("event_key") if captured is not None else None
        ) or (asked.get("event_key") if asked is not None else None) or await self._resolve_event_key(env)
        payload: dict = (
            {"response": env.response} if env.kind == "ask_user"
            else {"decision": env.decision, "reason": env.reason}
        )
        # 01a07be5 gate-review-2 finding 4: the gate's data was captured BEFORE
        # publish, not after. A post-publish re-read can resolve a
        # SUCCESSOR gate re-parked under the same tool_call_id (e.g. a
        # nested continuation minting a fresh pending entry that reuses
        # this raw id once the park has already advanced) if the publish
        # is processed synchronously within this same tick. Capturing
        # first guarantees the record describes the gate actually being
        # decided right now, not whatever is pending by the time we look.
        logger.info(
            "channel inbox publishing %s for session=%s tool_call=%s "
            "event_key=%s",
            env.kind, env.session_id, env.tool_call_id, event_key,
        )
        # 01a06b82 gate-review R1: publish FIRST, record AFTER. Writing
        # the record before the publish landed meant a publish that
        # raised (or a listener that never actually advanced the park)
        # left a permanently WRONG "decided" record on the books: the
        # gate then genuinely times out, the resume-time synthesis tries
        # to write the TRUE ("rejected", "timed-out") verdict, loses the
        # gate_event_key race to that earlier wrong write, and that
        # ConflictError used to be swallowed as an ordinary benign dedup
        # no-op. Publishing first means a raise here skips the record
        # entirely (no decision reached the system, nothing to record);
        # write_approval_record's warn_on_decision_mismatch on the
        # resume-time write is the remaining safety net for the case
        # where publish() itself succeeds but the listener still never
        # advances the park.
        await self._event_bus.publish(event_key, payload)
        if captured is not None:
            await self._write_captured_record(
                env, captured, event_key=event_key,
            )

    async def _resolve_approval_gate(self, env: ResponseEnvelope) -> "dict | None":
        """The SPECIFIC pending approval gate ``env`` answers, read ONCE, before anything is published.

        Returns ``{"gate", "agent_id", "parked_at"}``, or ``None`` when there is no gate to judge: no storage_provider wired (a
        lightweight test app; production always wires one), no such session (a chat surface), or no pending ``_approval`` entry for the
        tool_call_id (a park shape this lookup does not model, answered as before). A lookup that FAILS raises (ticket 01a11b64): it
        cannot be shown that the gate is unrestricted, so the reply is refused and nothing is published. So does a row whose checkpoint
        NAMES a gate for the tool_call_id (:func:`_matching_event_keys` finds it) when no pending entry carrying a spec resolves: the
        spec of that gate cannot be read, and the event-key lookup that follows would publish with no check at all.

        A reply that names a gate (``env.gate_id``, C-033) resolves the entry that carries it; one naming a gate that is no longer pending under the
        call id raises :class:`StaleGateError` (nothing published or recorded). One naming none resolves the first match, as before, and is counted.
        """
        if self._storage_provider is None:
            return None
        from primer.model.workspace_session import WorkspaceSession
        from primer.session.pending_gates import resolve_pending_gate

        row = await self._storage_provider.get_storage(WorkspaceSession).get(env.session_id)
        if row is None:
            return None
        blob = getattr(row, "parked_state", None) or {}
        gate = resolve_pending_gate(blob, tool_call_id=env.tool_call_id, kind="_approval", gate_id=env.gate_id)
        if gate is None and env.gate_id is not None and resolve_pending_gate(
            blob, tool_call_id=env.tool_call_id, kind="_approval",
        ) is not None:
            # The call id is pending but not under the gate the button named: the button is from an earlier round (C-033). Nothing is decided.
            count_gate_token(kind="approval", session_id=env.session_id, token=env.gate_id, stale=True)
            logger.warning(
                "channel inbox: refused a stale %s reply for session=%s tool_call=%s: the gate it named has been replaced",
                env.decision, env.session_id, env.tool_call_id,
            )
            raise StaleGateError("approval")
        if gate is None:
            if _matching_event_keys(row, env) or self._is_nameless_park_at_reconstructed_key(row, env):
                from primer.session.approvers import ApproverRefusedError

                logger.warning(
                    "channel inbox: refused a %s reply for session=%s tool_call=%s: the park names the gate but no pending "
                    "entry carries its approver spec", env.decision, env.session_id, env.tool_call_id,
                )
                raise ApproverRefusedError(
                    "this approval gate's approver spec cannot be read; decide it in the console as an approver or an admin"
                )
            return None
        return {
            "gate": gate,
            "agent_id": getattr(row.binding, "agent_id", None),
            "parked_at": getattr(row, "parked_at", None),
        }

    async def _fence_ask_user(self, env: ResponseEnvelope) -> "dict | None":
        """Refuse an ask_user reply that names a prompt which is no longer the pending one (C-033), before anything is published; return the gate it answers.

        Only a reply that carries a token is judged. A reply that names none answers whatever is pending (a thread reply whose correlation row
        predates gate ids has no token to bring back), so it is accepted, and counted ``absent`` when it reaches a pending ask_user prompt, so the
        flip to refusing tokenless replies can be scheduled from one number. A call id with no pending ask_user entry at all is left to the lookup
        that follows and is not counted.

        The returned entry is the SPECIFIC gate the reply resolved to (the one carrying the named token, else the first match by raw id), and its
        own ``event_key`` is what the reply is published to: two fan-out siblings that share a raw tool_call_id wait on different keys, and the lookup
        that used to follow matched by the raw id alone and took the first (C-033 round 2, PR 3). ``None`` when there is nothing to resolve.
        """
        if self._storage_provider is None:
            return None
        from primer.model.workspace_session import WorkspaceSession
        from primer.session.pending_gates import resolve_pending_gate

        row = await self._storage_provider.get_storage(WorkspaceSession).get(env.session_id)
        if row is None:
            return None
        blob = getattr(row, "parked_state", None) or {}
        if env.gate_id is None:
            gate = resolve_pending_gate(blob, tool_call_id=env.tool_call_id, kind="ask_user")
            if gate is not None:
                count_gate_token(kind="ask_user", session_id=env.session_id, token=None)
            return gate
        gate = resolve_pending_gate(blob, tool_call_id=env.tool_call_id, kind="ask_user", gate_id=env.gate_id)
        if gate is not None:
            count_gate_token(kind="ask_user", session_id=env.session_id, token=env.gate_id)
            return gate
        if resolve_pending_gate(blob, tool_call_id=env.tool_call_id, kind="ask_user") is not None:
            count_gate_token(kind="ask_user", session_id=env.session_id, token=env.gate_id, stale=True)
            logger.warning(
                "channel inbox: refused a stale ask_user reply for session=%s tool_call=%s: the prompt it named has been replaced",
                env.session_id, env.tool_call_id,
            )
            raise StaleGateError("ask_user")
        return None

    @staticmethod
    def _is_nameless_park_at_reconstructed_key(row: Any, env: ResponseEnvelope) -> bool:
        """True for a park with no ``tool_name`` whose own event key is the one the fallback publishes to.

        A nameless park is not matched by name (the gate resolver never reads one), but the fallback key is
        ``<kind>:<session_id>:<tool_call_id>``: when the park's key IS that, publishing to it wakes the park with no check of a spec
        nobody read. Such a reply is refused.
        """
        yielded = (getattr(row, "parked_state", None) or {}).get("yielded") or {}
        return (
            not yielded.get("tool_name")
            and yielded.get("event_key") == f"{env.kind}:{env.session_id}:{env.tool_call_id}"
        )

    def _enforce_approvers(self, env: ResponseEnvelope, gate: dict) -> None:
        """Refuse a reply that the gate's stamped approver spec does not admit (ticket 01a11b64), BEFORE anything is published.

        The envelope carries only a messaging-platform user id, which maps to no primer account, so the decider is UNIDENTIFIED and
        :func:`primer.session.approvers.may_decide` admits it only on a gate with no restriction; a restricted gate (specific users,
        a role, admins only, the duplicate-policy fallback) is decided in the console. The spec is read from the SPECIFIC gate the
        reply names, exactly as the REST respond route does.
        """
        from primer.session.approvers import ensure_may_decide

        try:
            ensure_may_decide(gate.get("resume_metadata") or {}, username=None, role=None)
        except Exception:
            logger.warning(
                "channel inbox: refused a %s reply for session=%s tool_call=%s: the gate is routed to specific approvers and a "
                "messaging-platform user is not one primer can identify (platform metadata: %s)",
                env.decision, env.session_id, env.tool_call_id, env.platform_metadata,
            )
            raise

    async def _write_captured_record(
        self, env: ResponseEnvelope, captured: dict, *, event_key: str,
    ) -> None:
        """Persist a durable ToolApprovalRecord for a channel-answered gate,
        using data captured BEFORE the publish (see
        :meth:`_resolve_approval_gate`).

        01a06b82: the REST respond route (tool_approval.py's
        _publish_decision) has written this record at decision time since
        01a068da; the channel surface never did, so a decision answered
        through Slack/Discord/etc. had no durable record unless the
        session later happened to resume (the resume-time fallback,
        write_approval_record_for_graph / the agent-path equivalent).

        This is advisory ONLY, unlike the REST route's write: that route
        stamps parked_status durably (the guarded parked -> resumable
        flip) BEFORE writing the record, so it always has a confirmed-real
        park to describe. handle_response has no equivalent durable step
        of its own -- it publishes straight onto the event bus, and the
        durable flip happens elsewhere (the bus listener / durable event
        dispatcher). So there is no natural place to hang a hard failure
        off: ANY problem here is logged and swallowed. Called AFTER the
        publish (R1): writing this BEFORE the publish used to mean a
        publish that raised (or a listener that never actually advanced
        the park) left a permanently wrong "decided" record on the books
        with nothing to correct it. A missed record here is recoverable
        (the resume-time write is still a backstop, now with its own
        disagreement check for exactly this residual race - see
        write_approval_record's warn_on_decision_mismatch); a missed or
        delayed wake would not have been.
        """
        try:
            from primer.agent.approval_record import (
                record_from_parked_blob,
                write_approval_record,
            )
            from primer.model.tool_approval import ToolApprovalRecord
            from primer.worker.yield_runtime import classify_approval_payload

            gate = captured["gate"]
            decision, reason, _kind = classify_approval_payload(
                {"decision": env.decision, "reason": env.reason},
            )
            record = record_from_parked_blob(
                blob={
                    "tool_call_id": env.tool_call_id,
                    "yielded": {"resume_metadata": gate.get("resume_metadata") or {}},
                },
                decision=decision,
                reason=reason,
                agent_id=captured["agent_id"],
                session_id=env.session_id,
                requested_at=captured["parked_at"],
                gate_event_key=gate.get("event_key") or event_key,
            )
            await write_approval_record(
                self._storage_provider.get_storage(ToolApprovalRecord), record,
            )
        except Exception:  # noqa: BLE001 -- advisory; must never block the wake publish
            logger.exception(
                "channel inbox: best-effort approval record write failed "
                "for session=%s tool_call=%s", env.session_id, env.tool_call_id,
            )

    async def _resolve_event_key(self, env: ResponseEnvelope) -> str:
        """The event_key to publish for ``env``.

        01a0518f: LOOKS UP the session's own stored pending-entry
        event_key (see :func:`_matching_event_keys`) instead of
        reconstructing ``f"{kind}:{session_id}:{tool_call_id}"`` - a
        graph park's key is node-qualified now, which the bare
        reconstruction can't reproduce. Falls back to the reconstructed
        (pre-fix) shape whenever the lookup can't help: no
        storage_provider wired, the lookup itself fails, ``env.session_id``
        doesn't resolve to a WorkspaceSession row at all (e.g. a chat
        surface response, which never uses a node-qualified key), or no
        pending entry matches - every one of those is exactly today's
        behaviour, so this is purely additive.
        """
        fallback = f"{env.kind}:{env.session_id}:{env.tool_call_id}"
        if self._storage_provider is None:
            return fallback
        try:
            from primer.model.workspace_session import WorkspaceSession

            row = await self._storage_provider.get_storage(
                WorkspaceSession,
            ).get(env.session_id)
        except Exception:  # noqa: BLE001 -- advisory; never block delivery
            logger.exception(
                "channel inbox: session lookup failed for %s; falling "
                "back to a reconstructed event_key", env.session_id,
            )
            return fallback
        if row is None:
            return fallback
        matches = _matching_event_keys(row, env)
        if not matches:
            return fallback
        if len(matches) > 1:
            logger.warning(
                "channel inbox: %d pending entries match "
                "tool_call_id=%r kind=%r on session %s; resolving the "
                "first",
                len(matches), env.tool_call_id, env.kind, env.session_id,
            )
        return matches[0]


__all__ = ["ChannelInbox"]
