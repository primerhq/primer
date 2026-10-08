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
alive during it with the LOWEST count any earlier test in this process saw. A
leak shows from the second test on. Run in isolation a single test has no
earlier sample to compare with; the guard needs the whole module.

Why the lowest and not the first (CI run 37739441310, a slow runner): the first
sample can be inflated by apps that earlier modules on the same xdist worker
left to be collected, and they are freed in a batch right after it. The old
check, ``apps != first``, then failed every later test (14, 15, 16, 17, 18
against a first sample of 19) although nothing had been pinned by THIS module.
Against the lowest, a batch release is fine and a steady count is fine, while a
real ``+1`` per test is still caught on the second test it appears.

Two more things make a failure useful instead of a guess:

* before judging a count that is over the lowest, the guard lets the event loop
  turn and collects again, a few times (``_SETTLE_ROUNDS`` x ``_SETTLE_SECONDS``):
  a cleanup that finishes a moment late (an executor or aiosqlite thread, a
  fire-and-forget task) is not a leak, and a real leak never goes away;
* when it still fails, the message says what refers to each lingering app from
  outside it, one to three hops of ``gc.get_referrers`` (``_describe_apps``), so
  the pin is named. Read the FIRST failure: pytest keeps a failed test's
  traceback (and its ``app`` local) alive, so the reports of the tests after it
  also list that traceback.

The judgement and the diagnostics are exercised on their own below, including a
deliberate leak, so the guard cannot go quiet without a test noticing.

