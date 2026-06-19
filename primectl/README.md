# primectl

A kubectl-style CLI for the Primer API. It learns the API surface at runtime
from the server's OpenAPI document, so generic verbs work against any resource
the server exposes.

## Install / run (uv workspace)

```bash
uv run --package primectl primectl --help
```

## Quick start

```bash
# point at a server (tokenless works when the server has auth disabled)
primectl config set-context dogfood --server http://localhost:9000
primectl config use-context dogfood

primectl api-resources                 # what resources exist
primectl get agents                    # list
primectl get agent <id> -o yaml        # one object as a manifest
primectl get agents --filter model=gpt # server-side find
primectl explain agent                 # schema fields

# declarative
primectl get agent <id> -o yaml > a.yaml   # edit it
primectl apply -f a.yaml                    # upsert (PUT if present else POST)

# collection documents, addressed by path
primectl doc put <col> notes/readme.md --content "hi" --title Readme
primectl doc get <col> notes/readme.md --content   # bare body
primectl doc list <col> --prefix notes/
primectl doc move <col> notes/a.md notes/b.md
primectl doc delete <col> notes/old.md

# custom operations + escape hatch
primectl call agent status <id>
primectl raw GET /v1/health
```

## Output formats

`-o table` (default), `json`, `yaml`, `name` (ids only), `wide`.
The -o flag is accepted by the read commands (get, describe, call, raw); write commands (create, apply, delete, edit) print a status line.

## Config

`~/.primectl/config.yaml` (override with `PRIMECTL_CONFIG`). Token resolution:
`--token` > context token (inline or `env:VARNAME`) > `PRIMER_API_TOKEN` > none.

## Shell completion

Typer provides it: `primectl --install-completion`.

## Not in v1

WebSocket streaming (`watch`/`logs`) is intentionally out of scope; v1 is
request/response only.
