---
slug: python-tools
title: Python tools - register a function as a tool
summary: How to write a python function that becomes a callable tool, including yielding tools, what the docstring must contain, and what the sandbox allows.
related: [yielding, tool-approval, agents]
mcp_tools:
  - system::create_python_toolset
  - system::update_python_toolset_source
  - system::list_python_tools
---

# Python tools - register a function as a tool

## Overview

A python toolset is one module. Every function in it decorated with
`@primer_tool` becomes a tool you can call like any other. Source lives in the
toolset record and is edited from the console, the REST API, or
`POST /v1/toolsets` with the python provider.

A new toolset starts empty. In the console, **Add function** inserts a
scaffold with the contract spelled out in `#` comments -- one for a plain
tool, one for a yielding tool and its `@resumes` companion. The editor
validates against the real registrar as you type, so a missing `Args:` entry
is marked on its line before you save rather than after.

## Mental model

### The shape

```python
@primer_tool()
def greet(name: str) -> str:
    """Greet a person by name.

    Use when you need a friendly greeting.

    Args:
        name: Who to greet.
    """
    return "hello " + name
```

Three things are enforced when you save, not when the tool is called:

1. **A docstring**, with a summary line, a `Use when ...` line, and an `Args:`
   entry for every argument. The description reaches the model in the same
   context as every built-in tool, so a vague one degrades every agent that
   sees it.
2. **A type annotation on every argument.** `str`, `int`, `float`, `bool`,
   `list[...]`, `dict`, and unions with `None` map to a schema. Anything else
   is rejected and names the parameter.
3. **A JSON-serialisable return value.** Returning an object gets you a tool
   error naming the type.

An `Examples:` section is optional and is validated against the schema, so an
example that disagrees with your signature fails the save.

### Arguments and context

Every parameter becomes a tool argument except one: a parameter named `ctx`
receives the call's context and is excluded from the schema.

```python
@primer_tool()
def whoami(ctx) -> str:
    """Report the session this call belongs to.

    Use when a tool needs to know its own session.
    """
    return ctx["session_id"]
```

`ctx` is **data only**: `tool_call_id`, `session_id`, `workspace_id`,
`parked_at`. It is not the live `ToolContext` object, because the
tool runs in a separate process. `ctx.inform` is not available.

### Yielding tools

A tool that waits for a human is a pair: the function that asks, and a
companion that handles the answer.

```python
@primer_tool(timeout_seconds=60)
async def ask_the_operator(question: str, ctx) -> str:
    """Ask the operator a question and wait for their reply.

    Use when a decision needs a human and cannot be inferred.

    Args:
        question: What to ask, in one sentence.
    """
    return ask_user(question)


@resumes(ask_the_operator)
def _answer(payload: dict, meta: dict) -> str:
    """Return the operator's answer.

    Use when resuming the ask.

    Args:
        payload: The response payload.
        meta: The resume metadata.
    """
    return payload["response"]
```

The `@resumes` companion is what makes a tool yielding. Helpers available:
`ask_user(question)`, `sleep_for(seconds)`, `watch_files(paths)`.

**Nothing survives in memory across the park.** The two halves are separate
process invocations, possibly minutes apart. Anything the companion needs must
travel in the metadata:

```python
    return ask_user(question, meta={"ticket": ticket_id})
```

### Timeouts

`@primer_tool(timeout_seconds=...)` per tool, defaulting to the toolset's
setting (30s) and capped at 300s. The process is killed at the limit, so an
infinite loop returns a timeout error rather than wedging a worker.

## Gotchas

### What the sandbox allows

Tools run in a separate process with resource limits. What else is enforced
depends on the deployment; `GET /v1/toolsets/{id}` lists the tools it exposes
reports the level, and the console shows it beside the editor.

- The standard library is available. Third-party packages are only available
  on container backends, from the toolset's image.
- Primer's own modules are never importable.
- Outbound network is denied unless the toolset sets `allow_network`.
- Spawning subprocesses is denied where the syscall filter is active.

See `docs/dev/architecture/python-tool-isolation.md` for exactly what each
level does and does not cover.

### Three that bite

- **Nothing survives across a park.** The two halves of a yielding tool are
  separate process invocations; put anything the companion needs in `meta`.
- **`ctx` is data, not the live object.** No `inform`, no graph services.
- **The return value must be JSON-serialisable.** Returning an object is a
  tool error naming the type.

## Related

- `docs/dev/architecture/python-tool-isolation.md` - what each isolation level
  does and does not cover.
- `docs/dev/subsystems/python-runner-toolset.md` - how registration and the
  runner work.
