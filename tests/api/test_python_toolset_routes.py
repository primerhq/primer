"""Python toolsets over REST: create, validate, read back."""

from __future__ import annotations

import pytest

from primer.api.registries import ProviderRegistry
from primer.model.providers.toolset import ToolsetProviderType


@pytest.fixture
def fake_provider_registry(fake_storage_provider):
    """Override the module default so python toolsets build a REAL provider.

    tests/api/conftest.py stubs toolset_factory with object(), which is fine
    for routes that never touch the provider. The runtime route does, so a
    stub would make the test assert nothing.
    """

    def _toolset_factory(toolset):
        if toolset.provider == ToolsetProviderType.PYTHON:
            from primer.toolset.python_runner.provider import PythonToolsetProvider
            from primer.toolset.python_runner.runners import LocalHardenedRunner

            return PythonToolsetProvider(
                toolset_id=toolset.id,
                config=toolset.config,
                runner=LocalHardenedRunner(),
            )
        return object()

    return ProviderRegistry(
        fake_storage_provider,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=_toolset_factory,
    )


GOOD = (
    "@primer_tool()\n"
    "def greet(name: str) -> str:\n"
    '    """Greet a person.\n\n    Use when greeting.\n\n'
    '    Args:\n        name: Who.\n    """\n'
    "    return 'hi ' + name\n"
)


async def _create(client, tid: str, source: str = GOOD, **cfg):
    body = {
        "id": tid,
        "provider": "python",
        "config": {"source": source, "source_version": 1, **cfg},
    }
    return await client.post("/v1/toolsets", json=body)


@pytest.mark.asyncio
async def test_create_a_python_toolset(client) -> None:
    r = await _create(client, "toolset-py")
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_a_missing_docstring_is_a_422_naming_the_function(client) -> None:
    r = await _create(
        client, "toolset-bad",
        source="@primer_tool()\ndef f(a: str) -> str:\n    return a\n",
    )
    assert r.status_code == 422
    assert "f" in r.text


@pytest.mark.asyncio
async def test_a_syntax_error_is_a_422_with_a_line_number(client) -> None:
    r = await _create(client, "toolset-bad2", source="def f(:\n")
    assert r.status_code == 422
    assert "1" in r.text


@pytest.mark.asyncio
async def test_an_undocumented_parameter_is_rejected(client) -> None:
    src = (
        "@primer_tool()\n"
        "def f(a: str, b: int) -> str:\n"
        '    """Do it.\n\n    Use when you must.\n\n    Args:\n        a: A.\n    """\n'
        "    return a\n"
    )
    r = await _create(client, "toolset-bad3", source=src)
    assert r.status_code == 422
    assert "b" in r.text


@pytest.mark.asyncio
async def test_env_values_are_masked_on_read(client) -> None:
    await _create(client, "toolset-sec", env={"API_KEY": "s3cret"})
    r = await client.get("/v1/toolsets/toolset-sec")
    assert r.status_code == 200
    assert "s3cret" not in r.text


@pytest.mark.asyncio
async def test_editing_source_bumps_the_version_server_side(client) -> None:
    await _create(client, "toolset-v")
    r = await client.put("/v1/toolsets/toolset-v", json={
        "id": "toolset-v", "provider": "python",
        # The client sends the version it read; the server owns the bump so
        # two concurrent editors cannot land on the same number.
        "config": {"source": GOOD + "\n", "source_version": 1},
    })
    assert r.status_code == 200, r.text
    assert r.json()["config"]["source_version"] == 2


@pytest.mark.asyncio
async def test_an_unchanged_source_does_not_bump_the_version(client) -> None:
    await _create(client, "toolset-v2")
    r = await client.put("/v1/toolsets/toolset-v2", json={
        "id": "toolset-v2", "provider": "python",
        "config": {"source": GOOD, "source_version": 1, "allow_network": True},
    })
    assert r.status_code == 200, r.text
    assert r.json()["config"]["source_version"] == 1


@pytest.mark.asyncio
async def test_an_update_with_bad_source_is_rejected(client) -> None:
    await _create(client, "toolset-v3")
    r = await client.put("/v1/toolsets/toolset-v3", json={
        "id": "toolset-v3", "provider": "python",
        "config": {"source": "def (", "source_version": 1},
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_the_runtime_route_reports_isolation_and_tools(client) -> None:
    await _create(client, "toolset-rt")
    r = await client.get("/v1/toolsets/toolset-rt/runtime")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["isolation_level"] in {
        "container", "seccomp", "sandbox-exec", "rlimit-only",
    }
    assert [t["id"] for t in body["tools"]] == ["greet"]
    assert body["registration_error"] is None


@pytest.mark.asyncio
async def test_a_round_tripped_config_can_be_put_back(client) -> None:
    """The console saves by GETting the toolset and PUTting config back.

    Any field the read path adds or transforms has to survive that round trip,
    or every save from the editor fails validation before it reaches the
    registration check.
    """
    await _create(client, "toolset-rt2")
    got = await client.get("/v1/toolsets/toolset-rt2")
    assert got.status_code == 200
    config = dict(got.json()["config"])
    config["source"] = GOOD + "\n"

    r = await client.put("/v1/toolsets/toolset-rt2", json={
        "id": "toolset-rt2", "provider": "python", "config": config,
    })
    assert r.status_code == 200, r.text
