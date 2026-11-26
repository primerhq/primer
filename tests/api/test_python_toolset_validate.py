"""POST /v1/toolsets/{id}/validate -- the registrar's verdict without a write.

The editor needs to tell the operator that a docstring is malformed while
they are still typing. Before this route the only way to find out was to
save, so every mistake round-tripped through a write to the stored toolset.

Two properties matter and are pinned here:

* It never persists. A dry run that mutated the toolset would be worse than
  no dry run at all.
* Invalid source is 200 with ``ok: false``, not a 4xx. Source that does not
  register yet is the *normal* state of a half-written function; treating it
  as an HTTP error would make the editor's happy path an error path. (The
  PUT route still 422s, because there an invalid source is a rejected write.)
"""

from __future__ import annotations

import pytest

GOOD = (
    "@primer_tool()\n"
    "def greet(name: str) -> str:\n"
    '    """Greet a person by name.\n\n'
    "    Use when you need a friendly greeting.\n\n"
    "    Args:\n        name: Who to greet.\n"
    '    """\n'
    "    return 'hello ' + name\n"
)

# Second function is undocumented -- registration names the parameter.
UNDOCUMENTED_ARG = (
    "@primer_tool()\n"
    "def add(a: int, b: int) -> int:\n"
    '    """Add two numbers.\n\n'
    "    Use when you must add.\n\n"
    "    Args:\n        a: The first.\n"
    '    """\n'
    "    return a + b\n"
)

# One good tool + two independently-broken tools -- pins that the dry run
# reports every function's verdict instead of aborting after the first bad
# one.
TWO_BAD_ONE_GOOD = GOOD + (
    "\n\n@primer_tool()\n"
    "def add(a: int, b: int) -> int:\n"
    '    """Add two numbers.\n\n'
    "    Use when you must add.\n\n"
    "    Args:\n        a: The first.\n"
    '    """\n'
    "    return a + b\n"
    "\n\n@primer_tool()\n"
    "def subtract(a: int, b: int) -> int:\n"
    '    """Subtract two numbers.\n\n'
    "    Use when you must subtract.\n"
    '    """\n'
    "    return a - b\n"
)

YIELDING = GOOD + (
    "\n\n@primer_tool()\n"
    "async def ask(question: str, ctx) -> str:\n"
    '    """Ask the operator.\n\n'
    "    Use when a human must decide.\n\n"
    "    Args:\n        question: What to ask.\n"
    '    """\n'
    "    return ask_user(question)\n"
    "\n\n@resumes(ask)\n"
    "def _ask_resume(payload: dict, meta: dict) -> str:\n"
    '    """Return the answer.\n\n'
    "    Use when resuming.\n\n"
    "    Args:\n        payload: The payload.\n        meta: The metadata.\n"
    '    """\n'
    "    return payload['response']\n"
)


