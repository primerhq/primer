# Modularity

## 1. Purpose

Primer ships as one wheel, `primer-ai`, and one image. It is not a
microservice split and not a plugin host. What it is instead is a modular
monolith: a lean always-imported core, a set of optional subsystems that
live behind packaging extras, and a discovery surface that lets any caller
ask which of those subsystems this particular deployment actually has.

The problem this solves is that "optional" used to be a claim rather than a
property. A dependency was optional because someone said so in a table,
while the code imported it at module scope; an operator learned a backend
was missing by configuring it and reading the traceback. This subsystem
turns the claim into two enforced rules, one guard helper, one capability
map, and a CI lane that fails when any of them stops being true.

## 2. Conceptual model

Three ideas carry the whole design.

**An extra is a capability.** Each entry in `[project.optional-dependencies]`
corresponds to a capability the deployment either has or does not.
`primer/common/optional.py` holds the one map from extra name to the
modules whose importability proves it, and every other part of the system
derives from that map rather than restating it.

**Absence is a first-class state, not an error.** A missing extra produces a
`ConfigError` naming the extra and the pip command that fixes it, raised at
the dispatch seam before the backend import. Bootstrap skips the default
rows it cannot construct. The console labels the option "(not installed)"
and explains underneath. Nothing crashes, and nothing pretends.

**Enablement is install plus restart.** Extras are resolved at import time,
so there is no runtime toggle and no partial hot-enable. `pip install
'primer-ai[lance]'` followed by a restart is the whole procedure, and every
hint in the product says exactly that.

The extra to marker-module map, mirroring `EXTRA_MODULES` in
`primer/common/optional.py`, which is the source of truth:

| Extra | Enables | Marker modules |
|---|---|---|
| `huggingface` | local embedder, local cross-encoder, exact Ollama token counts | `sentence_transformers` |
| `lance` | the embedded LanceDB vector store | `lancedb` |
| `kubernetes` | the Kubernetes workspace backend | `kubernetes_asyncio` |
| `docker` | the container workspace backend | `aiodocker` |
| `channels` | Slack, Telegram and Discord adapters | `slack_bolt`, `telegram`, `discord` |

`channels` is the one extra with several markers. It counts as installed
only when all three import, because a partial install is not the channels
feature; `channel_platforms()` exposes the per-platform detail that the
aggregate hides, which is what the capabilities endpoint reports so an
operator running Slack alone sees that rather than a bare "no".

## 3. Architecture patterns implemented

- **Single source of truth for capability wiring.** `EXTRA_MODULES` is
  defined once. A test asserts set equality against `pyproject.toml`, so
  adding or renaming an extra without updating the map fails immediately
  rather than drifting into a silent mismatch.
- **Guard at the dispatch seam, before the import.** `require_extra(extra,
  capability)` raises the standard `ConfigError`. It is called ahead of the
  backend import, so the operator sees the extra that fixes the problem
  rather than a `ModuleNotFoundError` naming a third-party module.
- **Lazy import for optional dependencies.** Anything behind an extra is
  imported inside the function that needs it. Module-scope imports of
  optional dependencies are what the core-install lane exists to catch.
- **PEP 562 module `__getattr__` for optional re-exports.** Packages that
  re-export an optional class keep raising `ModuleNotFoundError` from
  `__getattr__` rather than converting to `ConfigError`, because that is
  the import protocol and callers catch `ImportError` around attribute
  access. Only the hint text is centralised.
- **Degrade rather than fail where the feature is not the point.** Ollama
  token counting falls back to a character heuristic without
  `transformers`, because counting sits on the hot path of every turn and
  an exception there would be worse than an approximation.

## 4. Code layout

| Path | What |
|---|---|
| `primer/common/optional.py` | `EXTRA_MODULES`, `CHANNEL_PLATFORM_MODULES`, `has_extra`, `channel_platforms`, `install_hint`, `require_extra`, and the `_find_spec` test seam |
| `primer/api/routers/capabilities.py` | `GET /v1/capabilities` |
| `ui/foundation/capabilities.js` | `useCapabilities`, `capabilityHint`, `extraInstalled`, `CapabilityGate`, `EXTRA_FOR_PROVIDER_TYPE` |
| `scripts/measure_install.sh` | installed-size measurement per target |
| `pyproject.toml` | the extras themselves plus the `[tool.importlinter]` contracts |
| `.github/workflows/ci.yml` | the `core-install` lane and the `lint-imports` step |

## 5. Data model

There is no persisted entity. Capability state is computed per process from
importability and never written down, which is deliberate: a recorded
capability set would go stale the moment someone installed an extra, and
the failure would be silent. `GET /v1/capabilities` returns:

```json
{
  "version": "0.6.0",
  "extras": {
    "channels": {
      "installed": false,
      "platforms": {"slack": true, "telegram": false, "discord": false}
    },
    "docker": {"installed": true, "platforms": null}
  }
}
```

`platforms` is non-null only for `channels`.

## 6. Lifecycle

