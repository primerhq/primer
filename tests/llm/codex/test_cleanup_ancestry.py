"""Hard-process-bound coverage: a cancellation-resistant cycle must not hang CI."""
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time

import pytest


# Only actual model bridge code and a cooperative callback; no live transport.
PROBE = r'''
import asyncio
from primer.coordinator.in_memory import InMemoryRateLimiter
from primer.llm.codex.adapter import CodexFoundationLLM
from primer.llm.codex.boundary import BoundaryError, BridgeConfig
from primer.llm.codex.harness import RecordedChannel
from primer.llm.codex.lifecycle import OwnedStream
from primer.llm.codex.request import prepare_request
from primer.model.chat import TextDelta
from tests.llm.codex.test_bridge import bridge, frames, message

async def main():
    mode = MODE
    if mode == "source-self":
        registered = set()
        async def source():
            try:
                yield TextDelta(index=0, text="first")
            finally:
                await stream.aclose()
        stream = OwnedStream(source(), on_close=registered.discard)
        registered.add(stream)
        await anext(stream)
        try:
            await stream.aclose()
        except BoundaryError:
            pass
        else:
            raise AssertionError("recursive source close was not refused")
        assert not registered
    else:
        limiter = InMemoryRateLimiter()
        callback_entered = False
        class RecursiveChannel(RecordedChannel):
            async def aclose(self):
                nonlocal callback_entered
                callback_entered = True
                try:
                    await owner.aclose()
                finally:
                    await super().aclose()
        channel = RecursiveChannel(frames())
        instance, _ = bridge(factory=lambda _: channel, config=BridgeConfig(cleanup_seconds=.02))
        owner = CodexFoundationLLM(instance, provider_id="p", rate_limiter=limiter) if mode == "adapter" else instance
        stream = owner.stream(model="model", messages=[message()]) if mode == "adapter" else instance.stream(prepare_request("model", [message()]))
        assert (await asyncio.wait_for(anext(stream), 1)).type == "text_delta"
        try:
            await owner.aclose()
        except BoundaryError:
            pass
        else:
            raise AssertionError("recursive owner close was not refused")
        assert callback_entered and channel.closed and not instance._active
        if mode == "adapter":
            assert not owner._active
        async with asyncio.timeout(.2):
            async with await limiter.acquire("llm:p", max_concurrency=1):
                pass
    await asyncio.sleep(0)
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    print("CLOSED_NO_PENDING_TASKS", flush=True)

_phase(b"R")  # cold imports and definitions complete; operations start now
asyncio.run(main())
_phase(b"C")  # includes asyncio runner shutdown, not merely the last assertion
'''


def _run_cleanup_child(script, *, startup_seconds=20, operation_seconds=4, exit_seconds=4):
    """Hard deadlines per phase, including normal exit; never rely on cancellation.

    A private pipe carries markers independently of diagnostics. Temporary files
    prevent stdout/stderr backpressure. Every failure kills and reaps the owned
    child; these scripts create no descendants. No os._exit bypass of shutdown.
    """
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        read_fd, write_fd = os.pipe()
        process = None
        phase = "startup/import readiness"
        started = time.monotonic()
        timings = []
        failure = None
        prefix = (
            'import os as _phase_os\n'
            'def _phase(marker):\n'
            f'    _phase_os.write({write_fd}, marker)\n'
        )
        try:
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", prefix + script],
                cwd=Path(__file__).resolve().parents[3], env=env,
                pass_fds=(write_fd,), stdout=stdout, stderr=stderr,
            )
            os.close(write_fd)
            write_fd = None
            for phase, marker, seconds in (
                ("startup/import readiness", b"R", startup_seconds),
                ("cleanup operation and asyncio shutdown", b"C", operation_seconds),
            ):
                assert select.select([read_fd], [], [], seconds)[0], f"deadline {seconds}s exceeded"
                assert os.read(read_fd, 1) == marker, "child exited or sent an unexpected phase marker"
                timings.append((phase, time.monotonic() - started))
            phase = "normal interpreter exit"
            process.wait(timeout=exit_seconds)
            assert process.returncode == 0, f"child exit code {process.returncode}"
            timings.append((phase, time.monotonic() - started))
        except BaseException as exc:
            failure = exc
        finally:
            try:
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
            finally:
                os.close(read_fd)
                if write_fd is not None:
                    os.close(write_fd)
        stdout.seek(0)
        stderr.seek(0)
        output = stdout.read(16384).decode(errors="replace")
        errors = stderr.read(16384).decode(errors="replace")
        if failure is not None:
            raise AssertionError(
                f"{phase}: {failure}; completed phases (elapsed seconds): {timings}; "
                f"child reaped={process is not None and process.returncode is not None}\n"
                f"stdout: {output}\nstderr: {errors}"
            ) from failure
        return output


@pytest.mark.parametrize("mode", ["bridge", "adapter", "source-self"])
def test_cleanup_callback_cannot_join_its_own_ancestor(mode):
    output = _run_cleanup_child("MODE = " + repr(mode) + "\n" + PROBE)
    assert output.strip() == "CLOSED_NO_PENDING_TASKS"


@pytest.mark.parametrize("phase", ["startup", "operation", "exit"])
def test_cleanup_child_phase_deadline_kills_and_reaps(phase):
    # A successful assertion marker must not turn a later shutdown hang green.
    markers = {"startup": "", "operation": '_phase(b"R")\n',
               "exit": '_phase(b"R")\n_phase(b"C")\n'}[phase]
    script = 'import signal\n' + markers + 'print("CLOSED_NO_PENDING_TASKS", flush=True)\nsignal.pause()\n'
    expected = {"startup": "startup/import readiness", "operation": "cleanup operation and asyncio shutdown",
                "exit": "normal interpreter exit"}[phase]
    with pytest.raises(AssertionError, match=expected) as caught:
        _run_cleanup_child(script, startup_seconds=.2 if phase == "startup" else 20,
                           operation_seconds=.2 if phase == "operation" else 4, exit_seconds=.2)
    assert "child reaped=True" in str(caught.value)


@pytest.mark.asyncio
async def test_external_concurrent_close_still_joins_one_cleanup():
    import asyncio
    from primer.llm.codex.harness import RecordedChannel
    from primer.llm.codex.request import prepare_request
    from tests.llm.codex.test_bridge import bridge, frames, message

    entered, release = asyncio.Event(), asyncio.Event()
    class Held(RecordedChannel):
        closes = 0
        async def aclose(self):
            self.closes += 1
            entered.set()
            await release.wait()
            await super().aclose()
    channel = Held(frames())
    instance, _ = bridge(factory=lambda _: channel)
    stream = instance.stream(prepare_request("model", [message()]))
    await anext(stream)
    first = asyncio.create_task(stream.aclose())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(instance.aclose())
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert channel.closed and channel.closes == 1 and not instance._active
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
