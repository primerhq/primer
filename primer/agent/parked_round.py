"""The tool round a park left unfinished, and how a resume finishes it (ticket 01a15327).

A model can ask for several tool calls in ONE round: one assistant message holding several ``tool_use`` blocks. The loop runs them one after
another (:func:`primer.agent.loop._dispatch_tool_calls`), and when one of them asks to wait (an approval gate, ``ask_user``, a timer, a nested
run that parks) the park leaves in the middle of the round: the calls before it have run and have answers, the calls after it have not run. A
provider that pairs every ``tool_use`` with a result (Anthropic, OpenAI) rejects the next request if one is missing, so the park carries the round
and the resume finishes it before the model is called again:

* the park: ``_dispatch_tool_calls`` stamps the park exception as it leaves the batch (``YieldToWorker.completed_results``: the answers of the
  calls before the one that asked to wait; ``remaining_calls``: the calls after it), and the park writers store that as :class:`ParkedRound`
  says (``ParkedState`` for an agent session, the ``pending_agent_yields`` entry of a graph checkpoint for a graph agent node);
* the resume: once the parked call has its answer (the decision applied to a gate, the reply to an ``ask_user``, the result of a nested run),
  :func:`finish_round` runs the remaining calls in order and hands back EVERY answer of the round, once and in order, for the one ``tool``
  message the model is sent next. A remaining call that asks to wait leaves with the round as it then stands, and the resume parks again on it.

A park written before rounds were carried stores none: its round is empty, and the parked call alone is answered, as it always was.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from primer.common.log import redact_credentials
from primer.model.chat import ExtendedEvent, ToolCallPart, ToolResultPart, _ClientAction, _ExecutorToolResult
from primer.model.yield_ import YieldToWorker


if TYPE_CHECKING:
    from primer.agent.tool_manager import ToolExecutionManager


logger = logging.getLogger(__name__)

# The answer of a remaining call when the resumed run has no tool manager to run it with (no production resume lacks one).
_NO_MANAGER_REFUSAL = "not run: the resumed run had no tool manager to run the call with"


@dataclass(frozen=True)
class ParkedRound:
    """The part of a tool round a park left: ``completed``, the answers of the round's calls BEFORE the call that parked; ``remaining``, the
    round's calls AFTER it, which have not run. Both in the round's order, and both empty for a park whose call was the round's only one or that
    carried no round (written before rounds were carried)."""

    completed: tuple[ToolResultPart, ...] = ()
    remaining: tuple[ToolCallPart, ...] = ()

    @classmethod
    def of_park(cls, park: YieldToWorker) -> "ParkedRound":
        """The round ``park`` left, as ``_dispatch_tool_calls`` stamped it on the way out (empty when nothing stamped it)."""
        return cls(
            completed=tuple(getattr(park, "completed_results", None) or ()),
            remaining=tuple(getattr(park, "remaining_calls", None) or ()),
        )

    @classmethod
    def from_jsonable(cls, completed: Iterable[dict[str, Any]] | None, remaining: Iterable[dict[str, Any]] | None) -> "ParkedRound":
        """The round a park record stored (:meth:`completed_jsonable`, :meth:`remaining_jsonable`); ``None`` (a record from before) is empty."""
        return cls(
            completed=tuple(ToolResultPart.model_validate(part) for part in completed or ()),
            remaining=tuple(ToolCallPart.model_validate(call) for call in remaining or ()),
        )

    def completed_jsonable(self) -> list[dict[str, Any]]:
        return [part.model_dump(mode="json") for part in self.completed]

    def remaining_jsonable(self) -> list[dict[str, Any]]:
        return [call.model_dump(mode="json") for call in self.remaining]

    def stored_fields(self) -> dict[str, list[dict[str, Any]]]:
        """The round as the park records store it, under the names they share: ``completed_results`` and ``remaining_calls``."""
        return {"completed_results": self.completed_jsonable(), "remaining_calls": self.remaining_jsonable()}

    @property
    def is_empty(self) -> bool:
        return not self.completed and not self.remaining

    def stamp(self, park: YieldToWorker) -> YieldToWorker:
        """Stamp this round on ``park``: the parked call still waits, and the round around it is unchanged (``completed`` before it, ``remaining`` after).

        For a call that paused AGAIN on resume (an approved call that pauses for its own event): a nested run inside it may have stamped the park with
        ITS round, and the park writer must read the round of the call this resume answers.
        """
        park.batch_index = len(self.completed)
        park.completed_results = list(self.completed)
        park.remaining_calls = list(self.remaining)
        return park


def _result_event(part: ToolResultPart) -> ExtendedEvent:
    """The ``tool_result`` event of an answer, as the loop yields one for every call of a finished batch."""
    return ExtendedEvent(
        extended=_ExecutorToolResult(call_id=part.id, output=part.output, error=part.error, metadata=part.metadata)
    )


async def finish_round(
    parked_round: ParkedRound,
    answers: Sequence[ToolResultPart],
    *,
    tool_manager: "ToolExecutionManager | None",
    principal: str | None,
    results_out: list[ToolResultPart],
) -> AsyncIterator[ExtendedEvent]:
    """Finish the round ``parked_round`` was left at, given ``answers``, the answer of the call that parked.

    Runs every remaining call, in the round's order, with ``tool_manager`` (the RESUMED run's own manager, so each call meets its own role floor and
    approval gate: none of them was approved) and ``principal`` (the string the resumed run dispatches with). Each call runs as the live batch runs
    it (``_dispatch_tool_calls``): a notifying call is delivered, a ``PrimerError`` is an error result. A call that raises anything else (a sign-in
    the toolset needs, a bug) is answered with an error result too, logged: the resume cannot move the session to waiting in the middle of a round,
    and every call of the round must be answered.

    Yields, per call it runs, what the live batch yields for it: the ``_ClientAction`` of a notifying call, then the ``_ExecutorToolResult`` of its
    answer. The answers in ``parked_round.completed`` and ``answers`` were reported already and are not yielded again.

    When it returns, ``results_out`` holds EVERY answer of the round, once and in order: the completed ones, ``answers``, one per remaining call.
    A remaining call that asks to wait leaves as its :class:`~primer.model.yield_.YieldToWorker`, stamped with the round as it then stands
    (``completed_results``: everything answered before it, ``remaining_calls``: the calls after it, ``batch_index``: its position in the round), so
    the resume parks again carrying it; ``results_out`` is left as it was then.
    """
    # Function-local: primer.agent.loop imports the tool manager and its transitive graph.
    from primer.agent.loop import _dispatch_tool_calls

    done: list[ToolResultPart] = [*parked_round.completed, *answers]
    remaining = list(parked_round.remaining)
    if remaining and tool_manager is not None:
        # The loop lists the catalogue at the top of every turn, and the batch reads what it indexes (which tools are notifying) before it
        # runs a call. A resumed run's manager is new and has listed nothing yet: list first (the manager keeps the catalogue it built). A
        # listing that fails is not this round's to report: each call then fails on its own and is answered with an error result.
        try:
            await tool_manager.list_tools(principal=principal)
        except Exception:  # noqa: BLE001 - see above
            logger.warning("resume: the resumed run could not list its tools before finishing the parked round", exc_info=True)
    for index, call in enumerate(remaining):
        actions: list[_ClientAction] = []
        if tool_manager is None:
            logger.error("resume: no tool manager to run tool call %r (%s) of the parked round; answering it not run", call.id, call.name)
            part = ToolResultPart(id=call.id, output=_NO_MANAGER_REFUSAL, error=True)
        else:
            try:
                messages = await _dispatch_tool_calls([call], tool_manager=tool_manager, principal=principal, actions_out=actions)
            except YieldToWorker as park:
                ParkedRound(completed=tuple(done), remaining=tuple(remaining[index + 1:])).stamp(park)
                raise
            except Exception as exc:  # noqa: BLE001 - fail-closed synthesis: every call of the round is answered
                logger.exception("resume: tool call %r (%s) of the parked round raised; answering it with an error result", call.id, call.name)
                part = ToolResultPart(id=call.id, output=redact_credentials(str(exc)) or type(exc).__name__, error=True)
            else:
                part = next(p for message in messages for p in message.parts if isinstance(p, ToolResultPart))
        for action in actions:
            yield ExtendedEvent(extended=action)
        yield _result_event(part)
        done.append(part)
    results_out.extend(done)


__all__ = ["ParkedRound", "finish_round"]
