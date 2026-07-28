# Python-runner toolset

Registers a python module as a toolset. Source lives in the toolset record;
tools are derived from it by AST; calls execute out of process.

## Modules

| File | Responsibility |
| --- | --- |
| `docstring.py` | Google docstring to the purpose/when/args anatomy `make_tool` enforces. Pure. |
| `schema.py` | Function signature to a self-contained JSON Schema. Pure, AST-based. |
| `registration.py` | Collects `@primer_tool` / `@resumes` and builds `Tool` descriptors. |
| `protocol.py` | The host side of the shim wire format. |
| `_shim.py` | The in-sandbox runner, shipped as a source string. |
| `runners.py` | `LocalHardenedRunner`, `SandboxRunner`, isolation detection. |
| `yielding.py` | Yield request to `Yielded`, with host-built event keys. |
| `provider.py` | `PythonToolsetProvider` plus the shared resume hook. |

## Registration never executes the module

The source is untrusted: agents can reach `create_python_toolset`, so an agent
can author a tool. The host therefore inspects it structurally and never
imports it. Annotations are mapped over the AST rather than evaluated, for the
same reason. A module that raises at import time still registers cleanly, and
there is a test asserting that.

## Wire protocol

One JSON round trip over stdin/stdout:

```
host   -> {module, fn, phase: "call"|"resume", args, ctx, payload, meta,
           limits, allow_network}
sandbox -> {ok: true, value}
         | {ok: true, yield: {kind, params, meta}}
         | {ok: false, error: {type, message, traceback}}
```

Output that is not exactly one response object is an error, never a value: a
runner that died half way must not be able to look like a tool that succeeded.

## Yielding and resume

The tool names a *kind*; the host builds the `event_key` from the real
`ToolContext`. A function that could supply its own key could name
`ask_user:{another_session}:{their_tcid}` and resume a park it does not own.

Tools park under a **scoped** name, `{toolset_id}__{tool_id}`. The resume
registry is keyed by tool name process-wide and python tool names are
operator-chosen, so two toolsets both defining `ask` would otherwise collide.

One module-level `python_tool_resume` serves every python tool rather than a
closure per tool: `register_resume_hook` is idempotent on the (name, hook)
pair, so a shared function lets a provider rebuild re-register harmlessly
where a closure would trip the overwrite guard every time.

Resume hooks receive a `ResumeContext` (see
`primer/worker/yield_resume_registry.py`) carrying the tool name, call id,
session and an async `resolve_provider`. That is how the shared hook finds
which toolset's source to run.

## source_version

Stamped into `resume_metadata` at park time and checked on resume. If the
source moved, the resume refuses: running current code against an answer to
the old question is worse than refusing, because the operator answered a
prompt the new code may no longer ask. The server owns the bump, so two
concurrent editors cannot land on the same number.