``_task_states`` is anyio-private. Reading it is deliberate: it is the exact
structure that pinned the apps, and if anyio renames it this test should fail
loudly rather than quietly stop checking.
"""

from __future__ import annotations

import asyncio
import gc
import os
import types

import pytest
from anyio._backends import _asyncio as _anyio_asyncio
from fastapi import FastAPI

_REPEATS = 6

# How many times a count that is over the lowest is re-taken, and how long the loop is let run between takes, before it is believed.
_SETTLE_ROUNDS = 10
_SETTLE_SECONDS = 0.1

# Diagnostics are bounded: at most this many lingering apps are described, to this many hops, this many referrers per object.
_APPS_SHOWN = 3
_REFERRER_HOPS = 3
_REFERRERS_PER_OBJECT = 4


class _Guard:
    """The judgement: a sample is a problem when it is over the lowest earlier sample, for apps and for anyio task states."""

    def __init__(self) -> None:
        self.lowest_apps: int | None = None
        self.lowest_states: int | None = None

    def problems(self, apps: int, states: int) -> list[str]:
        found = []
        if self.lowest_apps is not None and apps > self.lowest_apps:
            found.append(
                f"{apps} live FastAPI apps against {self.lowest_apps} at the lowest earlier sample: an earlier test's app is "
                "still alive after its fixture was torn down"
            )
        if self.lowest_states is not None and states > self.lowest_states:
            found.append(
                f"anyio holds {states} task-state entries against {self.lowest_states} at the lowest earlier sample: a cancel "
                "scope was entered in one task and never cleanly exited in it, so its host task (and everything that task "
                "references) is pinned"
            )
        return found

    def observe(self, apps: int, states: int) -> None:
        self.lowest_apps = apps if self.lowest_apps is None else min(self.lowest_apps, apps)
        self.lowest_states = states if self.lowest_states is None else min(self.lowest_states, states)


def _collect() -> None:
    gc.collect()
    gc.collect()


def _live_fastapi_apps() -> list[FastAPI]:
    _collect()
    return [o for o in gc.get_objects() if type(o) is FastAPI]


def _counts() -> tuple[int, int]:
    apps = _live_fastapi_apps()
    try:
        return len(apps), len(_anyio_asyncio._task_states)  # noqa: SLF001
    finally:
        del apps  # a list of apps is itself a reference to them


async def _settled_counts(guard: _Guard) -> tuple[int, int]:
    """The counts, re-taken (with the loop allowed to run) while they are over the lowest, up to ``_SETTLE_ROUNDS`` times."""
    apps, states = _counts()
    for _ in range(_SETTLE_ROUNDS):
        if not guard.problems(apps, states):
            break
        await asyncio.sleep(_SETTLE_SECONDS)
        apps, states = _counts()
    return apps, states


def _label(referrer: object, child: object) -> str:
    """One line saying what ``referrer`` is and, where it can be found out, how it holds ``child``."""
    try:
        if isinstance(referrer, types.FrameType):
            return f"frame {referrer.f_code.co_name} ({os.path.basename(referrer.f_code.co_filename)}:{referrer.f_lineno})"
        if isinstance(referrer, types.FunctionType):
            return f"function {referrer.__module__}.{referrer.__qualname__}"
        if isinstance(referrer, types.MethodType):
            return f"bound method {getattr(referrer.__func__, '__qualname__', '?')}"
        if isinstance(referrer, types.CellType):
            return "closure cell"
        if isinstance(referrer, types.ModuleType):
            return f"module {referrer.__name__}"
        if isinstance(referrer, dict):
            keys = [k for k, v in list(referrer.items()) if v is child][:3]
            return f"dict[{len(referrer)}] holding it under {keys!r}"
        if isinstance(referrer, (list, tuple, set, frozenset)):
            return f"{type(referrer).__name__}[{len(referrer)}]"
        if isinstance(referrer, type):
            return f"class {referrer.__module__}.{referrer.__qualname__}"
        if isinstance(referrer, asyncio.Task):
            return f"asyncio.Task {referrer.get_name()!r} done={referrer.done()}"
        held_as = [k for k, v in list(getattr(referrer, "__dict__", {}).items()) if v is child][:3]
        suffix = f" holding it as {held_as!r}" if held_as else ""
        return f"{type(referrer).__module__}.{type(referrer).__qualname__}{suffix}"
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never raise out of the assertion it explains
        return f"{type(referrer).__name__} (could not be described: {type(exc).__name__})"


def _own_graph(root: object, limit: int = 300_000) -> set[int]:
    """The ids of everything ``root`` itself reaches (its routers, closures, state): referrers inside this set are the app holding
    itself together, not something that pins it, so the walk below does not report them.

    Classes, modules and frames are places things are kept, not parts of the app, and a function is followed through its closure and
    defaults but not its globals, so the walk never wanders into a whole module's namespace and mistakes the pin for part of the app.
    """
    seen = {id(root)}
    stack = [root]
    while stack and len(seen) < limit:
        obj = stack.pop()
        if isinstance(obj, types.FunctionType):
            kids = [*(obj.__closure__ or ()), *(obj.__defaults__ or ()), *(obj.__kwdefaults__ or {}).values()]
        elif isinstance(obj, (type, types.ModuleType, types.FrameType)):
            continue
        else:
            kids = gc.get_referents(obj)
        for kid in kids:
            if id(kid) not in seen and not isinstance(kid, (type, types.ModuleType)):
                seen.add(id(kid))
                stack.append(kid)
    return seen


def _describe_apps(apps: list[FastAPI]) -> str:
    """What refers to each of ``apps`` from OUTSIDE it: up to ``_REFERRER_HOPS`` hops of ``gc.get_referrers``, a few referrers per object.

    Frames of this module (the test and these helpers, whose locals reference what they are looking at), the containers the walk
    itself builds and everything the app reaches by itself are left out, so what is printed is the pin and not the measuring or the
    app's own wiring. Printed as a tree: each line is a referrer of the line above it with less indentation.
    """
    lines: list[str] = []
    ignore: set[int] = {id(apps), id(lines)}  # the walk's own containers and the caller's list of apps, never reported
    this_file = os.path.realpath(__file__)

    def walk(child: object, depth: int, seen: set[int]) -> None:
        refs = gc.get_referrers(child)
        ignore.add(id(refs))
        shown = 0
        for ref in refs:
            if id(ref) in seen or id(ref) in ignore:
                continue
            if isinstance(ref, types.FrameType) and os.path.realpath(ref.f_code.co_filename) == this_file:
                continue
            seen.add(id(ref))
            lines.append(f"{'  ' * depth}<- {_label(ref, child)}")
            shown += 1
            if depth < _REFERRER_HOPS:
                walk(ref, depth + 1, seen)
            if shown >= _REFERRERS_PER_OBJECT:
                break
        del refs

    for number in range(1, min(len(apps), _APPS_SHOWN) + 1):  # indexed, so no slice or iterator of the apps is left lying around
        lines.append(f"lingering app #{number} (title={getattr(apps[number - 1], 'title', '?')!r}):")
        seen = _own_graph(apps[number - 1])
        ignore.add(id(seen))
        walk(apps[number - 1], 1, seen)
        del seen
    ignore.clear()
    return "\n".join(lines)


_guard = _Guard()


@pytest.mark.asyncio
@pytest.mark.parametrize("i", range(_REPEATS))
async def test_repeated_app_fixtures_do_not_accumulate(app: FastAPI, i: int) -> None:
    # Without this the test would pass vacuously if the fixture stopped
    # starting the mount (the very mount whose task group caused the leak).
    assert app.state.mcp_session_manager is not None

    apps, states = await _settled_counts(_guard)
    problems = _guard.problems(apps, states)
    detail = ""
    if problems:
        # Built here and kept as text: the apps themselves must not stay referenced from this frame, or pytest's traceback would pin
        # them for the tests that follow.
        lingering = [a for a in _live_fastapi_apps() if a is not app]
        detail = "\n" + _describe_apps(lingering)
        del lingering
    _guard.observe(apps, states)

    # Both are evaluated before asserting so a failure reports each one that
    # tripped, rather than the first hiding the second.
    assert not problems, f"test #{i}: " + "; ".join(problems) + detail


# ---- the guard's own tests -------------------------------------------------------------------------------------------------
#
# Defined AFTER the parametrized guard so that, run in file order, they cannot move its baseline; each cleans up what it makes.


@pytest.mark.parametrize(
    "samples",
    [
        [14, 14, 14, 14, 14, 14],            # steady: the healthy case
        [19, 14, 14, 14, 14, 14],            # a batch release right after the first sample: the failure of run 37739441310
        [19, 19, 14, 14, 14, 14],            # the same one test later
        [14, 13, 13, 13, 13, 13],            # a count that only ever goes down
    ],
)
def test_the_guard_accepts_a_steady_count_and_a_batch_release(samples: list[int]) -> None:
    guard = _Guard()
    for apps in samples:
        assert guard.problems(apps, 5) == [], (samples, apps)
        guard.observe(apps, 5)


@pytest.mark.parametrize(
    ("samples", "first_flagged"),
    [
        ([12, 13, 14, 15, 16, 17], 1),       # a real leak of one app per test, flagged on the second test it shows
        ([14, 15, 16, 17, 18], 1),           # the same, from the lowest point
        ([19, 14, 15, 16, 17, 18], 2),       # what run 37739441310 actually showed: still red, once the batch is out of the way
    ],
)
def test_the_guard_still_catches_one_more_app_per_test(samples: list[int], first_flagged: int) -> None:
    guard = _Guard()
    flagged = []
    for n, apps in enumerate(samples):
        if guard.problems(apps, 5):
            flagged.append(n)
        guard.observe(apps, 5)
    assert flagged and flagged[0] == first_flagged and flagged == list(range(first_flagged, len(samples))), flagged


def test_the_guard_catches_anyio_task_states_that_grow() -> None:
    guard = _Guard()
    guard.observe(14, 5)
    assert guard.problems(14, 5) == []
    assert any("task-state" in p for p in guard.problems(14, 6))
    guard.observe(14, 4)  # a drop lowers the bar, as for apps
    assert any("task-state" in p for p in guard.problems(14, 5))


_deliberate_leak: list[FastAPI] = []


class _Holder:
    def __init__(self) -> None:
        self.apps: list[FastAPI] = []


def test_a_deliberate_leak_is_counted_flagged_and_its_pin_is_named() -> None:
    """Real objects: apps pinned in a module-level list grow the live count by one each time, the guard flags every step, and the
    report names the list and the module-level name that holds it."""
    guard = _Guard()
    try:
        guard.observe(*_counts())
        for step in range(1, 4):
            _deliberate_leak.append(FastAPI(title=f"deliberate-leak-{step}"))
            apps, states = _counts()
            assert guard.problems(apps, states), f"step {step}: {apps} apps went unnoticed"

        report = _describe_apps(list(_deliberate_leak))

        assert "deliberate-leak-1" in report
        assert f"list[{len(_deliberate_leak)}]" in report, report
        assert "'_deliberate_leak'" in report, report  # the module dict names the global that holds the list
    finally:
        _deliberate_leak.clear()
        _collect()


def test_a_pin_through_an_attribute_is_named_by_its_attribute() -> None:
    holder = _Holder()
    holder.apps.append(FastAPI(title="held-by-an-attribute"))

    report = _describe_apps(list(holder.apps))

    assert "held-by-an-attribute" in report
    assert "_Holder" in report and "'apps'" in report, report


def test_apps_that_are_dropped_are_not_counted() -> None:
    """The control for the leak test: the same construction without the pin leaves the count where it was."""
    before, _ = _counts()
    for step in range(3):
        FastAPI(title=f"dropped-{step}")
    after, _ = _counts()

    assert after == before


@pytest.mark.asyncio
async def test_an_app_that_is_released_a_moment_late_is_not_a_leak() -> None:
    """The settle loop: the count is over the lowest at first and back to it within the rounds, so the guard does not fire."""
    guard = _Guard()
    guard.observe(*_counts())
    late: list[FastAPI] = [FastAPI(title="released-late")]
    asyncio.get_running_loop().call_later(_SETTLE_SECONDS * 2.5, late.clear)

    apps, states = await _settled_counts(guard)

    assert guard.problems(apps, states) == [], (apps, guard.lowest_apps)


@pytest.mark.asyncio
async def test_an_app_that_is_never_released_is_still_a_leak_after_settling() -> None:
    guard = _Guard()
    guard.observe(*_counts())
    try:
        _deliberate_leak.append(FastAPI(title="never-released"))

        apps, states = await _settled_counts(guard)

        assert guard.problems(apps, states), "waiting made a real leak look fine"
    finally:
        _deliberate_leak.clear()
        _collect()
