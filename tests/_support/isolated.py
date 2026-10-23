"""Run a snippet in a child Python with a hard timeout and a memory cap.

A test that could make the ``regex`` engine compile a blow-up pattern (``(?:a{65535}){65535}`` exhausts memory, holding the
GIL the whole time) must not run it in the test process: a regression would hang the lane instead of failing the test. The
child gets the repository on ``sys.path``, an address-space cap, and is killed at the timeout.
"""

from __future__ import annotations

import json
import resource
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = str(Path(__file__).resolve().parents[2])


def _cap_memory(limit: int):
    def _apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return _apply


def run_isolated(snippet: str, *, timeout: float = 30.0, memory_bytes: int = 3_000_000_000) -> Any:
    """Run ``snippet`` (which must ``print(json.dumps(result))`` as its last line) and return the decoded result.

    Raises ``AssertionError`` when the child times out, runs out of memory, or exits non-zero.
    """
    code = f"import sys\nsys.path.insert(0, {_ROOT!r})\n" + snippet
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True, text=True, timeout=timeout, preexec_fn=_cap_memory(memory_bytes),
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f"the child ran past {timeout} s (killed): {exc.stdout!r}") from None
    if done.returncode != 0:
        raise AssertionError(f"the child exited {done.returncode}: {done.stderr[-2000:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])