async def _seed(client, tid: str) -> None:
    r = await client.post(
        "/v1/toolsets",
        json={"id": tid, "provider": "python",
              "config": {"source": GOOD, "source_version": 1}},
    )
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_valid_source_reports_its_tools(client) -> None:
    await _seed(client, "val-ok")
    r = await client.post(
        "/v1/toolsets/val-ok/validate", json={"source": GOOD},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert [t["id"] for t in body["tools"]] == ["greet"]
    assert body["tools"][0]["args"] == ["name"]


@pytest.mark.asyncio
async def test_a_broken_docstring_is_200_not_4xx(client) -> None:
    await _seed(client, "val-broken")
    r = await client.post(
        "/v1/toolsets/val-broken/validate", json={"source": UNDOCUMENTED_ARG},
    )
    # Half-written source is the editor's normal state, not a protocol error.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    # A per-function failure is NOT a module-level error: it shows up as
    # ok=false on its own entry in ``tools``, not the top-level ``error``.
    assert body["error"] is None
    assert len(body["tools"]) == 1
    entry = body["tools"][0]
    assert entry["id"] == "add"
    assert entry["ok"] is False
    assert entry["error"]["field"] == "b"
    assert entry["error"]["lineno"]


@pytest.mark.asyncio
async def test_two_bad_tools_both_report_instead_of_first_wins(client) -> None:
    """Regression: validation used to stop at the first bad tool and lose
    every other verdict (including the good ones). A module with 1 good
    + 2 independently-broken tools must report all 3."""
    await _seed(client, "val-two-bad")
    r = await client.post(
        "/v1/toolsets/val-two-bad/validate", json={"source": TWO_BAD_ONE_GOOD},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error"] is None
    by_id = {t["id"]: t for t in body["tools"]}
    assert set(by_id) == {"greet", "add", "subtract"}

    assert by_id["greet"]["ok"] is True
    assert by_id["greet"]["args"] == ["name"]

    assert by_id["add"]["ok"] is False
    assert by_id["add"]["error"]["field"] == "b"

    assert by_id["subtract"]["ok"] is False
    assert by_id["subtract"]["error"]["message"]


@pytest.mark.asyncio
async def test_the_error_carries_a_line_the_editor_can_mark(client) -> None:
    await _seed(client, "val-line")
    r = await client.post(
        "/v1/toolsets/val-line/validate", json={"source": "def (\n"},
    )
    body = r.json()
    assert body["ok"] is False
    assert isinstance(body["error"]["lineno"], int)
    assert body["error"]["message"]


@pytest.mark.asyncio
async def test_validate_never_persists(client) -> None:
    """The whole point of a dry run."""
    await _seed(client, "val-nowrite")
    other = "@primer_tool()\ndef other(a: str) -> str:\n" \
            '    """Other.\n\n    Use when other.\n\n    Args:\n        a: A.\n    """\n'

    r = await client.post(
        "/v1/toolsets/val-nowrite/validate", json={"source": other},
    )
    assert r.json()["ok"] is True
    assert [t["id"] for t in r.json()["tools"]] == ["other"]

    # The stored toolset still holds the seeded source.
    got = await client.get("/v1/toolsets/val-nowrite")
    assert got.status_code == 200
    assert "greet" in got.json()["config"]["source"]
    assert "other" not in got.json()["config"]["source"]


@pytest.mark.asyncio
async def test_a_yielding_tool_is_reported_as_yielding(client) -> None:
    # The outline marks these differently: a yielding tool parks a run, which
    # is the single most important thing to know about it at a glance.
    await _seed(client, "val-yield")
    r = await client.post(
        "/v1/toolsets/val-yield/validate", json={"source": YIELDING},
    )
    body = r.json()
    assert body["ok"] is True
    by_id = {t["id"]: t for t in body["tools"]}
    assert by_id["ask"]["yields"] is True
    assert by_id["greet"]["yields"] is False
    # The @resumes companion is not itself a tool.
    assert "_ask_resume" not in by_id


@pytest.mark.asyncio
async def test_every_tool_carries_the_line_of_its_def(client) -> None:
    # Without this the outline can list a function but not jump to it.
    await _seed(client, "val-lineno")
    r = await client.post(
        "/v1/toolsets/val-lineno/validate", json={"source": YIELDING},
    )
    tools = r.json()["tools"]
    assert all(t["lineno"] > 0 for t in tools), tools
    by_id = {t["id"]: t["lineno"] for t in tools}
    # `ask` is defined after `greet`, so its line must be greater.
    assert by_id["ask"] > by_id["greet"]


@pytest.mark.asyncio
async def test_empty_source_is_valid_with_no_tools(client) -> None:
    # A toolset now starts empty, so this is the very first thing the editor
    # validates. It must not read as an error.
    await _seed(client, "val-empty")
    r = await client.post(
        "/v1/toolsets/val-empty/validate", json={"source": ""},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["tools"] == []


@pytest.mark.asyncio
async def test_over_cap_source_is_413_before_parsing(client, monkeypatch) -> None:
    """Review round 1 (4.2): a source one byte over the registrar's cap
    (MAX_PYTHON_TOOLSET_SOURCE_BYTES, 256 KiB of encoded bytes) is refused
    with a 4xx problem response and ast.parse never runs. The cap is a
    memory bound, not a parse timeout: ast.parse of a 4 MB source peaked
    near 2 GB RSS, so the app's 32 MiB request-body cap was no bound at
    all."""
    import ast

    import primer.toolset.python_runner.registration as reg

    parsed: list[str] = []
    real_parse = ast.parse

    # Record and delegate: pytest's own traceback rendering also calls
    # ast.parse, so a raising spy would mask a real test failure with an
    # INTERNALERROR. A recording spy keeps the assertion honest.
    def _spy_parse(source, *args, **kwargs):
        parsed.append(source)
        return real_parse(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", _spy_parse)
    over_cap = "x" * (reg.MAX_PYTHON_TOOLSET_SOURCE_BYTES + 1)

    r = await client.post(
        "/v1/toolsets/val-over-cap/validate",
        json={"source": over_cap},
    )
    assert r.status_code == 413, r.text
    body = r.json()
    assert body["type"] == "/errors/payload-too-large"
    assert body["status"] == 413
    assert body["extensions"]["code"] == "payload_too_large"
    assert body["extensions"]["limit_bytes"] == reg.MAX_PYTHON_TOOLSET_SOURCE_BYTES
    assert str(reg.MAX_PYTHON_TOOLSET_SOURCE_BYTES) in body["detail"], body
    assert over_cap not in parsed, "the registrar must not parse a source over the cap"


@pytest.mark.asyncio
async def test_a_source_at_exactly_the_cap_still_validates(client) -> None:
    """The cap is inclusive: exactly MAX_PYTHON_TOOLSET_SOURCE_BYTES
    encoded bytes still validates, so a toolset at the limit is not
    refused (the over-cap case above is the one byte past it)."""
    import primer.toolset.python_runner.registration as reg

    pad = reg.MAX_PYTHON_TOOLSET_SOURCE_BYTES - len(GOOD.encode("utf-8"))
    source = GOOD + " " * pad
    assert len(source.encode("utf-8")) == reg.MAX_PYTHON_TOOLSET_SOURCE_BYTES

    r = await client.post(
        "/v1/toolsets/val-at-cap/validate", json={"source": source},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert [t["id"] for t in r.json()["tools"]] == ["greet"]


@pytest.mark.asyncio
async def test_the_parse_runs_off_the_event_loop(client, app, monkeypatch) -> None:
    """Review 4.2: the registrar's ast.parse must not hold the event loop;
    a parse of a large source answers from a worker thread while the loop
    stays free."""
    import threading

    import primer.toolset.python_runner.registration as reg

    threads = []
    real = reg.register_module_report

    def spy(source, toolset_id, default_timeout):
        threads.append(threading.current_thread())
        return real(source, toolset_id, default_timeout)

    monkeypatch.setattr(reg, "register_module_report", spy)
    loop_thread = threading.current_thread()

    r = await client.post(
        "/v1/toolsets/val-offloop/validate", json={"source": GOOD},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert len(threads) == 1, "the registrar must run exactly once"
    assert threads[0] is not loop_thread, (
        "the parse ran on the event loop's own thread; a large source would "
        "stall every request on this process"
    )


@pytest.mark.asyncio
async def test_validate_does_not_execute_the_module(client) -> None:
    """Source is untrusted; the registrar reads the AST and never runs it."""
    await _seed(client, "val-noexec")
    hostile = (
        "raise RuntimeError('module executed')\n"
        "@primer_tool()\n"
        "def f(a: str) -> str:\n"
        '    """Do it.\n\n    Use when you must.\n\n    Args:\n        a: A.\n    """\n'
    )
    r = await client.post(
        "/v1/toolsets/val-noexec/validate", json={"source": hostile},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert [t["id"] for t in r.json()["tools"]] == ["f"]
