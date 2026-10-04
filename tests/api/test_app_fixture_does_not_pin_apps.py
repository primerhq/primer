"""The shared ``app`` fixture must release every app it builds.

Regression guard for a leak that made tests/api retain ~4.2 MiB per test
(~6.1 GiB by test 1226 in one process, throttled to a crawl under a 3G
memory limit): the fixture entered the MCP session manager's anyio task
group in pytest-asyncio's SETUP task and exited it from the TEARDOWN task.
anyio requires a cancel scope to be exited by the task that entered it, so
every exit raised "Attempted to exit cancel scope in a different task than it
was entered in" - swallowed by the fixture - and anyio's module-level
``_task_states`` WeakKeyDictionary kept the setup Task alive through
TaskState -> CancelScope -> that same Task. A finished Task keeps its result,
which for an async-generator fixture is the yielded FastAPI app, so the whole
app graph was pinned.

The leak only exists ACROSS tests, so each parametrized test compares what is
alive during it with what was alive during the first one in this process. A
leak shows from the second test on. Run in isolation a single test has no
earlier sample to compare with; the guard needs the whole module.

``_task_states`` is anyio-private. Reading it is deliberate: it is the exact
structure that pinned the apps, and if anyio renames it this test should fail
loudly rather than quietly stop checking.
"""

from __future__ import annotations

import gc

import pytest
from anyio._backends import _asyncio as _anyio_asyncio
from fastapi import FastAPI

_REPEATS = 6
_first_sample: dict[str, int] = {}


def _live_fastapi_apps() -> int:
    gc.collect()
    gc.collect()
    return sum(1 for o in gc.get_objects() if type(o) is FastAPI)


@pytest.mark.asyncio
@pytest.mark.parametrize("i", range(_REPEATS))
async def test_repeated_app_fixtures_do_not_accumulate(app: FastAPI, i: int) -> None:
    # Without this the test would pass vacuously if the fixture stopped
    # starting the mount (the very mount whose task group caused the leak).
    assert app.state.mcp_session_manager is not None

    apps = _live_fastapi_apps()
    states = len(_anyio_asyncio._task_states)  # noqa: SLF001

    if not _first_sample:
        _first_sample.update(apps=apps, states=states)
        return

    # Both are evaluated before asserting so a failure reports each one that
    # tripped, rather than the first hiding the second.
    problems = []
    if apps != _first_sample["apps"]:
        problems.append(
            f"{apps} live FastAPI apps against {_first_sample['apps']} during "
            "the first test: an earlier test's app is still alive after its "
            "fixture was torn down"
        )
    if states > _first_sample["states"]:
        problems.append(
            f"anyio holds {states} task-state entries against "
            f"{_first_sample['states']} during the first test: a cancel scope "
            "was entered in one task and never cleanly exited in it, so its "
            "host task (and everything that task references) is pinned"
        )
    assert not problems, f"test #{i}: " + "; ".join(problems)