An extra is resolved at first ask, not at startup. `has_extra` consults
`importlib.util.find_spec` through the module-level `_find_spec` alias, so
a test can simulate absence without touching `sys.modules` or the import
system.

Three moments matter in a process's life. At bootstrap, `BootstrapRunner`
asks `has_extra` before registering each dep-backed default row and records
a skip rather than creating a provider that cannot be constructed. At
dispatch, a factory calls `require_extra` before importing its backend. At
render, the console fetches `/v1/capabilities` once and annotates the
options it offers. All three read the same map, so they cannot disagree.

## 7. Persistence

None of its own. The bootstrap skips it causes are visible in
`BootstrapResult.skipped`, and the rows that a fatter deployment created
remain in the database when a leaner one starts against it. That case is
handled rather than prevented: the channels page shows a banner and stays
usable, because viewing and deleting those rows is still useful where the
SDKs to run them are absent.

## 8. Public surfaces

- `GET /v1/capabilities` returns the version and the per-extra status.
- The workspace shell annotates unavailable provider types "(not installed)" and
  explains underneath, across the LLM/embedding/cross-encoder provider
  forms, the workspace provider form, semantic search, and knowledge
  ingestion; channels gets a banner instead of a gate.

## 9. Internal contracts

Two boundary rules, both enforced rather than documented.

**Rule 1: no optional third-party dependency in the always-imported path.**
Enforced by the `core-install` job in `.github/workflows/ci.yml`, which
syncs with no extras, imports `primer.api.app` and `primer.cli`, and runs
the guard suite. A module-scope import of an optional dependency fails
there first.

**Rule 2: core never imports optional subsystems.** Enforced by
import-linter contracts in `pyproject.toml` under `[tool.importlinter]`,
run by the `lint-imports` CI step. The first contract forbids
`primer.model`, `primer.int`, `primer.storage`, `primer.common` and
`primer.claim` from importing the optional subsystems; the second forbids
`primer.agent` and `primer.worker` from importing `primer.channel`. Both
police direct imports only, since indirect chains through the app layer are
expected.

A genuinely necessary lazy seam is sanctioned by adding it to that
contract's `ignore_imports` with a comment saying why. Widening
`source_modules` or `forbidden_modules` to make the check pass defeats the
contract; the ignore list is meant to read as an audit trail of deliberate
exceptions.

**Adding a new extra** touches, in order: the extra in `pyproject.toml`; an
`EXTRA_MODULES` entry, without which the lockstep test fails; a
`require_extra` guard at the dispatch seam; a `has_extra` skip in bootstrap
if it seeds default rows; a capability hint in the console; a row in the
install matrix in `README.md`; and a decision about whether the release
image carries it, which is the difference between the default tag and
`-full`.

## 10. Testing patterns

Absence is simulated at the seam rather than by uninstalling. Tests
monkeypatch `primer.common.optional._find_spec` to return `None`, which
exercises every guard identically on a full or a core-only install.
Patching `importlib.util.find_spec` globally does not work, because the
helper binds the function at import time.

Two traps are worth knowing. A test asserting only that Ollama counting
equals the character heuristic passes for the wrong reason, since a failed
Hub load reaches the same fallback; assert on the log line, which
distinguishes the absent-dependency path from a load failure. And tests
that patch an optional library's attribute cannot run without that library,
because `patch` imports the module to replace the attribute on it: gate
them on the extra being present, and leave the fallback tests ungated.

## 11. Historical decisions

- **Multi-wheel split, rejected (2026-08-09 spec).** Splitting `primer-ai`
  into separately versioned distributions would move the coupling from
  imports into version constraints without making the boundaries any more
  real, and would multiply the release surface for a project that ships one
  server. The wheel stays single; the boundaries are enforced by contracts
  instead.
- **Runtime extra installation from the UI, rejected.** Installing into the
  running interpreter cannot reliably make a new dependency importable in
  an already-warm process, so the honest contract is install plus restart,
  which is what every hint says.
- **`transformers` demoted to `huggingface` (2026-08-09).** It was a core
  dependency imported at module scope by the Ollama tokenizer, so every
  install carried it whether or not it counted a single Ollama token. Exact
  counts now need `primer-ai[huggingface]`; core installs degrade to the
  character heuristic.
- **Core dependency audit (2026-08-09).** A core install measures 345M of
  site-packages, via `scripts/measure_install.sh`. The rest of the heavy
  core dependencies were examined and kept, each because it serves an
  always-on path: `mini-racer` transpiles the console JSX bundle at
  startup, `grpcio` backs the OTLP gRPC span exporter, `trafilatura`
  performs extraction for the core web-fetch toolset, and `tiktoken` counts
  tokens for the OpenAI family.
- **The slim image became the default tag (2026-08-09).** Unsuffixed
  `:{version}` and `:latest` now resolve to the slim build; batteries
  included moved to `-full`. The `-slim` and `-fat` aliases are kept one
  release cycle so existing pulls keep working.
