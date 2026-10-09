"""A compaction round's tool failure is fed back to the summariser without the credential its text carried (#676 review round 1, B3).

Compaction runs its tool loop outside the turn's park machinery and swallows a tool's exception into an error result for the next round (and an event for
the live tap). The exception text is a library's own and can quote the URL or header it was given.
"""

from __future__ import annotations

from primer.model.chat import ExtendedEvent, ToolCallPart, ToolResultPart

from tests.agent.test_compaction_tools import _FakeToolManager, _run, _ScriptedLLM, _text_round, _tool_round

BEARER = "sk-bearer-ABCDEFGH12345678"
URL = "https://svc-user:hunter2pw@gateway.internal/v1/chat?api_key=SKSECRET123456"


class _Leaky(_FakeToolManager):
    async def execute(self, call: ToolCallPart, *, principal: str | None = None) -> ToolResultPart:
        self.executed.append(call)
        raise RuntimeError(f"connection refused for url '{URL}' with Authorization: Bearer {BEARER}")


def _clean(text: str) -> bool:
    return "hunter2pw" not in text and "SKSECRET123456" not in text and BEARER not in text


async def test_a_compaction_tool_failure_is_fed_back_and_streamed_without_credentials() -> None:
    llm = _ScriptedLLM([_tool_round("c1", "workspace__write", {"path": "x"}), _text_round("done")])

    _result, events = await _run(llm, _Leaky())

    streamed = [e.extended.output for e in events if isinstance(e, ExtendedEvent) and getattr(e.extended, "error", False)]
    assert streamed and all(_clean(text) for text in streamed), "the live tap event carried a credential"
    fed_back = [
        p.output for call in llm.calls for m in call["messages"] for p in m.parts if isinstance(p, ToolResultPart) and p.error
    ]
    assert fed_back and all(_clean(text) for text in fed_back), "the summariser's next round saw a credential"
