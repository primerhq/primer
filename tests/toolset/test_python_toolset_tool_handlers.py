"""The three Python-toolset tools say what their handlers do, and refuse what they cannot handle (ticket 01a16f1d, from #933's source corrections).

* Ruling 1: an edit that changes the source moves ``source_version``, and a call parked in one of the toolset's tools is then refused when it is
  answered (``PythonToolsetProvider.resume_tool``: "... was edited while this call was parked ...; the answer was not applied"). The update tool's
  description and the ``PythonConfig.source_version`` field said the call "resumes against the code that parked". They now say what happens.
* Ruling 2: the descriptions gave the result as ``tools: [{id, yields, args}]``. Each tool also carries ``timeout_seconds``, and ``args`` is the list
  of argument names. The keys each description shows are compared with a real handler result here.
* Ruling 3: given the id of a toolset that is not a Python toolset (an MCP one), ``list_python_tools`` and ``update_python_toolset_source`` raised
  ``AttributeError`` (``McpConfig`` has no ``source``). They answer ``type=validation-error`` naming the id.
* Ruling 5: ``update_python_toolset_source`` bumped the version even for the same source, so a no-op update voided a parked call. It now keeps the
  version when the source is unchanged, through the helper the REST route uses (``toolset_checks.own_python_source_version``).

The handlers run on the fake storage and a park is made and answered through a real ``PythonToolsetProvider`` with the local runner, so the version a
park carries and the check the resume makes are the production ones.
"""

from __future__ import annotations

import json
import re

import pytest

from primer.model.chat import ToolCallResult
from primer.model.principal import PrincipalRef
from primer.model.provider import Toolset
from primer.model.providers.toolset import PythonConfig
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.toolset._python_tools import build_python_toolset_tools
from primer.toolset.python_runner.provider import PythonToolsetProvider
from primer.toolset.python_runner.runners import LocalHardenedRunner

GREET = '''
@primer_tool()
def greet(name: str) -> str:
    """Greet a person by name.

    Use when you need a friendly greeting.

    Args:
        name: Who to greet.
    """
    return "hello " + name
'''

ASK = GREET + '''

@primer_tool(timeout_seconds=20)
def ask(question: str, ctx) -> str:
    """Ask the operator something.

    Use when a human must decide.

    Args:
        question: What to ask.
    """
    return ask_user(question)


@resumes(ask)
def _ask_resume(payload: dict, meta: dict) -> str:
    """Return the answer.

    Use when resuming.

    Args:
        payload: The response payload.
        meta: The resume metadata.
    """
    return payload["response"]
'''

BROKEN = "@primer_tool()\ndef nodoc(a: str) -> str:\n    return a\n"

MCP_ROW = {"id": "kb", "provider": "mcp", "config": {"transport": "http", "config": {"url": "https://mcp.example/mcp", "headers": {}}}}

CTX = ToolContext(
    tool_call_id="tc-1", session_id="s-1", workspace_id=None,
    initiated_by=PrincipalRef(type="user", id="user-1", display="user", role="admin", source="local"),
)


@pytest.fixture
def tools(fake_storage_provider):
    return build_python_toolset_tools(storage_provider=fake_storage_provider, toolset_id="crud")


async def _call(tools, name: str, **arguments) -> tuple[ToolCallResult, dict]:
    _tool, handler = tools[name]
    result = await handler(arguments, CTX)
    return result, json.loads(result.output)


def _shape(text: str) -> tuple[set[str], set[str]]:
    """The keys of a ``{ok: true, a, b, tools: [{x, y}]}`` shape: the top-level keys, and the keys of each tool (empty when it lists none)."""
    match = re.fullmatch(r"\{(.*)\}", text.strip("`"))
    assert match, text
    body = match.group(1)
    inner = re.search(r"\[\{(.*?)\}\]", body)
    tool_keys = {key.strip() for key in inner.group(1).split(",")} if inner else set()
    top = re.sub(r"\[\{.*?\}\]", "", body)
    return {part.split(":")[0].strip() for part in top.split(",") if part.strip()}, tool_keys


def _described_failure(description: str) -> set[str]:
    """The keys of the registration failure a description names, ``{ok: false, error, field, lineno}``: the ``{ok: false, ...}`` shape that lists
    keys only (``{ok: false, error: not-found}`` is the unknown-id answer, a value)."""
    shapes = [found for found in re.findall(r"``\{ok: false, ([^}]*)\}``", description) if ":" not in found]
    assert len(shapes) == 1, description
    return {"ok", *(key.strip() for key in shapes[0].split(","))}


async def _provider(fake_storage_provider, toolset_id: str) -> PythonToolsetProvider:
    """The provider the registry would build from the stored row now (the update evicts the cached one)."""
    row = await fake_storage_provider.get_storage(Toolset).get(toolset_id)
    return PythonToolsetProvider(toolset_id=toolset_id, config=row.config.model_copy(deep=True), runner=LocalHardenedRunner())


# ---- ruling 2: the result shape each description shows is the one the handler returns ----------------------------------------------------------------------


