"""ClaudeCodeLLM: drive the operator's logged-in claude CLI as a model backend.

One fresh CLI process per model step (the claude_code plan, spec 6.2): the
older history rides in the system prompt file, the structured tail rides on
stdin, the step's tools ride on a per-step bridge path, and the CLI's
stream-json stdout is translated onto primer stream events (translate.py).
The process dies only via its process group (SIGKILL) and its temp directory
is removed with it, so a step leaves no live process, no directory and no
worker secret behind (process.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from primer.int.coordinator import RateLimiter
from primer.int.llm import LLM
from primer.llm._failure import describe_failure
from primer.llm._reasoning import claude_code_extended, debug_once
from primer.llm._timeout import GenerationBudgetExceeded, _iter_with_timeout, _open_lazy_with_connect_timeout
from primer.llm._trace import _serialize_messages
from primer.llm.claude_code.bridge import BridgeStep, alias_for, get_bridge
from primer.llm.claude_code.history import plan_replay
from primer.llm.claude_code.process import child_env, child_process, read_stderr_tail
from primer.llm.claude_code.translate import StreamTranslator
from primer.llm.claude_code.wire import build_argv, tail_lines
from primer.llm.settings_catalogue import Catalogue, catalogue_for, warn_dropped, wire_of
from primer.model.chat import (
    Done,
    Error as ChatError,
    Message,
    StreamEvent,
    TextDelta,
    Tool,
    ToolCallStart,
    ToolChoice,
    Usage,
)
from primer.model.except_ import (
    BadRequestError,
    ConfigError,
    PrimerError,
    ProviderError,
    ProviderTimeoutError,
    TokenCounterUnavailable,
    TransientError,
)
from primer.model.model_profile import ModelProfileConfig
from primer.model.provider import ClaudeCodeConfig, LLMProvider, LLMProviderType
from primer.observability import tracing as _tracing
import primer.observability.metrics as _metrics

logger = logging.getLogger(__name__)


def _tool_label(name: str) -> str:
    """The model-visible label of a bridge tool, as the replayed history renders it."""
    return "mcp__p__" + alias_for(name)


def _remove_cli_socket(path: str | None) -> None:
    """Unlink the CLI's messaging socket (spec 6.8 v1.6), best effort.

    The path comes from the CLI's own init line: it is touched only when its
    NORMALISED location is under ``/tmp/cc-socks/`` (a ``..`` that escapes the
    directory does not count) and owned by this worker's user; every error is
    ignored (hygiene, not data)."""
    if not isinstance(path, str) or os.path.dirname(os.path.normpath(path)) != "/tmp/cc-socks":
        return
    try:
        if os.stat(path).st_uid != os.getuid():
            return
        os.unlink(path)
    except OSError:
        pass


def _bridge_connected(line: dict) -> bool:
    """Whether the CLI's init line reports the bridge (mcp server ``p``) as
    connected (spec 6.6 v1.5)."""
    for server in line.get("mcp_servers") or []:
        if isinstance(server, dict) and server.get("name") == "p" and server.get("status") == "connected":
            return True
    return False


class ClaudeCodeLLM(LLM):
    """Streaming LLM adapter for the claude CLI (one fresh process per step)."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        rate_limiter: RateLimiter | None = None,
        trace_llm_io: bool = False,
    ) -> None:
        if provider.provider != LLMProviderType.CLAUDE_CODE:
            raise ConfigError(
                f"ClaudeCodeLLM requires provider type CLAUDE_CODE; "
                f"got {provider.provider}"
            )
        if not isinstance(provider.config, ClaudeCodeConfig):
            raise ConfigError(
                "ClaudeCodeLLM requires ClaudeCodeConfig in provider.config"
            )

        self._provider = provider
        self._config: ClaudeCodeConfig = provider.config
        self._catalogue: Catalogue = catalogue_for(wire_of(provider.provider.value, None))
        if rate_limiter is None:
            from primer.coordinator.in_memory import InMemoryRateLimiter
            rate_limiter = InMemoryRateLimiter()
        self._rate_limiter = rate_limiter
        self._rate_limit_key = f"llm:{provider.id}"
        self._max_concurrency = provider.limits.max_concurrency
        self._request_timeout_seconds = provider.limits.request_timeout_seconds
        self._total_timeout_seconds = provider.limits.total_timeout_seconds
        self._connect_timeout_seconds = provider.limits.connect_timeout_seconds
        self._trace_llm_io = trace_llm_io
        # Bridge steps this adapter opened and has not closed yet; aclose closes
        # whatever is still open (streams normally close theirs on their way out).
        self._open_steps: list[BridgeStep] = []

        logger.info(
            "ClaudeCode adapter initialized",
            extra={
                "provider_id": provider.id,
                "binary_path": self._config.binary_path,
                "max_concurrency": self._max_concurrency,
            },
        )

    async def count_tokens(
        self,
        *,
        model: str,
        messages: list[Message],
        tools: list[Tool] | None = None,
    ) -> int:
        """The CLI has no token-counting surface; the wrapper estimates instead."""
        raise TokenCounterUnavailable(
            f"the claude_code adapter cannot count tokens for model {model!r}"
        )

    def profile_extended(
        self,
        config: ModelProfileConfig,
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        return claude_code_extended(config)

    async def aclose(self) -> None:
        """Close the bridge steps still open on this adapter. Idempotent."""
        for step in list(self._open_steps):
            self._open_steps.remove(step)
            try:
                await step.close()
            except Exception as exc:  # noqa: BLE001 -- teardown is best-effort
                logger.warning("claude_code bridge step close failed: %s", exc)

    async def stream(
        self,
        *,
        model: str,
        messages: list[Message],
        temperature: float | None = None,
        top_p: float | None = None,
        max_output_tokens: int | None = None,
        stop: list[str] | None = None,
        response_format: type[BaseModel] | dict[str, Any] | None = None,
        tools: list[Tool] | None = None,
        tool_choice: ToolChoice | None = None,
        extended: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        ignored = [
            name
            for name, value in (
                ("temperature", temperature),
                ("top_p", top_p),
                ("stop", stop),
                ("max_output_tokens", max_output_tokens),
            )
            if value is not None
        ]
        if ignored:
            debug_once(
                ("claude_code-ignored", self._provider.id),
                "claude_code ignores %s: the CLI exposes no control for them",
                ", ".join(ignored),
            )

        unknown = [key for key in (extended or {}) if key not in self._catalogue.keys()]
        if unknown:
            warn_dropped("ClaudeCode", self._provider.id, unknown)

        effort: str | None = (extended or {}).get("effort")
        schema = response_format.model_json_schema() if isinstance(response_format, type) else response_format

        kind = self._provider.provider.value
        tracer = _tracing.get_tracer("primer.llm")
        t0 = time.monotonic()
        messaging_socket_path: str | None = None
        with _tracing.span(tracer, "llm.stream") as span:
            span.set_attribute("llm.provider", kind)
            span.set_attribute("llm.model", model)
            if max_output_tokens is not None:
                span.set_attribute("llm.request.max_tokens", max_output_tokens)
            if self._trace_llm_io:
                span.set_attribute(
                    "llm.request.messages",
                    json.dumps(_serialize_messages(messages)),
                )
            tokens_in = 0
            tokens_out = 0
            try:
                async with await self._rate_limiter.acquire(
                    self._rate_limit_key, max_concurrency=self._max_concurrency,
                ):
                    binary = self._config.binary_path
                    resolved = shutil.which(binary)
                    if resolved is None:
                        raise ConfigError(
                            f"claude CLI not found on {socket.gethostname()}: {binary}"
                        )
                    plan = plan_replay(
                        messages, name_of=_tool_label,
                    )
                    if not plan.tail or plan.tail[-1].role not in ("user", "tool"):
                        raise BadRequestError("history must end with a user or tool message")
                    step: BridgeStep | None = None
                    mcp_url: str | None = None

                    def unalias(name: str) -> str:
                        """A bridge alias back to the scoped tool id (identity without tools)."""
                        return step.aliases.get(name, name) if step is not None else name

                    if tools and tool_choice != "none":
                        step = await (await get_bridge()).register(tools)
                        mcp_url = step.url
                        self._open_steps.append(step)
                    env = child_env(self._config.pass_env)
                    tmp_dir: dict[str, Path] = {}

                    def argv_for(tmp: Path) -> list[str]:
                        tmp_dir["tmp"] = tmp
                        return build_argv(
                            resolved, tmp, model=model, mcp_url=mcp_url,
                            effort=effort, json_schema=schema,
                        )

                    translator = StreamTranslator(unalias=unalias)
                    yielded_any = False
                    terminal = False
                    dropped_after_done = 0
                    buffered: list[str] = []
                    primer_tool_call = False
                    pending_done: Done | None = None
                    stdin_task: asyncio.Task[None] | None = None
                    try:
                        async with child_process(argv_for, env=env, system_text=plan.system_text) as proc:
                            # The tail is written CONCURRENTLY with the stdout reads:
                            # a tail beyond the pipe buffer must not hang the step
                            # (a CLI that never reads would stall the drain, holding
                            # the llm slot), and a CLI that exits at startup breaks
                            # the pipe INSIDE the task, never raw into the step.
                            if proc.stdin is not None:
                                async def write_tail() -> None:
                                    for line in tail_lines(plan.tail):
                                        proc.stdin.write(
                                            json.dumps(line, ensure_ascii=False).encode("utf-8") + b"\n"
                                        )
                                    await proc.stdin.drain()
                                    proc.stdin.close()

                                stdin_task = asyncio.create_task(write_tail())

                            async def open_lines() -> AsyncIterator[dict]:
                                return self._stdout_lines(proc.stdout)

                            try:
                                lines = await _open_lazy_with_connect_timeout(
                                    open_lines, self._connect_timeout_seconds,
                                    provider_id=self._provider.id, model=model,
                                )
                            except ProviderTimeoutError:
                                raise
                            except Exception as exc:
                                err = describe_failure(
                                    ProviderError(str(exc) or type(exc).__name__, code="claude_code_step_failed"),
                                    exc, self._provider,
                                )
                                logger.error(
                                    "claude_code failed before the first line",
                                    extra={
                                        "provider_id": self._provider.id,
                                        "model": model,
                                        "exception": type(exc).__name__,
                                    },
                                )
                                raise err from exc
                            try:
                                async for line in _iter_with_timeout(
                                    lines,
                                    self._request_timeout_seconds,
                                    self._total_timeout_seconds,
                                ):
                                    if line.get("type") == "system" and line.get("subtype") == "init":
                                        # The init line names the CLI's messaging socket (spec 6.8);
                                        # the kill removes it.
                                        messaging_socket_path = line.get("messaging_socket_path")
                                        if step is not None and not _bridge_connected(line):
                                            # Spec 6.6 v1.5: tools were registered but the CLI
                                            # never connected the bridge: the model would run
                                            # without its tools, so the step fails instead.
                                            bridge_err = "the claude CLI did not connect the primer tool bridge (mcp server 'p')"
                                            if not yielded_any:
                                                # Retryable: the worker backs off and
                                                # re-enqueues the session.
                                                raise TransientError(bridge_err, code="claude_code_bridge_not_connected")
                                            yield ChatError(fatal=True, code="claude_code_bridge_not_connected", message=bridge_err)
                                            terminal = True
                                            break
                                    for ev in translator.feed(line):
                                        if isinstance(ev, ChatError):
                                            if not yielded_any:
                                                # A failing result before any event:
                                                # raise the classified error, never yield it.
                                                err = translator.error_for_result(line)
                                                if err is None and ev.code == "bad_request":
                                                    # Spec 6.6 v1.6: a too-long prompt
                                                    # keeps the CLI text (describe_failure
                                                    # keeps it for this class and caps its
                                                    # length), so is_context_overflow and
                                                    # the reactive compaction recognise it.
                                                    err = describe_failure(
                                                        BadRequestError(
                                                            ev.message or "prompt is too long",
                                                            code=ev.code,
                                                        ),
                                                        RuntimeError(ev.message or ""), self._provider,
                                                    )
                                                elif err is None:
                                                    # Spec 6.6: even the pre-event
                                                    # failure message is scrubbed.
                                                    err = describe_failure(
                                                        ProviderError(
                                                            ev.message or "the claude CLI failed before answering",
                                                            code=ev.code or "claude_code_result_error",
                                                        ),
                                                        RuntimeError(ev.message or ""), self._provider,
                                                    )
                                                raise err
                                            if terminal:
                                                # Done was already yielded: a late failure is
                                                # counted, never a second terminal.
                                                dropped_after_done += 1
                                                continue
                                            scrubbed = describe_failure(
                                                ProviderError(ev.message, code=ev.code or "claude_code_result_error"),
                                                RuntimeError(ev.message), self._provider,
                                            )
                                            yield ChatError(fatal=True, code=scrubbed.code, message=scrubbed.message)
                                            terminal = True
                                            continue
                                        if terminal:
                                            # Done was already yielded: a late translated
                                            # event is counted, never a second terminal.
                                            dropped_after_done += 1
                                            continue
                                        if isinstance(ev, Done):
                                            if schema is not None and not primer_tool_call:
                                                # Spec 6.7 v1.6: with response_format
                                                # set and no primer tool call, the
                                                # Done is held until the result line;
                                                # the answer is the result's
                                                # structured_output, else the
                                                # buffered text.
                                                pending_done = ev
                                            else:
                                                # Spec 6.2 step 9 v1.6: no
                                                # response_format, or a primer tool
                                                # call was emitted (the bridge holds
                                                # it and the CLI never writes a
                                                # result line): the message's Done
                                                # ends the step at once, whatever
                                                # its stop reason. A held Done from
                                                # an earlier (rejected and retried)
                                                # message is superseded by this one:
                                                # clear it, or the step would wait
                                                # out the stall window for a result
                                                # line that never comes.
                                                pending_done = None
                                                if buffered:
                                                    yield TextDelta(text="".join(buffered), index=0)
                                                    buffered = []
                                                yield ev
                                                terminal = True
                                                yielded_any = True
                                        elif isinstance(ev, TextDelta) and schema is not None:
                                            # Spec 6.7 v1.5: streamed text is buffered,
                                            # never streamed: the answer is emitted
                                            # once, at the result line.
                                            buffered.append(ev.text)
                                        else:
                                            yield ev
                                            if isinstance(ev, Usage):
                                                tokens_in = ev.input_tokens
                                                tokens_out = ev.output_tokens
                                            if isinstance(ev, ToolCallStart):
                                                primer_tool_call = True
                                            yielded_any = True
                                    if terminal and pending_done is None:
                                        # Spec 6.2 step 9 v1.5: a Done that is not held
                                        # (and a fatal Error) ends the step at once: a CLI
                                        # left running can spend plan or block on a
                                        # bridge-held call. Only a response_format step
                                        # waits for the result line.
                                        if dropped_after_done:
                                            logger.warning(
                                                "claude_code: %d events dropped after the terminal event",
                                                dropped_after_done,
                                                extra={"provider_id": self._provider.id, "model": model},
                                            )
                                        break
                                    if line.get("type") == "result":
                                        if pending_done is not None and not terminal:
                                            # The answer to the held Done (spec 6.7
                                            # v1.5): the CLI's validated
                                            # structured_output, else the buffered
                                            # text. A failing result already set
                                            # terminal (or raised pre-event), so no
                                            # answer ever follows an error.
                                            value = line.get("structured_output")
                                            if value is not None:
                                                yield TextDelta(text=json.dumps(value), index=0)
                                            else:
                                                text = "".join(buffered)
                                                if text:
                                                    # No structured_output: the
                                                    # buffered text is the answer,
                                                    # as one delta.
                                                    yield TextDelta(text=text, index=0)
                                            yield pending_done
                                            pending_done = None
                                            terminal = True
                                            yielded_any = True
                                        elif not terminal and not line.get("is_error"):
                                            # Spec 6.7 v1.6: a successful result BEFORE
                                            # any Done (a disabled slash command's
                                            # reply: the synthetic assistant line is
                                            # not translated) answers with its text,
                                            # then Done.
                                            text = line.get("result")
                                            if isinstance(text, str) and text:
                                                yield TextDelta(text=text, index=0)
                                            yield Done(stop_reason="stop", raw_reason="")
                                            terminal = True
                                            yielded_any = True
                                        break
                                    if translator.kill_now:
                                        break
                            except TimeoutError as exc:
                                if terminal:
                                    # Done was already yielded: a late stall or total
                                    # timeout is a log plus return, never a failure
                                    # after the terminal event.
                                    logger.warning(
                                        "claude_code: timeout after Done (logged, not raised)",
                                        extra={"provider_id": self._provider.id, "model": model},
                                    )
                                    return
                                if isinstance(exc, GenerationBudgetExceeded):
                                    total_val = self._total_timeout_seconds
                                    logger.error(
                                        "claude_code generation exceeded its total budget (%.1f s)",
                                        total_val,
                                        extra={"provider_id": self._provider.id, "model": model},
                                    )
                                    raise ProviderTimeoutError(
                                        f"claude_code generation exceeded its total budget of "
                                        f"{total_val} s (provider_id={self._provider.id!r}, "
                                        f"model={model!r})",
                                        code="generation_timeout",
                                    ) from exc
                                timeout_val = self._request_timeout_seconds
                                logger.error(
                                    "claude_code stream timed out (no line in %.1f s)",
                                    timeout_val,
                                    extra={"provider_id": self._provider.id, "model": model},
                                )
                                raise ProviderTimeoutError(
                                    f"claude_code stream stalled: no line received within "
                                    f"{timeout_val} s (provider_id={self._provider.id!r}, "
                                    f"model={model!r})",
                                    code="stream_timeout",
                                ) from exc
                            except PrimerError:
                                # A classified error this loop raised on purpose
                                # (a pre-stream failing result); it must not be
                                # folded into a mid-stream Error.
                                raise
                            except Exception as exc:
                                if terminal:
                                    # Done was already yielded: a late failure is a
                                    # log plus return, never an Error after the
                                    # terminal event.
                                    logger.warning(
                                        "claude_code: exception after Done (logged, not yielded): %s",
                                        type(exc).__name__,
                                        extra={"provider_id": self._provider.id, "model": model},
                                    )
                                    return
                                if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                                    # Defensive: the stdout reader normally just
                                    # EOFs when the CLI dies, so a pipe error here
                                    # is unexpected; route it to the crash path,
                                    # which quotes the stderr tail and classifies
                                    # it as claude_code_cli_failed.
                                    logger.error(
                                        "claude_code pipe broken mid-stream",
                                        extra={
                                            "provider_id": self._provider.id,
                                            "model": model,
                                            "exception": type(exc).__name__,
                                        },
                                    )
                                else:
                                    err = describe_failure(
                                        ProviderError(str(exc) or type(exc).__name__, code="claude_code_step_failed"),
                                        exc, self._provider,
                                    )
                                    logger.error(
                                        "claude_code stream aborted",
                                        extra={
                                            "provider_id": self._provider.id,
                                            "model": model,
                                            "exception": type(exc).__name__,
                                        },
                                    )
                                    yield ChatError(fatal=True, code=err.code, message=err.message)
                                    return
                            if not terminal:
                                # The process stopped without a result line (a crash,
                                # or a cut-off stream). The stderr tail is only
                                # readable while the temp dir still exists.
                                stderr_tail = (
                                    read_stderr_tail(tmp_dir["tmp"]).strip() if "tmp" in tmp_dir else ""
                                )
                                if pending_done is not None:
                                    # The message_stop was a real Done; the result
                                    # line never came (spec 6.7 v1.5). A tool-call
                                    # Done ends the step without a result line by
                                    # design (the kill never answers the call);
                                    # otherwise the buffered text is the answer --
                                    # and with neither an answer nor a result line
                                    # the step fails below.
                                    text = "".join(buffered)
                                    if text:
                                        yield TextDelta(text=text, index=0)
                                    if text or pending_done.stop_reason == "tool_use":
                                        yield pending_done
                                        pending_done = None
                                        terminal = True
                                if not terminal:
                                    try:
                                        returncode = await asyncio.wait_for(proc.wait(), timeout=2.0)
                                    except TimeoutError:
                                        returncode = None
                                    if returncode is not None:
                                        message = f"the claude CLI exited without a result (code {returncode})"
                                    else:
                                        message = "the claude CLI stopped without a result"
                                    if stderr_tail:
                                        message += f": {stderr_tail[-500:]}"
                                    # Spec 6.6: the message (with the stderr tail) goes
                                    # through describe_failure before it leaves the adapter.
                                    err = describe_failure(
                                        ProviderError(message, code="claude_code_cli_failed"),
                                        RuntimeError(message), self._provider,
                                    )
                                    if not yielded_any:
                                        raise err
                                    yield ChatError(fatal=True, code=err.code, message=err.message)
                    except OSError as exc:
                        # Spec 6.6 v1.5: a raw OSError from the spawn setup
                        # (mkdtemp ENOSPC, exec ENOEXEC/ENOENT, EMFILE) is
                        # classified, never raw; the inner handlers convert
                        # every in-block error, so only a failed setup
                        # reaches here.
                        err = describe_failure(
                            ProviderError(f"the claude CLI could not be spawned: {exc}", code="claude_code_spawn_failed"),
                            exc, self._provider,
                        )
                        logger.error(
                            "claude_code could not spawn the CLI",
                            extra={
                                "provider_id": self._provider.id,
                                "model": model,
                                "exception": type(exc).__name__,
                            },
                        )
                        raise err from exc
                    finally:
                        # The tail write may still be running (a CLI that never
                        # reads it): the process group dies when the block exits,
                        # so the write cannot succeed -- cancel it and wait.
                        # asyncio.wait does NOT re-raise the task's exception,
                        # so a pending cancellation of THIS step propagates
                        # normally and a non-pipe write error is only logged.
                        if stdin_task is not None and not stdin_task.done():
                            stdin_task.cancel()
                        try:
                            if stdin_task is not None:
                                await asyncio.wait({stdin_task})
                                if (
                                    stdin_task.done()
                                    and not stdin_task.cancelled()
                                    and stdin_task.exception() is not None
                                ):
                                    logger.debug(
                                        "claude_code tail write task failed: %s",
                                        stdin_task.exception(),
                                        extra={"provider_id": self._provider.id, "model": model},
                                    )
                        finally:
                            # The CLI's process group is dead by here (the
                            # block's own cleanup kills it); only then does the
                            # bridge step close, so a held call never ends with
                            # the CLI still alive. A second cancel that lands
                            # in the join above must not skip the close.
                            if step is not None:
                                if step in self._open_steps:
                                    self._open_steps.remove(step)
                                try:
                                    await step.close()
                                except Exception as exc:  # noqa: BLE001 -- teardown is best-effort
                                    logger.warning("claude_code bridge step close failed: %s", exc)
            except Exception as exc:
                _metrics.llm_failure_total.labels(kind, type(exc).__name__).inc()
                raise
            finally:
                # Spec 6.8: after the kill, the CLI's messaging socket is
                # removed (best effort); nothing else on this path awaits.
                _remove_cli_socket(messaging_socket_path)
                span.set_attribute("llm.usage.tokens_in", tokens_in)
                span.set_attribute("llm.usage.tokens_out", tokens_out)
                _metrics.llm_tokens_total.labels(kind, "in").inc(tokens_in)
                _metrics.llm_tokens_total.labels(kind, "out").inc(tokens_out)
                _metrics.llm_duration_seconds.labels(kind).observe(time.monotonic() - t0)

    @staticmethod
    async def _stdout_lines(stdout: asyncio.StreamReader) -> AsyncIterator[dict]:
        """One parsed JSON line of the CLI's stdout per yield.

        Blank and non-JSON lines are skipped: the CLI is the only writer of a
        well-formed stream, and a torn line must not kill the step.
        """
        while True:
            raw = await stdout.readline()
            if not raw:
                return
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                yield json.loads(text)
            except ValueError:
                continue
