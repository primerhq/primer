---
title: "Agent runtimes"
summary: "Admin: configure Claude Code runtime providers or operator-provisioned Codex model brokers."
audience: admin
order: 8
tools: [system__create_llm_provider, system__fetch_llm_provider_models, llm_providers__get_llm_provider_runtime_status]
related: [providers/llm, providers/model-profiles]
---
## What it is
The console's Agent runtimes group includes CLI-backed providers and the Codex model broker. `claude_code` drives the operator's own logged-in `claude` CLI (Claude Code) on each worker host as the model backend: primer keeps the system prompt, the conversation, the tool catalogue and the execution of every tool call, and Claude Code contributes the model call and nothing else. Codex uses the dedicated broker described below. Each row is an ordinary [LLM provider](providers/llm): create it, check the host, register [model profiles](providers/model-profiles) for the models you will use, and point agents at the profiles.

## Concepts
- Codex uses a separately provisioned model broker with dedicated credentials and signed permissions. It is grouped under Agent runtimes in the console, but its implemented transport is not the stock CLI. See the Codex model broker instructions below.
- `provider` is `claude_code`. `config` has two fields and no credential: `binary_path` (default `claude`, resolved on the worker's `PATH`) and `pass_env` (names of extra worker variables copied to the child, for example `HTTPS_PROXY` on hosts behind a proxy). A `pass_env` name that starts with `PRIMER_` or contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD` or `DATABASE` is refused, so primer's own secrets cannot reach the child.
- Install `claude` on every host that runs a worker, and log in once, under the user that runs primer. The CLI's own login on the worker host is the credential; primer never stores, reads or forwards it. A worker that lacks the CLI fails the step with an error that names the binary and the host; a CLI that is logged out fails the step with an authentication error.
- Model discovery answers with the CLI's aliases `sonnet`, `opus` and `haiku` (a profile's model name may use an alias or a full model id). Set the context length on the [model profile](providers/model-profiles).
- `limits` works as for any provider; `max_concurrency` bounds the simultaneous Claude Code processes, and so the load on the operator's plan.
- Every model step spawns one fresh `claude` process: about 2.2 s from spawn to the first result per step, and one CLI process per concurrent model call, so the memory is bounded by `max_concurrency`.
- Claude Code adds its own text to every request: an identity line, the account email in a reminder, and an environment block. The CLI's login decides whose plan the call runs on.
- `temperature`, `top_p`, `stop` and `max_output_tokens` are ignored: the CLI exposes no control for them. A profile's reasoning level maps to the CLI's effort, and `response_format` becomes a JSON schema the CLI returns as text.
- A user message that is a known command name (for example `/context`) is never run as a command: the CLI is started with `--disable-slash-commands`, so the step gets the CLI's "isn't available in this environment" reply as a normal answer (no local command, no model call); ordinary text that merely contains a slash (for example `/etc/hosts is wrong`) reaches the model.
- The provider is admin-created and private by default, like every provider. Admins may use it, as may turns started by the operator's channels, webhooks and triggers. Every request through the provider runs on the plan of the `claude` CLI login on the worker host, and the plan's terms ([legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)) are the provider owner's responsibility.

## How to
1. Install `claude` on every worker host and log in once, as the user that runs primer.
2. Create the provider row (`provider: "claude_code"`), naming `binary_path` and any `pass_env` the hosts need.
3. Check the host: the status answers installed, version, logged in and meets-minimum on the host that serves it.
4. Ask which models it serves: the same check runs, and the answer is the aliases with the status alongside.
5. Register a model profile per model you will use, and point agents at the profiles: [create and edit an agent](agents/create-and-edit).

## Tool calls
### Check the host
`llm_providers__get_llm_provider_runtime_status` runs `claude --version` and `claude auth status` on the host that serves the call and answers with `{host, installed, version, logged_in, meets_minimum}`. It never returns the account email. It 404s for a row that is missing or not a `claude_code` provider.

```json
{"toolset_id": "llm_providers", "tool_name": "get_llm_provider_runtime_status", "arguments": {"provider_id": "claude-1"}}
```

Reference: [`llm_providers__get_llm_provider_runtime_status`](reference/tools/llm_providers/get_llm_provider_runtime_status)

### Create the provider
`system__create_llm_provider` registers the runtime; the [LLM providers](providers/llm) page shows the rest of the lifecycle.

```json
{"toolset_id": "system", "tool_name": "create_llm_provider", "arguments": {"entity": {"id": "claude-1", "provider": "claude_code", "config": {"binary_path": "claude", "pass_env": ["HTTPS_PROXY"]}, "limits": {"max_concurrency": 2}}}}
```

Reference: [`system__create_llm_provider`](reference/tools/system/create_llm_provider)

### Discover the models it serves
`system__fetch_llm_provider_models` probes a stored provider with its own config. For a `claude_code` row it runs the host check and answers with the aliases when it passes.

```json
{"toolset_id": "system", "tool_name": "fetch_llm_provider_models", "arguments": {"provider_id": "claude-1"}}
```

Reference: [`system__fetch_llm_provider_models`](reference/tools/system/fetch_llm_provider_models)

## Gotchas
- The step never checks the CLI's version or login: a missing binary fails it with an error that names the binary and the host, and a logged-out CLI fails it with an authentication error. The minimum version (2.1.322, the version the stream contract is pinned to) is enforced only by model discovery, which refuses a missing, older or logged-out CLI with a 400 that names the host; the status route refuses nothing and reports the facts (installed, version, logged in, meets the minimum), so it is the way to check a host before a turn does.
- Claude Code cannot execute a tool on its own: every tool call returns to primer, which executes it, with approval gates included.
- An interrupted step reports 0 usage; that step is not counted in the recorded token totals.

## Codex model broker

A `codex` provider runs model steps through the broker's Responses connection.
The operator provisions dedicated sign-in and signed model permissions, and
binds the provider ID on the API for discovery and on each worker for inference.
If a step reports a missing or mismatched binding, check that worker's configuration.
Stock Codex CLI 0.171.0 has no qualified mode for running this provider with
Primer as the sole tool executor.
Terminal tool calls and completion events are readmitted against the original broker policy after a
paused consumer resumes. A policy change or expired registration fails the step;
start a new admitted step rather than replaying buffered tool actions.

For a saved Codex provider, `system__fetch_llm_provider_models` returns the
account-visible model names allowed by the operator's current signed policy.
Profile creation and update check the same list. No particular model name is
automatically allowed, and an operator catalogue file does not grant account
access. Discovery supplies no guessed context length: fill that required profile
field from supported model information. A successful discovery or profile save
does not prove inference works; every inference step still checks authorization.
Draft discovery has no saved account binding and refuses. Configure the saved
provider's trusted broker first.

In the console, choose **Providers > Agent runtimes > Register**, select
**Codex**, and enter the exact provider ID supplied by the operator. Leave
the broker connection and credentials to the operator; the form exposes no CLI
or login fields and registers an empty config. Choose **Register provider**.
Reopen that saved provider and choose **Import more models**. Select an offered
model, enter its context length from supported model information, and import it.
Then choose the new profile on the agent. If discovery fails, resolve the broker
binding or account/policy error before importing; there is no draft or model-name
fallback. Existing rows with CLI overrides need the operator to reset their
config to the broker defaults before they can discover or run models.
If first-run setup resumes the saved broker, its model step likewise requires
you to enter the model's supported context length when discovery supplies none.

## Related
[LLM providers](providers/llm), [model profiles](providers/model-profiles).
