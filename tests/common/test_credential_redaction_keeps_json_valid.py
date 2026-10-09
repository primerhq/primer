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


# ---- a secret with a non-ASCII character, in text that is already JSON (#676 review round 2) -------------------------------------------------------
# json.dumps writes a non-ASCII character as a ``\uXXXX`` escape, which holds a backslash. The value class stops at a backslash (so an escaped closing
# quote survives), and a class that stopped at ANY backslash kept the tail of the secret after the first escape. It consumes a backslash that starts a
# four-hex-digit unicode escape and stops at every other one.

NON_ASCII_SECRETS = ["SK\u00e9TAIL-ONE-123456", "SK\u4e2d\u6587TAIL-TWO-123456", "SK\U0001F680TAIL-THREE-123456"]


@pytest.mark.parametrize("secret", NON_ASCII_SECRETS, ids=["latin", "cjk", "astral"])
def test_a_non_ascii_secret_is_masked_whole_in_json_encoded_text(secret: str) -> None:
    envelope = json.dumps({"error": f'upstream said: GET "https://h/v1?api_key={secret}" -> 401'})
    assert "\\u" in envelope, "the setup must put a unicode escape inside the secret"

    out = redact_credentials(envelope)

    assert "TAIL" not in out, f"the tail of the secret survived: {out}"
    assert json.loads(out)["error"].endswith('" -> 401'), "the closing quote of the URL survived and the result is valid JSON"


@pytest.mark.parametrize("secret", NON_ASCII_SECRETS, ids=["latin", "cjk", "astral"])
def test_masking_the_encoded_text_agrees_with_masking_the_text(secret: str) -> None:
    text = f'upstream said: GET "https://h/v1?api_key={secret}" -> 401'

    assert json.loads(redact_credentials(json.dumps({"e": text})))["e"] == redact_credentials(text)


def test_an_escaped_quote_after_the_secret_is_still_not_consumed() -> None:
    """The control for the class: a backslash that does NOT start a unicode escape ends the value (``\\"`` closes the URL's quote)."""
    out = redact_credentials(json.dumps({"e": f'x "https://h/v1?api_key={SECRET}" y'}))

    assert SECRET not in out and json.loads(out)["e"] == 'x "https://h/v1?api_key=[REDACTED]" y'


# ---- a typed refusal reason ----------------------------------------------------------------------------------------------------------------------


def test_a_refusal_reason_is_masked_when_it_is_credential_shaped() -> None:
    """The reason is what a person typed and is not redacted where ``result_json`` builds it, but the body is delivered in an ERROR result, which masks it."""
    from primer.graph.base import _ToolApprovalRejected

    code = _ToolApprovalRejected(kind="rejected").ended_detail_code
    body = ChildGraphFailed(code=code, message=f"no: call https://h/v1?api_key={SECRET} instead", node_id="n", tool_name="t").result_json()
    assert json.loads(body)["rejected"] is True and SECRET in body, "the setup must be a refusal that carries the secret"

    out = _without_credentials(ToolResultPart(id="c", output=body, error=True)).output

    assert SECRET not in out and json.loads(out)["reason"] == "no: call https://h/v1?api_key=[REDACTED] instead"


def test_an_ordinary_refusal_reason_is_untouched() -> None:
    from primer.graph.base import _ToolApprovalRejected

    code = _ToolApprovalRejected(kind="rejected").ended_detail_code
    body = ChildGraphFailed(code=code, message="not now, ask me tomorrow", node_id="n", tool_name="t").result_json()

    assert _without_credentials(ToolResultPart(id="c", output=body, error=True)).output == body
