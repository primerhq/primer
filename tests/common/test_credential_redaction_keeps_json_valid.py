"""Masking credentials in text that is already a JSON envelope leaves valid JSON (#676 review round 1, B2).

The query-key value class of ``redact_url_secrets`` was ``[^&#\\s'"<>]+``: it ate the backslash of an escaped quote, so ``...?api_key=K\\"`` became
``...?api_key=[REDACTED]`` and the closing quote's escape was gone: the envelope ``invoke_agent`` returns, ``ChildGraphFailed.result_json()`` and a tool's
``err()`` envelope were invalid JSON after the manager redacted them a second time. The class now stops at a backslash, and these pin that masking text and
masking its JSON encoding agree.
"""

from __future__ import annotations

import json

import pytest

from primer.agent.tool_manager import _without_credentials
from primer.common.log import redact_credentials, redact_url_secrets
from primer.graph.invoke_graph import ChildGraphFailed
from primer.model.chat import ToolResultPart
from primer.toolset._helpers import err

SECRET = "SKSECRET123456"
MESSAGE = f'upstream said: GET "https://h/v1?api_key={SECRET}" -> 401'


def test_masking_a_json_envelope_leaves_valid_json() -> None:
    envelope = json.dumps({"error": MESSAGE})

    out = redact_credentials(envelope)

    assert SECRET not in out
    assert json.loads(out)["error"].endswith('" -> 401'), "the closing quote of the URL survived"


def test_the_invoke_agent_envelope_is_valid_json_after_the_manager_masks_it() -> None:
    envelope = err(f"subagent 'a' LLM stream failed: {redact_credentials(MESSAGE)}", error_type="provider-error").output

    out = _without_credentials(ToolResultPart(id="c", output=envelope, error=True)).output

    assert SECRET not in out and json.loads(out)["type"] == "provider-error"


def test_a_child_graph_failure_body_is_valid_json_after_the_manager_masks_it() -> None:
    body = ChildGraphFailed(code="tool_execution_failed", message=MESSAGE, node_id="n").result_json()

    out = _without_credentials(ToolResultPart(id="c", output=body, error=True)).output

    assert SECRET not in out and json.loads(out)


def test_a_tools_own_err_envelope_is_valid_json_after_the_manager_masks_it() -> None:
    out = _without_credentials(ToolResultPart(id="c", output=err(f"fetch failed: {MESSAGE}").output, error=True)).output

    assert SECRET not in out and json.loads(out)


@pytest.mark.parametrize("text", [MESSAGE, f"{MESSAGE} and Authorization: Bearer sk-bearer-ABCDEFGH12345678", "plain text, nothing to mask"])
def test_masking_is_idempotent_across_the_json_boundary(text: str) -> None:
    once = redact_credentials(text)

    assert redact_credentials(once) == once
    assert redact_credentials(json.dumps(once)) == json.dumps(once), "masking the encoding of masked text changes nothing"
    assert json.loads(redact_credentials(json.dumps(text))) == once, "masking the encoding equals encoding the masking"


def test_the_log_filters_masker_stops_at_a_backslash_too() -> None:
    assert redact_url_secrets('x ?api_key=abc\\"y') == 'x ?api_key=[REDACTED]\\"y'
