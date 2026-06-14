# Contributing to Primer

Thanks for your interest in contributing. This guide covers the essentials;
the full contributor handbook (project layout, the coordinator/worktree
workflow, and the Definition of Done) lives in [AGENTS.md](AGENTS.md).

## Development setup

Primer targets Python 3.13 and uses [uv](https://github.com/astral-sh/uv) for
dependency and environment management.

```bash
git clone https://github.com/<org>/primer
cd primer
uv sync
uv run primer api --config config.example.yaml   # then GET /v1/health
```

See [config.example.yaml](config.example.yaml) for the configuration shape
(Postgres + providers). A newcomer should be able to go from clone to a running
`/v1/health` in under ten minutes.

## Running tests

```bash
# Narrowed unit sweep (the fast signal; what CI runs):
uv run pytest tests/ -q \
  --ignore=tests/e2e --ignore=tests/ui_e2e --ignore=tests/distributed \
  --ignore=tests/integration --ignore=tests/llm

# Documentation hygiene (frontmatter, ref/embed resolution, style):
PRIMER_USER_DOCS_STRICT=1 uv run pytest tests/user_docs tests/docs
```

The end-to-end suite (`tests/e2e`) runs against a live server and Postgres and
is CPU-exclusive; it is not part of the default contributor loop.

## Pull requests

- Branch off `main`; keep each PR focused.
- Use [conventional commit](https://www.conventionalcommits.org/) messages
  (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`, `perf:`).
- Add or update tests for the behavior you change, and update the relevant docs.
- Keep the narrowed sweep and the doc-hygiene checks green.

## Style

- Do not use the em-dash character. Use a hyphen, a double hyphen, or reword.
- Match the conventions of the surrounding code; prefer small, focused files.
- User-facing documentation lives under `primer/user_docs/`; contributor and
  architecture docs live under `docs/dev/`.

## Reporting bugs and requesting features

Open an issue using the templates under
[.github/ISSUE_TEMPLATE/](.github/ISSUE_TEMPLATE/). For security issues, do not
open a public issue; see [SECURITY.md](SECURITY.md).

## Code of conduct

Participation in this project is governed by our
[Code of Conduct](CODE_OF_CONDUCT.md).
