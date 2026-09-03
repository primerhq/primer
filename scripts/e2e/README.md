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