async def test_each_description_shows_the_keys_its_handler_returns(tools) -> None:
    created, created_body = await _call(tools, "create_python_toolset", toolset_id="py-1", source=GREET)
    updated, updated_body = await _call(tools, "update_python_toolset_source", toolset_id="py-1", source=GREET + "\n# edited\n")
    listed, listed_body = await _call(tools, "list_python_tools", toolset_id="py-1")
    assert not (created.is_error or updated.is_error or listed.is_error)

    for name, body in (("create_python_toolset", created_body), ("update_python_toolset_source", updated_body), ("list_python_tools", listed_body)):
        tool = tools[name][0]
        returns = tool.examples[0].returns
        assert returns in tool.description, name
        top, per_tool = _shape(returns)
        assert top == set(body), (name, top, set(body))
        assert per_tool == set(body["tools"][0]), (name, per_tool, set(body["tools"][0]))


async def test_args_is_the_list_of_argument_names_and_the_descriptions_say_so(tools) -> None:
    _result, body = await _call(tools, "create_python_toolset", toolset_id="py-1", source=ASK)

    by_id = {tool["id"]: tool for tool in body["tools"]}
    assert by_id["greet"] == {"id": "greet", "yields": False, "timeout_seconds": 30.0, "args": ["name"]}
    assert by_id["ask"] == {"id": "ask", "yields": True, "timeout_seconds": 20.0, "args": ["question"]}, "ctx is not an argument"
    for name in ("create_python_toolset", "update_python_toolset_source", "list_python_tools"):
        assert "argument names" in tools[name][0].description, name


@pytest.mark.parametrize("name", ["create_python_toolset", "update_python_toolset_source"])
async def test_the_described_registration_failure_is_the_one_returned(tools, name) -> None:
    if name == "update_python_toolset_source":
        await _call(tools, "create_python_toolset", toolset_id="py-1", source=GREET)

    result, body = await _call(tools, name, toolset_id="py-1", source=BROKEN)

    assert not result.is_error and body["ok"] is False, "a source that does not register is an {ok: false} answer, not an error result"
    assert _described_failure(tools[name][0].description) == set(body)


# ---- ruling 3: a toolset that is not a Python toolset is a typed refusal, never a crash -------------------------------------------------------------------


@pytest.mark.parametrize("name, extra", [("list_python_tools", {}), ("update_python_toolset_source", {"source": GREET})])
async def test_a_toolset_that_is_not_a_python_toolset_is_a_validation_error(tools, fake_storage_provider, name, extra) -> None:
    store = fake_storage_provider.get_storage(Toolset)
    await store.create(Toolset.model_validate(MCP_ROW))
    before = (await store.get("kb")).model_dump(mode="json")

    result, body = await _call(tools, name, toolset_id="kb", **extra)

    assert result.is_error and body["type"] == "validation-error"
    assert "'kb'" in body["message"] and "not a Python toolset" in body["message"]
    assert (await store.get("kb")).model_dump(mode="json") == before, "the refused call changed the row"


@pytest.mark.parametrize("name", ["list_python_tools", "update_python_toolset_source"])
def test_the_descriptions_name_the_refusal(tools, name) -> None:
    description = tools[name][0].description
    assert "not a Python toolset" in description and "``type=validation-error``" in description


# ---- ruling 5 (and ruling 1's words): what a parked call gets after an update ----------------------------------------------------------------------------


async def _park(fake_storage_provider) -> dict:
    """Park a call of ``ask`` through the provider built from the stored row; the resume metadata it carries."""
    with pytest.raises(YieldToWorker) as parked:
        await (await _provider(fake_storage_provider, "py-ask")).call(tool_name="ask", arguments={"question": "ship it?"}, ctx=CTX)
    return parked.value.yielded.resume_metadata


async def test_a_parked_call_survives_an_update_that_keeps_the_source(tools, fake_storage_provider) -> None:
    await _call(tools, "create_python_toolset", toolset_id="py-ask", source=ASK)
    meta = await _park(fake_storage_provider)
    assert meta["source_version"] == 1

    result, body = await _call(tools, "update_python_toolset_source", toolset_id="py-ask", source=ASK)

    assert not result.is_error and body["ok"] is True and body["source_version"] == 1
    assert (await fake_storage_provider.get_storage(Toolset).get("py-ask")).config.source_version == 1
    resumed = await (await _provider(fake_storage_provider, "py-ask")).resume_tool(tool_id="ask", payload={"response": "yes"}, resume_metadata=meta)
    assert not resumed.is_error and resumed.output == "yes"


async def test_a_parked_call_is_refused_after_an_update_that_changes_the_source(tools, fake_storage_provider) -> None:
    await _call(tools, "create_python_toolset", toolset_id="py-ask", source=ASK)
    meta = await _park(fake_storage_provider)

    _result, body = await _call(tools, "update_python_toolset_source", toolset_id="py-ask", source=ASK + "\n# edited\n")

    assert body["source_version"] == 2
    resumed = await (await _provider(fake_storage_provider, "py-ask")).resume_tool(tool_id="ask", payload={"response": "yes"}, resume_metadata=meta)
    assert resumed.is_error
    assert "parked on v1, now v2" in resumed.output and "the answer was not applied" in resumed.output


def test_the_update_description_says_what_a_parked_call_gets(tools) -> None:
    flat = " ".join(tools["update_python_toolset_source"][0].description.split())

    assert "resumes against the code that parked" not in flat
    assert "ends with an error that names both versions" in flat and "the answer is not applied" in flat
    assert "The same source keeps the version" in flat


def test_the_source_version_field_says_what_a_parked_call_gets() -> None:
    flat = " ".join(PythonConfig.model_fields["source_version"].description.split())

    assert "runs the version that parked" not in flat
    assert "refused" in flat and "kept when it does not" in flat
