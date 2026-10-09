"""A provider-controlled error ``code`` is a short identifier or nothing (security ticket 01a11fbc-ffea).

The OpenResponses ``error`` event's ``code`` and the OpenAI SDK exception's ``code`` (the body's ``error.code``) were passed through whole: unbounded, and
with any character the provider chose. They reach the ERROR record, ``TurnStreamFailure.ended_detail_code`` and the session's ``ended_detail``,
``last_turn_error.code``, the ``session.turn_failed`` payload and, for a graph, the ``X-Primer-Graph-Ended-Detail`` git trailer, whose line is
``f"{key}: {value}"``: a code holding a newline injected trailer lines the history parser reads. A code now survives only when it matches
``[A-Za-z0-9_.:-]{1,64}`` (what every real provider and every code of ours looks like), else it becomes the fixed sentinel ``provider_error``. NOT
``None``: a code that IS set and that nobody classified ENDS an interactive session and is only config-eligible for aggregated failover, while ``None``
makes the callers classify the failure by its class (a forged code on a 5xx would then REST the session and fail over under the strict policy: the rule
a hostile code is judged by must not flip). An empty code is no code. The rule is in two places every code passes: ``Error.code`` and ``PrimerError.code``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from primer.common.openai_errors import classify_openai_exception
from primer.llm.openresponses import _StreamState, _translate_event
from primer.model.chat import Error as ChatError
from primer.model.except_ import BadRequestError, PrimerError, ServerError
from tests.llm.test_failure_messages import _openai_sdk_error

FORGED = "rate_limited\nX-Primer-Graph: forged"
LONG = "a" * 65


@pytest.mark.parametrize("code", ["rate_limit", "context_length_exceeded", "server_error", "a.b:c-d_9", "x", "a" * 64])
def test_a_real_code_is_kept(code):
    assert ChatError(code=code, message="m", fatal=True).code == code
    assert PrimerError("m", code=code).code == code


@pytest.mark.parametrize(
    "code", [FORGED, "a\rb", "with space", "tab\there", "semi;colon", LONG, "ünïcode", "a/b", "{x}", "x\x00y"],
    ids=["newline", "cr", "space", "tab", "semicolon", "65-chars", "non-ascii", "slash", "braces", "nul"],
)
def test_a_code_that_is_not_an_identifier_becomes_the_sentinel(code):
    assert ChatError(code=code, message="m", fatal=True).code == "provider_error"
    assert PrimerError("m", code=code).code == "provider_error"
    assert BadRequestError("m", code=code, status_code=400).code == "provider_error"


def test_an_empty_code_is_no_code():
    assert ChatError(code="", message="m", fatal=True).code is None
    assert PrimerError("m", code="").code is None


@pytest.mark.parametrize(
    "code,expected", [(429, "429"), (-32603, "-32603"), (True, "provider_error"), (["a"], "provider_error"), ({"x": 1}, "provider_error"), (1.5, "provider_error")],
)
def test_a_code_that_is_not_a_string_is_not_a_validation_error(code, expected):
    """A proxy can send ``"code": 429`` (an int): the field is declared ``str | None`` and a ``mode="after"`` validator never runs (pydantic fails the
    type first), so the error event would raise instead of reporting the failure."""
    assert ChatError(code=code, message="m", fatal=True).code == expected
    assert PrimerError("m", code=code).code == expected


def test_no_code_stays_no_code():
    assert ChatError(message="m", fatal=True).code is None
    assert PrimerError("m").code is None


def test_the_str_of_an_error_does_not_carry_a_forged_code():
    assert "X-Primer-Graph" not in str(ServerError("boom", code=FORGED, status_code=500))


def test_the_openresponses_error_event_does_not_forward_a_forged_code():
    event = SimpleNamespace(type="error", code=FORGED, message="upstream said no")

    (error,) = _translate_event(event, _StreamState())

    assert isinstance(error, ChatError) and error.code == "provider_error" and error.message == "upstream said no"


def test_the_openresponses_error_event_keeps_a_real_code():
    (error,) = _translate_event(SimpleNamespace(type="error", code="rate_limited", message="slow down"), _StreamState())

    assert error.code == "rate_limited"


@pytest.mark.asyncio
async def test_the_openai_classifier_does_not_forward_a_forged_body_code():
    exc = await _openai_sdk_error(400, {"error": {"message": "bad", "code": FORGED, "type": "invalid_request_error"}})

    assert classify_openai_exception(exc).code == "provider_error"


@pytest.mark.asyncio
async def test_the_context_overflow_code_still_reaches_the_veto():
    """The overflow check reads ``context_length_exceeded``; a real code must not be a casualty of the rule."""
    exc = await _openai_sdk_error(400, {"error": {"message": "too long", "code": "context_length_exceeded", "type": "invalid_request_error"}})

    assert classify_openai_exception(exc).code == "context_length_exceeded"


def test_a_failure_with_a_forged_code_is_reported_as_an_unclassified_provider_error_and_ends_the_session():
    """The forged code must not flip the rule it is judged by: it is SET and nobody classified it, so an interactive session ENDS (it did, with the
    forged text as the code), where ``None`` would have classified it by class (``server_error``) and RESTED it."""
    from primer.session.dispatch import _RESTING_FAILURE_CODES, _failure_code

    code = _failure_code(ServerError("boom", code=FORGED, status_code=500))

    assert code == "provider_error"
    assert code not in _RESTING_FAILURE_CODES


def test_a_forged_code_is_not_transient_for_the_strict_failover_policy():
    from primer.llm.aggregated import FailoverClasses, _yielded_eligible

    code = ChatError(code=FORGED, message="m", fatal=True).code

    assert _yielded_eligible(code, FailoverClasses.TRANSIENT) is False       # None would have been eligible: failover under the strict policy
    assert _yielded_eligible(code, FailoverClasses.TRANSIENT_AND_CONFIG) is True
    assert _yielded_eligible(None, FailoverClasses.TRANSIENT) is True        # an adapter that sends no code is still classified as before
