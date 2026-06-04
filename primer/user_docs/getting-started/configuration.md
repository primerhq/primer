---
slug: configuration
title: Configuration
section: getting-started
summary: The config file, environment variables, and the order primer reads them in.
---

## Three sources, one order

Primer reads configuration from three places. They are merged in
order of precedence, lowest to highest:

1. Defaults baked into `primer/api/config.py` (the `AppConfig`
   Pydantic settings class).
2. A TOML file at `$PRIMER_CONFIG_PATH` (when set).
3. Environment variables prefixed with `PRIMER_`.

Later sources win. So an env var beats a TOML setting, which beats
the built-in default.

```callout:warning
Secrets follow the same precedence rule. If you commit a
`primer.toml` with a `PRIMER_SESSION_SECRET`, an operator's
production env var will override it -- but anyone who can read the
file still has the secret. Keep secrets in env vars or in a vault,
not in the TOML.
```

## The TOML file

Point `PRIMER_CONFIG_PATH` at a TOML file. Most operators only set
the storage backend and the auth settings; defaults are sensible
for everything else.

```code-tabs:yaml,bash
--- yaml
# primer.toml
host = "0.0.0.0"
port = 8000
auto_bootstrap = true

[db]
provider = "postgres"
dsn = "postgres://primer:secret@db.example/primer"

[auth]
require_auth = true

[observability]
enabled = true
otel_endpoint = "http://otel-collector:4318"
--- bash
# Set the path, then run as normal.
export PRIMER_CONFIG_PATH=/etc/primer/primer.toml
uv run primer api
```

## Environment variables

Every `AppConfig` field is reachable via env var with the
`PRIMER_` prefix and double-underscore nesting. Examples:

```code-tabs:bash
--- bash
# Top-level scalar
export PRIMER_PORT=9000

# Nested struct (db.provider)
export PRIMER_DB__PROVIDER=postgres
export PRIMER_DB__DSN=postgres://primer@localhost/primer

# Auth subblock
export PRIMER_AUTH__REQUIRE_AUTH=true
```

The double underscore is Pydantic's `env_nested_delimiter`; a
single underscore reads as a normal field-name separator.

## Where to look next

For the full enumerated list of `PRIMER_*` env vars and their
defaults, see the reference:

```ref:reference/rest-api-overview
The reference section enumerates env vars in its own doc once
Phase H of the doc rollout lands.
```
