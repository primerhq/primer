"""The /v1/tools catalogue must survive a toolset that never answers.

The route's docstring already promised this: a broken toolset "is reported
with ``available: false`` and the failure reason instead of bringing down
the whole catalogue". That was implemented as a ``try/except`` around each
toolset's ``list_tools`` -- which covers a toolset that *fails*, but not one
that *hangs*.

An MCP server whose process is wedged still accepts the TCP connection and
then never replies. Nothing is raised, so the ``except`` never runs and the
request blocks forever, taking the Toolsets and Tools pages with it. Seen in
production: a Deployment reporting 1/1 Running whose endpoint answered
neither POST nor GET.

These pin the bound rather than the exception handling, because the bound is
what was missing.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.api.routers import providers as providers_mod


class _Tool:
    def __init__(self, tid: str) -> None:
        self.id = tid
        self.description = "does a thing"
        self.args_schema = {"type": "object"}


class _HealthyProvider:
    async def list_tools(self, *, principal=None):  # noqa: ANN001
        for t in ("alpha", "beta"):
            yield _Tool(t)


class _HangingProvider:
    """Accepts the call and never yields -- a wedged MCP server."""

    async def list_tools(self, *, principal=None):  # noqa: ANN001
        await asyncio.sleep(3600)
        yield _Tool("never")  # pragma: no cover


class _RaisingProvider:
    async def list_tools(self, *, principal=None):  # noqa: ANN001
        raise ConnectionRefusedError("connection refused")
        yield  # pragma: no cover - makes this an async generator


class _GroupRaisingProvider:
    """HTTP MCP runs in anyio task groups, so errors arrive wrapped."""

    async def list_tools(self, *, principal=None):  # noqa: ANN001
        raise BaseExceptionGroup(  # noqa: TRY003
            "unhandled errors in a TaskGroup",
            [ConnectionRefusedError("connection refused")],
        )
        yield  # pragma: no cover


class _Registry:
    def __init__(self, provider) -> None:  # noqa: ANN001
        self._provider = provider

    async def get_toolset(self, tid: str):  # noqa: ANN001, ARG002
        return self._provider


@pytest.fixture
def fast_bound(monkeypatch) -> float:
    """Shrink the production bound so the hang test stays quick."""
    monkeypatch.setattr(providers_mod, "_CATALOGUE_PROBE_TIMEOUT_S", 0.25)
    return 0.25


@pytest.mark.asyncio
async def test_a_healthy_toolset_returns_its_tools() -> None:
    tools, reason = await providers_mod._catalogue_tools(
        _Registry(_HealthyProvider()), "ts", None,
    )
    assert reason is None
    assert [t["id"] for t in tools] == ["alpha", "beta"]
    assert [t["scoped_id"] for t in tools] == ["ts__alpha", "ts__beta"]


@pytest.mark.asyncio
async def test_a_hanging_toolset_is_bounded_not_awaited_forever(
    fast_bound: float,
) -> None:
    """The regression. Before the bound this never returned."""
    # wait_for so a regression fails the test instead of hanging the suite.
    tools, reason = await asyncio.wait_for(
        providers_mod._catalogue_tools(
            _Registry(_HangingProvider()), "wedged", None,
        ),
        timeout=10.0,
    )
    assert tools == []
    assert reason is not None
    assert "did not respond" in reason


@pytest.mark.asyncio
async def test_the_bound_is_reported_as_the_reason(fast_bound: float) -> None:
    # The operator needs to know it timed out, not just that it is
    # unavailable -- a timeout and a refused connection call for different
    # fixes.
    _tools, reason = await asyncio.wait_for(
        providers_mod._catalogue_tools(
            _Registry(_HangingProvider()), "wedged", None,
        ),
        timeout=10.0,
    )
    assert "0.25s" in reason


@pytest.mark.asyncio
async def test_a_failing_toolset_still_reports_its_error() -> None:
    _tools, reason = await providers_mod._catalogue_tools(
        _Registry(_RaisingProvider()), "refused", None,
    )
    assert reason is not None
    assert "ConnectionRefusedError" in reason


@pytest.mark.asyncio
async def test_a_taskgroup_wrapped_error_is_unwrapped_to_its_leaf() -> None:
    # Reporting the group gives the operator "ExceptionGroup: unhandled
    # errors in a TaskGroup (1 sub-exception)", which names nothing useful.
    _tools, reason = await providers_mod._catalogue_tools(
        _Registry(_GroupRaisingProvider()), "wrapped", None,
    )
    assert reason is not None
    assert "ConnectionRefusedError" in reason
    assert "TaskGroup" not in reason


@pytest.mark.asyncio
async def test_one_wedged_toolset_does_not_stop_the_others(
    fast_bound: float,
) -> None:
    """The whole point: the catalogue keeps going."""

    class _Mixed:
        async def get_toolset(self, tid: str):  # noqa: ANN001
            return _HangingProvider() if tid == "wedged" else _HealthyProvider()

    reg = _Mixed()
    results = {}
    for tid in ("good-1", "wedged", "good-2"):
        results[tid] = await asyncio.wait_for(
            providers_mod._catalogue_tools(reg, tid, None), timeout=10.0,
        )

    assert results["good-1"][1] is None
    assert results["good-2"][1] is None
    assert len(results["good-1"][0]) == 2
    assert len(results["good-2"][0]) == 2
    assert results["wedged"][1] is not None
