"""Count what a code path asks of its collaborators (storage, event store, embedder, vector store): the cheap, CI-safe way to show a perf gain.

A perf test asserts an upper bound on COUNTS (queries per request, rows decoded, embed calls, connections held), never on wall time. Wrap
a provider once and read a frozen snapshot after the code under test ran::

    sp = CountingStorageProvider(fake_storage_provider)
    with sp.counted() as c:
        await handler(sp)
    assert c.calls("find", model="Workspace") <= 2
    assert c.rows <= 200

Every wrapped object is a transparent proxy: the method still runs on the inner object and returns its result, so the same test
fixtures produce the same results through it (``tests/perf/test_counting_harness.py`` pins that). Counting is on only inside
``counted()``; outside it the proxy just forwards.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Counts:
    """A tally of calls by ``(label, method)`` plus the rows the calls returned."""

    by_call: Counter = field(default_factory=Counter)
    rows: int = 0
    log: list[tuple[str, str]] = field(default_factory=list)

    def calls(self, *methods: str, model: str | None = None, label: str | None = None) -> int:
        """Calls to any of ``methods`` (all when none given), optionally for one model or one wrapped object label."""
        want = set(methods)
        total = 0
        for (lab, method), n in self.by_call.items():
            if want and method not in want:
                continue
            if model is not None and lab != model and not lab.endswith(f":{model}"):
                continue
            if label is not None and lab.split(":", 1)[0] != label:
                continue
            total += n
        return total

    @property
    def total(self) -> int:
        return sum(self.by_call.values())

    def freeze(self) -> "Counts":
        return Counts(Counter(self.by_call), self.rows, list(self.log))


class _Recorder:
    """The live tally the proxies write to while a ``counted()`` block is open."""

    def __init__(self) -> None:
        self.active: Counts | None = None

    def record(self, label: str, method: str, result: Any) -> None:
        counts = self.active
        if counts is None:
            return
        counts.by_call[(label, method)] += 1
        counts.log.append((label, method))
        counts.rows += _rows_of(result)

    @contextlib.contextmanager
    def counted(self) -> Iterator[Counts]:
        counts = Counts()
        previous, self.active = self.active, counts
        try:
            yield counts
        finally:
            self.active = previous


def _rows_of(result: Any) -> int:
    """How many stored rows a call handed back: a page's items, a list, or one model for a hit."""
    if result is None or isinstance(result, (bool, int, str, bytes, dict)):
        return 0
    items = getattr(result, "items", None)
    if isinstance(items, list):
        return len(items)
    if isinstance(result, (list, tuple)):
        return len(result)
    return 1 if hasattr(result, "model_dump") else 0


class CountingProxy:
    """Forward every attribute to ``inner``; count the awaited and the plain method calls while a ``counted()`` block is open."""

    def __init__(self, inner: Any, recorder: _Recorder, label: str) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_recorder", recorder)
        object.__setattr__(self, "_label", label)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr) or name.startswith("_"):
            return attr
        recorder, label = self._recorder, self._label
        if inspect.iscoroutinefunction(attr):
            async def awaited(*args: Any, **kwargs: Any) -> Any:
                result = await attr(*args, **kwargs)
                recorder.record(label, name, result)
                return result

            return awaited

        def plain(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            if inspect.isawaitable(result) or inspect.isasyncgen(result) or inspect.isgenerator(result):
                recorder.record(label, name, None)
                return result
            recorder.record(label, name, result)
            return result

        return plain

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)


class CountingStorageProvider:
    """Wraps a ``StorageProvider``: ``get_storage(Model)`` and ``get_event_store()`` hand out counting proxies; the rest forwards."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._recorder = _Recorder()
        self._proxies: dict[Any, CountingProxy] = {}

    def counted(self) -> contextlib.AbstractContextManager[Counts]:
        return self._recorder.counted()

    def get_storage(self, model_class: type) -> CountingProxy:
        key = ("storage", model_class)
        if key not in self._proxies:
            self._proxies[key] = CountingProxy(self._inner.get_storage(model_class), self._recorder, f"storage:{model_class.__name__}")
        return self._proxies[key]

    def get_event_store(self) -> CountingProxy:
        if "events" not in self._proxies:
            self._proxies["events"] = CountingProxy(self._inner.get_event_store(), self._recorder, "events")
        return self._proxies["events"]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def counted(provider: CountingStorageProvider) -> contextlib.AbstractContextManager[Counts]:
    """``with counted(sp) as c:`` for a wrapped provider."""
    return provider.counted()


class CountingEmbedder(CountingProxy):
    """Counts calls to a wrapped embedder (``embed`` and friends); ``texts`` is the number of inputs embedded."""

    def __init__(self, inner: Any, recorder: _Recorder | None = None) -> None:
        super().__init__(inner, recorder or _Recorder(), "embedder")

    def counted(self) -> contextlib.AbstractContextManager[Counts]:
        return self._recorder.counted()


class CountingVectorStore(CountingProxy):
    """Counts calls to a wrapped vector store (``put``, ``put_many``, ``search`` ...)."""

    def __init__(self, inner: Any, recorder: _Recorder | None = None) -> None:
        super().__init__(inner, recorder or _Recorder(), "vector")

    def counted(self) -> contextlib.AbstractContextManager[Counts]:
        return self._recorder.counted()


async def loop_ticks(duration_ticks: int = 5, interval: float = 0.01) -> int:
    """Run ``duration_ticks`` short sleeps and return how many completed: a task started alongside the code under test proves the loop kept turning.

    Assert ``ticks == duration_ticks`` AFTER awaiting the code under test with ``asyncio.gather(code(), loop_ticks())``; a coroutine that
    blocked the loop in a sync call still lets the ticks complete afterwards, so use this to prove interleaving (the tick ran BEFORE the
    code finished), via the ``order`` list the test appends to, not a duration.
    """
    done = 0
    for _ in range(duration_ticks):
        await asyncio.sleep(interval)
        done += 1
    return done
