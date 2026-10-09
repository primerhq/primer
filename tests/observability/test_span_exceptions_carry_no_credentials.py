"""A failed span exports no credential (security ticket 01a1201c-8918, item 2; #676 security review).

``tracer.start_as_current_span`` records the exception that escapes its ``with`` block (type, MESSAGE and STACKTRACE as a span event, and the message again as
the status description), and the tool and LLM spans also called ``span.record_exception(exc)`` themselves. With an OTLP exporter configured the raw text of a
tool's exception (httpx prints a request URL whole, ``user:password@`` and ``?api_key=`` included, and a library may echo an ``Authorization`` header) went to
the collector. ``primer.observability.tracing.span`` turns the SDK's own recording off and records the failure itself: the exception type and the message with
credentials masked (URL credentials, Bearer and Basic tokens), and no stacktrace (it ends in the same message). A credential-free message is recorded as it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import primer.workspace  # noqa: F401  (resolves ToolCallContext's forward reference)
from primer.agent.tool_manager import ToolExecutionManager, invoke_one
from primer.model.chat import ToolCallPart
from primer.model.principal import PrincipalRef
from primer.observability import tracing

LEAKY = (
    "ConnectError: All connection attempts failed for url 'https://svc-user:hunter2pw@gateway.internal/v1/x?api_key=SKSECRET123456' "
    "(retry with Bearer sk-abcdefgh12345678 or Basic dXNlcjpwYXNzd29yZA==)"
)
SECRETS = ("hunter2pw", "SKSECRET123456", "sk-abcdefgh12345678", "dXNlcjpwYXNzd29yZA==")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def exporter(monkeypatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_provider", provider)
    return exporter


def _everything_exported(exporter: InMemorySpanExporter) -> str:
    """Every string a collector would receive for the finished spans: attributes, events (with their attributes) and the status description."""
    out = []
    for span in exporter.get_finished_spans():
        out.append(json.dumps(dict(span.attributes or {}), default=str))
        out.append(str(span.status.description))
        for event in span.events:
            out.append(event.name)
            out.append(json.dumps(dict(event.attributes or {}), default=str))
    return "\n".join(out)


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} was exported: {text!r}"


class _Boom:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def call(self, *, tool_name, arguments, principal, ctx):
        raise self._exc


@pytest.mark.asyncio
async def test_a_tool_that_raises_exports_no_credential_on_the_direct_path(exporter) -> None:
    with pytest.raises(RuntimeError):
        await invoke_one(provider=_Boom(RuntimeError(LEAKY)), tool_name="fetch", arguments={}, principal=None)

    text = _everything_exported(exporter)
    _assert_clean(text)
    assert "gateway.internal" in text, "what failed is still said"
    assert "RuntimeError" in text, "and which exception it was"


@pytest.mark.asyncio
async def test_an_exception_out_of_the_managers_execute_exports_no_credential(exporter, monkeypatch) -> None:
    manager = ToolExecutionManager(toolset_providers={}, initiated_by=PrincipalRef.system())  # type: ignore[arg-type]

    async def _raises(*_a, **_k):
        raise RuntimeError(LEAKY)

    monkeypatch.setattr(manager, "_execute_inner", _raises)

    with pytest.raises(RuntimeError):
        await manager.execute(ToolCallPart(id="c", name="a__foo", arguments={}))

    text = _everything_exported(exporter)
    _assert_clean(text)
    assert "gateway.internal" in text


@pytest.mark.asyncio
async def test_the_span_is_marked_failed_and_carries_the_exception_event(exporter) -> None:
    with pytest.raises(ValueError):
        await invoke_one(provider=_Boom(ValueError("boom")), tool_name="x", arguments={}, principal=None)

    [span] = exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    [event] = [e for e in span.events if e.name == "exception"]
    assert event.attributes["exception.type"] == "ValueError", "a builtin is named as the SDK and the semantic conventions name it"
    assert event.attributes["exception.message"] == "boom"
    assert "exception.stacktrace" not in event.attributes, "a stacktrace ends in the message"


class _OurError(Exception):
    pass


def test_an_exception_of_ours_is_named_by_its_module_and_class(exporter) -> None:
    tracer = tracing.get_tracer("t")

    with pytest.raises(_OurError), tracing.span(tracer, "unit"):
        raise _OurError("boom")

    [event] = [e for e in exporter.get_finished_spans()[0].events if e.name == "exception"]
    assert event.attributes["exception.type"] == f"{__name__}._OurError"


class _StrRaises(Exception):
    def __str__(self) -> str:
        raise RuntimeError("the message cannot be rendered")


def test_an_exception_whose_text_cannot_be_rendered_is_still_recorded_and_still_the_one_that_propagates(exporter) -> None:
    """A raising ``__str__`` must not turn the span's record into a second failure that replaces the tool's own exception."""
    tracer = tracing.get_tracer("t")

    with pytest.raises(_StrRaises), tracing.span(tracer, "unit"):
        raise _StrRaises()

    [span] = exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    [event] = [e for e in span.events if e.name == "exception"]
    assert event.attributes["exception.message"] == "" and event.attributes["exception.type"].endswith("_StrRaises")


class _CountsStr(Exception):
    renders = 0

    def __str__(self) -> str:
        type(self).renders += 1
        return "boom"


def test_a_span_that_is_not_recording_does_not_render_the_exception() -> None:
    """With tracing off (or a sampler that drops the span) nothing is exported, so the masking pass over the message is not paid for."""
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

    tracer = TracerProvider(sampler=ALWAYS_OFF).get_tracer("t")
    _CountsStr.renders = 0

    with pytest.raises(_CountsStr), tracing.span(tracer, "unit"):
        raise _CountsStr()

    assert _CountsStr.renders == 0


@pytest.mark.asyncio
async def test_a_successful_call_records_no_exception(exporter) -> None:
    class _Ok:
        async def call(self, *, tool_name, arguments, principal, ctx):
            from primer.model.chat import ToolCallResult

            return ToolCallResult(output=LEAKY, is_error=False)

    await invoke_one(provider=_Ok(), tool_name="x", arguments={}, principal=None)

    [span] = exporter.get_finished_spans()
    assert span.status.status_code.name != "ERROR" and not span.events


def test_the_span_helper_records_the_failure_and_reraises(exporter) -> None:
    tracer = tracing.get_tracer("t")

    with pytest.raises(RuntimeError), tracing.span(tracer, "unit"):
        raise RuntimeError(LEAKY)

    text = _everything_exported(exporter)
    _assert_clean(text)
    assert "unit" == exporter.get_finished_spans()[0].name


def test_a_cancellation_is_not_recorded_as_a_failure(exporter) -> None:
    import asyncio

    tracer = tracing.get_tracer("t")

    with pytest.raises(asyncio.CancelledError), tracing.span(tracer, "unit"):
        raise asyncio.CancelledError()

    [span] = exporter.get_finished_spans()
    assert not span.events


# ---- the sites ----------------------------------------------------------------------------------------------------------------------------------

SITES = [
    "primer/agent/tool_manager.py", "primer/llm/gemini.py", "primer/llm/ollama.py", "primer/llm/anthropic.py", "primer/llm/openrouter.py",
    "primer/llm/openresponses.py", "primer/llm/openchat.py",
]


@pytest.mark.parametrize("path", SITES)
def test_a_span_that_can_see_a_tools_or_a_providers_exception_uses_the_redacting_helper(path) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")

    assert "tracing.span(" in source, f"{path} does not open its span through primer.observability.tracing.span"


# What the SDK's own exception recording may still be used for: the claim engines' ``claim.due`` spans (a database call; no tool's or provider's text can leave the
# block). The helper itself is the one place that names the SDK calls, to turn them off.
SDK_RECORDING = ("start_as_current_span(", "start_span(", ".record_exception(")
ALLOWED = {"primer/observability/tracing.py": None, "primer/claim/in_memory.py": 'start_as_current_span("claim.due")', "primer/claim/postgres.py": 'start_as_current_span("claim.due")'}


def test_no_module_under_primer_records_an_exception_on_a_span_the_sdks_way() -> None:
    """A guard over the whole package, not a list of the modules that have a span today: a span added anywhere with ``start_as_current_span(...)`` (or a raw
    ``record_exception``) exports the raw exception text, so it must go through ``tracing.span`` or be added to ``ALLOWED`` with a reason."""
    offenders = []
    for path in sorted((ROOT / "primer").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if ALLOWED.get(rel, ...) is None:
            continue
        remainder = path.read_text(encoding="utf-8").replace(ALLOWED.get(rel) or "", "")
        offenders += [f"{rel}: {needle}" for needle in SDK_RECORDING if needle in remainder]

    assert not offenders, offenders
