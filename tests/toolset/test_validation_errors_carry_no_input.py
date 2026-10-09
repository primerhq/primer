"""The tools' "argument validation failed" text never carries the INPUT of the field that did not validate (review of #645, round 3).

``_validation_error_handler`` (REST) dropped ``input`` from every 422 error in round 2. The system tools render a pydantic error the same way, ``json.dumps(exc.errors())``, and
``input`` is in every one of those errors: a ``create_embedding_provider`` whose ``entity.config.url`` is ``http://svc:<password>@emb.local:badport/v1`` answered with the password,
and a ``huggingface`` row without a token answered with the whole config dict (the key beside it). The text goes to the agent that sent the call, which already has the value, but it also goes into
the transcript, the turn log and whatever shows them. ``type``, ``loc`` and ``msg`` stay: they are what a caller needs to fix the call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import BaseModel, HttpUrl, ValidationError

from tests.toolset.test_system_agent_reference_checks import _call_surface
from tests.toolset.test_system_crud_guards import world  # noqa: F401  (world is a fixture)

PASSWORD = "s3cr3t-pw-0123456789"
KEY = "sk-secret-0123456789abcdefghijklmnopqrstuvwxyz"
ROOT = Path(__file__).resolve().parents[2]


def _embedding(provider: str, config: dict) -> dict:
    return {"id": "emb-a", "provider": provider, "models": [{"name": "m"}], "config": config, "limits": {"max_concurrency": 1}}


# ---- through the real system tool -----------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_system_create_with_a_credentialed_url_that_does_not_parse_does_not_echo_it(world) -> None:
    sp, toolset, _ = world

    is_error, answer = await _call_surface("system", sp, toolset, "create_embedding_provider", entity=_embedding("openai", {"url": f"http://svc:{PASSWORD}@emb.local:badport/v1"}))

    assert is_error and answer["type"] == "validation-error", answer
    text = json.dumps(answer)
    assert PASSWORD not in text and "pw-0123" not in text, text
    assert "url" in text, "the caller still needs to know WHICH field is wrong"


@pytest.mark.asyncio
async def test_a_system_create_of_a_huggingface_row_without_a_token_does_not_echo_the_config(world) -> None:
    sp, toolset, _ = world

    is_error, answer = await _call_surface("system", sp, toolset, "create_embedding_provider", entity=_embedding("huggingface", {"url": "http://x.local/v1", "api_key": KEY}))

    assert is_error and answer["type"] == "validation-error", answer
    assert KEY not in json.dumps(answer) and "sk-secret" not in json.dumps(answer), answer


# ---- every tool module's builder of that text ---------------------------------------------------------------------------------------------------------------------------------


class _Probe(BaseModel):
    url: HttpUrl
    count: int


def _validation_error() -> ValidationError:
    try:
        _Probe.model_validate({"url": f"http://svc:{PASSWORD}@emb.local:badport/v1", "count": "not a number " + KEY})
    except ValidationError as exc:
        return exc
    raise AssertionError("the probe model accepted its bad input")


def _builders():
    from primer.toolset import _system_common, misc, trigger, workspaces

    return [
        pytest.param(_system_common._err_from_validation, id="_system_common"),
        pytest.param(misc._err_from_validation, id="misc"),
        pytest.param(trigger._err_validation, id="trigger"),
        pytest.param(workspaces._err_from_validation, id="workspaces"),
    ]


@pytest.mark.parametrize("build", _builders())
def test_the_error_text_of_every_tool_module_drops_the_input_and_keeps_type_loc_and_msg(build) -> None:
    result = build(_validation_error())

    text = result.output if isinstance(result.output, str) else json.dumps(result.output)
    assert PASSWORD not in text and KEY not in text and "pw-0123" not in text, text
    assert result.is_error is True
    start = text.index("[")
    errors = json.loads(text[start : text.rindex("]") + 1])
    assert errors and all("input" not in error and {"type", "loc", "msg"} <= set(error) for error in errors), errors


def test_no_module_renders_pydantic_errors_with_their_input_any_more() -> None:
    """A static guard: ``json.dumps(exc.errors()...)`` is how the text was made, and a new tool module that copies it brings the leak back."""
    unsafe = re.compile(r"json\.dumps\(\s*exc\.errors\(\)")
    offenders = [str(path.relative_to(ROOT)) for path in sorted((ROOT / "primer").rglob("*.py")) if unsafe.search(path.read_text(encoding="utf-8"))]

    assert not offenders, f"these render pydantic errors with their input: {offenders}"


def test_the_shared_filter_keeps_everything_but_the_input() -> None:
    from primer.common.validation_errors import without_input

    errors = [{"type": "missing", "loc": ["body", "token"], "msg": "Field required", "input": {"api_key": KEY}, "ctx": {"error": "x"}, "url": "https://errors.pydantic.dev/x"}]

    assert without_input(errors) == [{"type": "missing", "loc": ["body", "token"], "msg": "Field required", "ctx": {"error": "x"}, "url": "https://errors.pydantic.dev/x"}]
    assert errors[0]["input"] == {"api_key": KEY}, "the argument is not modified"
    assert without_input([]) == []
