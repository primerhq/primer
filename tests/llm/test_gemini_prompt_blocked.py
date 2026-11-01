"""A Gemini prompt blocked by safety ends with a Done (Part 2 T51, AG-M20).

A chunk with no candidates and a ``prompt_feedback.block_reason`` produced no Done and no error: the turn ended with no
reply and an interactive session went ENDED/completed instead of resting with ``content_filter``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

from primer.llm.gemini import _StreamState, _translate_chunk
from primer.model.chat import Done, StreamStart, Usage
from tests.llm.test_adapter_characterization import _OK, _collect, _provider, _route
import primer.llm.gemini as gemini_mod


def test_the_translator_ends_a_blocked_prompt_with_content_filter() -> None:
    chunk = NS(response_id="r", candidates=None,
               prompt_feedback=NS(block_reason="SAFETY", block_reason_message="blocked"),
               usage_metadata=NS(prompt_token_count=12, candidates_token_count=None,
                                 cached_content_token_count=None, thoughts_token_count=None))
    assert _translate_chunk(chunk, _StreamState(), "gemini-x") == [
        StreamStart(request_id="r", model="gemini-x"),
        Usage(input_tokens=12, output_tokens=0, cumulative=True),
        Done(stop_reason="content_filter", raw_reason="prompt_blocked:SAFETY"),
    ]


async def test_the_adapter_ends_a_blocked_prompt_with_content_filter(monkeypatch) -> None:
    body = b"data: " + json.dumps({
        "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"},
        "usageMetadata": {"promptTokenCount": 12, "totalTokenCount": 12}, "responseId": "r1",
    }).encode() + b"\r\n\r\n"
    _route(monkeypatch, "gemini", [(200, body, _OK)])
    events = await _collect(gemini_mod.GeminiLLM(_provider("gemini")), "gemini")
    assert events == [
        StreamStart(request_id="r1", model="gemini-x"),
        Usage(input_tokens=12, output_tokens=0, cumulative=True),
        Done(stop_reason="content_filter", raw_reason="prompt_blocked:PROHIBITED_CONTENT"),
    ]
