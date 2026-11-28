# scripts/e2e/

Local e2e-style stack lifecycle + fixture staging. Each script is
self-documenting (read its own top-of-file docstring/header for the
full detail) — this is just an index.

## Lifecycle

- **bringup.sh** — starts postgres (podman/docker compose), resets the
  `primer_e2e` database, renders `tests/.e2e/config.yaml`, and launches
  `uv run primer api` in the background. `PRIMER_DB_PORT` overrides the
  postgres port (default 5432) if you need to avoid colliding with
  another stack. Exit 0 = ready; exit 1 = dumps diagnostics and the
  caller should run teardown.sh, not proceed.
- **teardown.sh** — stops the server, `compose down -v` (drops the
  postgres volume too). Always exits 0 (best-effort).
- **ui-bringup.sh** — the docker/podman-compose variant for exercising
  the console from a container network namespace (e.g. to reach a
  standalone `run_mock_llm.py` bound to `0.0.0.0`).

## Fixture staging

- **seed_staged_fixtures.py** — seeds a live stack (after bringup.sh)
  with one instance of every fixture-gated entity the console can
  render: approval decisions in all four terminal states plus one left
  genuinely pending, an ask_user park with a `response_schema` enum
  (radio-button path), one trigger per kind, a channel, a harness, a
  versioned service, a Python toolset, a collection + document, a graph
  + bound run session, an API token, an SSO provider, and a disabled +
  non-admin user — plus the in-process multi-scenario mock LLM
  (`MockLLMServer`) it drives all of that through. Safe to run more
  than once against the same stack: platform entities (providers,
  profiles, agents, policies, toolsets, etc.) have fixed ids and a
  409-on-conflict is treated as success; instance-scoped things
  (sessions, the workspace instance, the token, harness, service, doc)
  create a fresh one every run, which is the right behaviour for them
  — see the module docstring for the full breakdown. Writes
  `tests/.e2e/seed_summary.json` (gitignored) with every entity's id,
  for a companion script (e.g. a Playwright capture pass) to read.

  ```
  PRIMER_E2E_BASE_URL=http://127.0.0.1:8765 \
      uv run python scripts/e2e/seed_staged_fixtures.py
  ```

  Two lessons worth knowing before you extend this script (both are
  also documented inline where they bite):
  1. Workspace tool ids are short — `workspace__write` / `workspace__read`,
     not `workspace__write_file` / `workspace__read_file`.
  2. A custom-seeded provider/profile does not satisfy
     `GET /v1/auth/status`'s `setup_complete` on its own — that flag
     checks for the RESERVED default workspace / operator+builder
     agents / system collection specifically. Call `POST /v1/setup/seed`
     once a provider + profile exist, or the console renders the
     first-boot wizard for every route instead of the real app.

- **run_mock_llm.py** — the same mock-LLM app as a standalone process
  (rather than in-process), for manual/live diagnostics or reaching it
  from a container network namespace. See its own docstring.

## Real-account Codex workspace journeys

Run these two cases serially on an authorized, dedicated E2E deployment. The
API and worker must already have the same provider-ID binding to a provisioned
Responses broker with a dedicated signed-in account. Choose an existing single
model profile whose model that account is allowed to use. An existing Primer
administrator must be able to discover the saved provider, create an agent, and
create a fresh workspace from the built-in `local-default` template. The worker
must share that workspace's storage with the API.

Set `PRIMER_E2E_BASE_URL` to that deployment's API origin, and set
`PRIMER_E2E_CODEX_PROVIDER_ID` and `PRIMER_E2E_CODEX_PROFILE_ID` to the two
saved IDs. Inject the administrator's existing **Primer API token** as the masked
secret `PRIMER_E2E_CODEX_OPERATOR_TOKEN`. Keep the token out of URLs and command
lines. The broker's model credentials stay on its host.

Use an HTTPS origin with a trusted certificate. Plain HTTP is accepted only for
a literal loopback IP such as `http://127.0.0.1:8765`. The client connects directly,
with redirects disabled. Before discovery, it checks that Primer has an existing
user and that the supplied bearer authenticates as an administrator. A fresh
server or one running with authentication disabled fails this check.

From a checkout with the test dependencies installed:

```sh
PRIMER_RUN_E2E=1 PRIMER_E2E_REAL_CODEX_BROKER=1 \
    uv run --no-sync python -m pytest -n0 tests/e2e/test_codex_broker_real_workspace.py
```

One session copies a fresh random receipt. The other reads the repository's
`AGENTS.md` and `scripts/e2e/teardown.sh` as text and writes a fact-checked
operator note. Each case checks the saved file, correlated tool results, final
answer and committed turn. Running them consumes account quota and updates the
provider's discovery timestamp. The test calls the deployment through REST.
The operator supplies the real upstream account. Installed Codex CLI acceptance
requires its own qualified transport and run.
