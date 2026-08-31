# Python-runner toolset

## 1. Purpose

Registers a python module as a toolset. Source lives in the toolset record;
tools are derived from it by AST; calls execute out of process.

## 2. Conceptual model

The source is untrusted by construction: agents can reach
`create_python_toolset`, so an agent can author a tool. Everything here
follows from that. The host reads the module structurally and never
imports it; the sandbox runs it in a fresh process it cannot escape; and
the two sides talk over one JSON round trip whose shape the host
validates.

Registration and execution are therefore completely separate concerns. A
module that raises at import time still registers cleanly, because
registration never ran it.

## 3. Architecture patterns implemented

- **Structural inspection over evaluation.** Annotations are mapped over
  the AST rather than evaluated, so building a tool descriptor cannot run
  operator code.
- **The host owns every security-relevant identifier.** The tool names a
  yield kind; the host builds the `event_key` from the real
  `ToolContext`.
- **Scoped tool names.** Tools park under `{toolset_id}__{tool_id}`
  because the resume registry is keyed process-wide and python tool names
  are operator-chosen.
- **One shared resume hook, not a closure per tool.**
  `register_resume_hook` is idempotent on the (name, hook) pair, so a
  module-level function lets a provider rebuild re-register harmlessly.

## 4. Code layout

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

## 5. Data model

The durable state is the toolset record: its source, its
`source_version`, and the `Tool` descriptors derived from them. Nothing
the sandbox produces is persisted except through the normal tool-result
path. `RegisteredTool.lineno` is carried alongside each descriptor so the
console outline can jump to a function.

## 6. Lifecycle

### Registration never executes the module

The source is untrusted: agents can reach `create_python_toolset`, so an agent
can author a tool. The host therefore inspects it structurally and never
imports it. Annotations are mapped over the AST rather than evaluated, for the
same reason. A module that raises at import time still registers cleanly, and
there is a test asserting that.

### Wire protocol

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

### Yielding and resume

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

## 7. Persistence

### source_version

Stamped into `resume_metadata` at park time and checked on resume. If the
source moved, the resume refuses: running current code against an answer to
the old question is worse than refusing, because the operator answered a
prompt the new code may no longer ask. The server owns the bump, so two
concurrent editors cannot land on the same number.

## 8. Public surfaces

### The console builder

`ui/components/toolsets/python-editor.jsx` is the page; the editing surface
itself is `python-code-editor.jsx`, wrapping CodeMirror 6 from
`ui/vendor/codemirror.min.js`.

The vendored bundle is built from pinned npm packages to a single IIFE
exposing one global, `window.CM6`, holding only the names the editor uses.
`ui/vendor/MANIFEST.md` carries the package inventory, both sha256s, and the
rebuild recipe; `codemirror.entry.js` is vendored beside it as the bundle's
source and is deliberately not loaded by the browser. This is the only
vendored file with transitive packages baked in, which is why the manifest
spells out what "no transitive deps" means for it: resolved once, pinned by
version, frozen into a file whose hash is recorded.

If the bundle fails to load the editor renders a plain textarea instead. Both
carry `data-testid="python-source"`, and the real one is distinguished by
`data-editor="codemirror"` -- the e2e suite asserts on that, because every
static test passes either way.

### Completions

Deliberately not general Python. The completion list covers the six names
primer injects (`primer_tool`, `resumes`, `ask_user`, `sleep_for`,
`watch_files`, `ctx`) and the docstring sections, because those appear in no
Python documentation anywhere. Each carries an `info` string: the list is the
only place that surface is discoverable.

### Live validation

`POST /v1/toolsets/{id}/validate` runs `register_module` against candidate
source and returns the tools it would produce, or the error with its field
and line. It never persists.

It always answers 200. Source that does not register is the normal state of a
half-written function, so treating it as an HTTP error would make the
editor's happy path an error path. The PUT route still 422s, because there an
invalid source is a rejected write.

The console debounces at 450ms and feeds the result to two places: CodeMirror
diagnostics (the failing line is marked) and the function outline (which lists
what the DRAFT would register, beside a separate panel showing what is saved
and callable). Those two panels differ exactly while there are unsaved edits,
which is the gap an operator needs to see before saving.

`RegisteredTool.lineno` exists for this: the outline can list a function only
if it can also jump to it.

## 9. Internal contracts

- **Output that is not exactly one response object is an error, never a
  value.** A runner that died half way must not be able to look like a
  tool that succeeded.
- **A tool may name a yield KIND, never an event key.** A function that
  could supply its own key could name `ask_user:{another_session}:{their_tcid}`
  and resume a park it does not own.
- **A moved `source_version` refuses the resume.** Running current code
  against an answer to the old question is worse than refusing, because
  the operator answered a prompt the new code may no longer ask.
- **Validation always answers 200; the PUT still 422s.** Source that does
  not register is the normal state of a half-written function, so treating
  it as an HTTP error would make the editor's happy path an error path. A
  save is a different question, and an invalid source is a rejected write.

## 10. Testing patterns

The pure halves (`docstring.py`, `schema.py`) are unit-tested directly.
Registration is tested against modules that would fail if they were ever
imported, which is what pins the never-execute contract. The runner is
exercised end to end through `tests/toolset/`, and the console builder's
two rendering paths both carry `data-testid="python-source"`, with the
CodeMirror one distinguished by `data-editor="codemirror"`, because every
static test passes either way and only the e2e suite can tell them apart.

## 11. Historical decisions

- **Completions are deliberately not general Python.** Why: the list
  covers the six names primer injects and the docstring sections, because
  those appear in no Python documentation anywhere. General completion is
  the editor's job, not ours.
- **The CodeMirror bundle is vendored as a single pinned IIFE.** Why: it
  is the only vendored file with transitive packages baked in, so
  `ui/vendor/MANIFEST.md` spells out what "no transitive deps" means for
  it: resolved once, pinned by version, frozen into a file whose hash is
  recorded.
- **The editor degrades to a textarea.** Why: a failed bundle load should
  cost syntax highlighting, not the ability to edit a tool.
