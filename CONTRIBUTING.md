# Contributing to Primer

Thanks for your interest in contributing. This guide covers the essentials;
the full contributor handbook (project layout, the coordinator/worktree
workflow, and the Definition of Done) lives in [AGENTS.md](AGENTS.md).

> AI agents working on this repo must follow the full agent contract in
> [AGENTS.md](AGENTS.md) (the coordinator/worktree working model, the
> Definition of Done, and the hard rules). The rules below are the human-facing
> summary and do not override it.

## Contribution workflow

Primer uses an **issue-first** workflow so that work is tracked, agreed on, and
not duplicated. Before you write any code:

1. **Search existing issues first.** Look through the
   [issue tracker](https://github.com/<org>/primer/issues), including closed
   issues, for an existing report or request that matches. If one already
   exists, comment there to say you would like to work on it rather than opening
   a duplicate.
2. **If nothing matches, open an issue first.** Use the
   [bug report or feature request templates](.github/ISSUE_TEMPLATE/) to
   describe the problem or proposal, and wait for a maintainer to confirm the
   direction before investing in a large change. For open-ended questions, use
   [Discussions](https://github.com/<org>/primer/discussions) instead of an
   issue.
3. **Open a pull request against that issue.** Branch off `main`, make the
   change, and reference the issue in the PR description with `Closes #<n>` (the
   PR template has a "Related issues" field for this). Every PR should trace
   back to an issue; for a genuinely trivial fix (a typo, an obvious one-liner)
   you may describe it inline in the PR instead of filing a separate issue.

Security issues are the one exception: do NOT open a public issue. Report them
privately as described in [SECURITY.md](SECURITY.md).

## Development setup

Primer targets Python 3.13 and uses [uv](https://github.com/astral-sh/uv) for
dependency and environment management. The stack is FastAPI + asyncio with
Postgres + pgvector, and a vanilla-React (JSX, no build step) operator console.

```bash
git clone https://github.com/<org>/primer
cd primer
uv sync
docker compose up -d postgres
uv run primer api   # starts the API plus an in-process worker; then GET /v1/health
```

See [config.example.yaml](config.example.yaml) for the configuration shape
(Postgres + providers). A newcomer should be able to go from clone to a running
`/v1/health` in under ten minutes.

### Pre-commit hooks

Install the git hooks once so lint, formatting, secret scanning, and the
doc-hygiene check run automatically on every commit:

```bash
uv run pre-commit install
```

The hooks (configured in [.pre-commit-config.yaml](.pre-commit-config.yaml))
run ruff (`--fix`) and ruff-format on the files you touch, fix trailing
whitespace / end-of-file, scan for secrets with gitleaks, and run the
`tests/docs/` hygiene suite (the em-dash ban). To run them across the whole
repo on demand:

```bash
uv run pre-commit run --all-files
```

### Common tasks (Makefile)

A [Makefile](Makefile) wraps the same commands CI runs, so local green means
CI green:

```bash
make setup          # uv sync
make lint           # ruff check .
make fmt            # ruff check --fix .
make test           # narrowed unit sweep
make cov            # unit sweep + coverage (enforces the 90% threshold)
make docs-hygiene   # the tests/docs hygiene suite
make serve          # uv run primer api
make docker-build   # build the primer image
```

The repository layout is described in [AGENTS.md](AGENTS.md) section 2:
`primer/<subsystem>/` is the backend, `ui/` the console, `primectl/` the CLI,
`tests/` the test suites, and `docs/dev/` the authoritative developer reference
(start at [docs/dev/README.md](docs/dev/README.md) before changing a subsystem).

## Running tests

```bash
# Narrowed unit sweep (must stay green at every commit; parallel, ~90s):
uv run pytest tests/ -q --ignore=tests/distributed --ignore=tests/ui_e2e \
  --ignore=tests/e2e --ignore=tests/integration --ignore=tests/llm

# Documentation hygiene (em-dash ban, frontmatter, ref/embed resolution, style):
PRIMER_USER_DOCS_STRICT=1 uv run pytest tests/user_docs tests/docs
```

Add `-n0` to run a single module serially while debugging.

**The end-to-end suite (`tests/e2e`) saturates all CPU cores and must run
EXCLUSIVELY.** It is not part of the default contributor loop. When you do run it:

- Never run two e2e runs at once. Before starting, check for an already-running
  one and kill it first:

  ```bash
  pgrep -af "pytest tests/e2e" | grep -v pgrep   # kill any match before starting
  ```

- Bring the environment up with `scripts/e2e/bringup.sh` (it reuses the shared
  dev Postgres on a separate `primer_e2e` database). Do NOT run
  `scripts/e2e/teardown.sh` with volume removal: it shares the Postgres
  container with the dogfood instance and will wipe it.
- Run targeted e2e serially with `-n0` and the e2e gate set, e.g.
  `PRIMER_RUN_E2E=1 PRIMER_E2E_PORT=8765 uv run pytest tests/e2e/<file> -n0`.
  The suite is skipped unless `PRIMER_RUN_E2E=1` is set.

Some tests are environment-gated: real-LLM tests need a reachable LM Studio
(`LMSTUDIO_API_KEY` set), and the Kubernetes workspace tests need a live
cluster. After a code-changing task, restart the dogfood `uv run primer api`
and confirm `/v1/health` returns 200.

## Pull requests

A change is not done until every applicable track is done (see the full
Definition of Done in [AGENTS.md](AGENTS.md) section 4). In summary:

- **Backend** under `primer/` follows the rest-api conventions (RFC7807 errors,
  observability hooks).
- **UI** in `ui/` is updated for any user-visible feature (a backend feature
  with no console surface is incomplete).
- **System tools** for new functionality are exposed in `primer/toolset/`
  (built with `make_tool`, callable over `POST /v1/mcp`).
- **Docs** are updated: operator docs in `primer/user_docs/`, agent-usage docs
  in `docs/agents/`, and dev docs in `docs/dev/` as the change warrants.
- **Tests**: add or extend unit tests and, for user-visible flows, e2e coverage.
- **Regressions**: keep the suites green; do not weaken a test to hide a real
  regression - fix the cause.
- **primectl**: keep the CLI in parity when you add an API endpoint.

If a track is genuinely not applicable, say so explicitly with a one-line
reason rather than skipping it silently.

PR mechanics:

- Branch off `main`; never force-push `main`. Keep each PR focused.
- Use [conventional commit](https://www.conventionalcommits.org/) messages
  (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`, `perf:`), and do
  NOT add a `Co-Authored-By` footer.
- Stage only the files your change touches (`git add <named files>`); never
  `git add -A`.
- Keep the narrowed sweep and the doc-hygiene checks green.

## Style

- Python is linted with [ruff](https://docs.astral.sh/ruff/) (config in the
  `[tool.ruff]` block of [pyproject.toml](pyproject.toml)); `make lint` and CI
  run `ruff check`. Auto-fix safe findings with `make fmt`.
- Never use the em-dash character (U+2014) in committed files. Use a regular
  hyphen, "to", or reword. The `tests/docs/` hygiene suite enforces this.
- Match the conventions of the surrounding code; prefer small, focused files.
- Every relative markdown link from `AGENTS.md` or `docs/dev/` must resolve.
- User-facing documentation lives under `primer/user_docs/`; contributor and
  architecture docs live under `docs/dev/`.

## Reporting bugs and requesting features

Follow the [issue-first contribution workflow](#contribution-workflow) above:
search the existing issues, then file a new one using the templates under
[.github/ISSUE_TEMPLATE/](.github/ISSUE_TEMPLATE/) (blank issues are disabled).
For security issues, do not open a public issue; see [SECURITY.md](SECURITY.md).

## Code of conduct

Participation in this project is governed by our
[Code of Conduct](CODE_OF_CONDUCT.md).
