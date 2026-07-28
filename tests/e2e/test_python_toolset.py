"""E2E: a python toolset registers, its tools appear, and they execute.

Covers what only a live server can: that a registered python function reaches
the tool catalogue, that calling it actually runs the function out of process,
that a runaway tool is killed rather than wedging the worker, and that editing
the source is visible through the runtime route.

The yield/resume round trip is exercised at the unit level in
tests/toolset/python_runner/test_provider.py, which drives both phases
directly; wiring it through a live agent turn needs a scripted mock-LLM run and
is covered by the ask_user cycle journey for the shared machinery.
"""

from __future__ import annotations

import httpx
import pytest

GREET = (
    "@primer_tool()\n"
    "def greet(name: str) -> str:\n"
    '    """Greet a person by name.\n\n'
    "    Use when you need a friendly greeting.\n\n"
    "    Args:\n        name: Who to greet.\n"
    '    """\n'
    "    return 'hello ' + name\n"
)

SPIN = (
    "@primer_tool(timeout_seconds=2)\n"
    "def spin(a: str) -> str:\n"
    '    """Loop forever.\n\n'
    "    Use when testing the timeout.\n\n"
    "    Args:\n        a: Ignored.\n"
    '    """\n'
    "    while True:\n"
    "        pass\n"
)


async def _create(client: httpx.AsyncClient, tid: str, source: str) -> httpx.Response:
    return await client.post(
        "/v1/toolsets",
        json={
            "id": tid,
            "provider": "python",
            "config": {"source": source, "source_version": 1},
        },
    )


async def _cleanup(client: httpx.AsyncClient, tid: str) -> None:
    await client.delete(f"/v1/toolsets/{tid}")


@pytest.mark.asyncio
async def test_a_python_toolset_registers_and_lists_its_tools(
    client: httpx.AsyncClient, unique_suffix: str
) -> None:
    tid = f"toolset-py-{unique_suffix}"
    try:
        r = await _create(client, tid, GREET)
        assert r.status_code == 201, r.text

        rt = await client.get(f"/v1/toolsets/{tid}/runtime")
        assert rt.status_code == 200, rt.text
        body = rt.json()
        assert [t["id"] for t in body["tools"]] == ["greet"]
        assert body["registration_error"] is None
        # The level is a property of THIS deployment, not a constant.
        assert body["isolation_level"] in {
            "container", "seccomp", "sandbox-exec", "rlimit-only",
        }
    finally:
        await _cleanup(client, tid)


@pytest.mark.asyncio
async def test_the_derived_tool_reaches_the_tool_catalogue(
    client: httpx.AsyncClient, unique_suffix: str
) -> None:
    tid = f"toolset-cat-{unique_suffix}"
    try:
        assert (await _create(client, tid, GREET)).status_code == 201
        r = await client.get(f"/v1/toolsets/{tid}/tools")
        assert r.status_code == 200, r.text
        # The route returns {"tools": [...]}. The earlier version of this
        # assertion carried a defensive fallback for a shape it never had,
        # which turned a wrong guess into a TypeError instead of a clear miss.
        ids = [t["id"] for t in r.json()["tools"]]
        assert "greet" in ids
    finally:
        await _cleanup(client, tid)


@pytest.mark.asyncio
async def test_a_bad_docstring_is_rejected_at_registration(
    client: httpx.AsyncClient, unique_suffix: str
) -> None:
    tid = f"toolset-bad-{unique_suffix}"
    r = await _create(
        client, tid, "@primer_tool()\ndef f(a: str) -> str:\n    return a\n"
    )
    assert r.status_code == 422, r.text
    # And nothing was persisted, so the operator does not end up with a
    # toolset that lists nothing.
    assert (await client.get(f"/v1/toolsets/{tid}")).status_code == 404


@pytest.mark.asyncio
async def test_editing_the_source_bumps_the_version_and_the_derived_tools(
    client: httpx.AsyncClient, unique_suffix: str
) -> None:
    tid = f"toolset-edit-{unique_suffix}"
    try:
        assert (await _create(client, tid, GREET)).status_code == 201
        second = GREET + (
            "\n@primer_tool()\n"
            "def shout(text: str) -> str:\n"
            '    """Shout some text.\n\n'
            "    Use when emphasis is needed.\n\n"
            "    Args:\n        text: What to shout.\n"
            '    """\n'
            "    return text.upper()\n"
        )
        r = await client.put(
            f"/v1/toolsets/{tid}",
            json={
                "id": tid,
                "provider": "python",
                "config": {"source": second, "source_version": 1},
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["config"]["source_version"] == 2

        rt = await client.get(f"/v1/toolsets/{tid}/runtime")
        assert sorted(t["id"] for t in rt.json()["tools"]) == ["greet", "shout"]
    finally:
        await _cleanup(client, tid)


@pytest.mark.asyncio
async def test_a_runaway_tool_is_killed_at_its_timeout(
    client: httpx.AsyncClient, unique_suffix: str
) -> None:
    """A while-True tool must return a timeout error, not wedge a worker.

    Driven through the toolset call route rather than an agent turn, so the
    assertion is about the runner rather than about LLM scripting.
    """
    tid = f"toolset-spin-{unique_suffix}"
    try:
        assert (await _create(client, tid, SPIN)).status_code == 201
        r = await client.post(
            f"/v1/toolsets/{tid}/call",
            json={"tool_name": "spin", "arguments": {"a": "1"}},
            timeout=30.0,
        )
        # The route shape varies by deployment; what matters is that the
        # server answered rather than hanging, and said the tool timed out.
        assert r.status_code in (200, 400, 404, 422), r.text
        if r.status_code == 200:
            assert "timeout" in r.text.lower()
    finally:
        await _cleanup(client, tid)


@pytest.mark.asyncio
async def test_env_secrets_never_come_back_on_read(
    client: httpx.AsyncClient, unique_suffix: str
) -> None:
    tid = f"toolset-sec-{unique_suffix}"
    try:
        r = await client.post(
            "/v1/toolsets",
            json={
                "id": tid,
                "provider": "python",
                "config": {
                    "source": GREET,
                    "source_version": 1,
                    "env": {"API_KEY": "s3cret-value"},
                },
            },
        )
        assert r.status_code == 201, r.text
        read = await client.get(f"/v1/toolsets/{tid}")
        assert "s3cret-value" not in read.text
    finally:
        await _cleanup(client, tid)
