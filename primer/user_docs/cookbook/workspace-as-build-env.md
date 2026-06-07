---
slug: workspace-as-build-env
title: Workspace as a build environment
section: cookbook
summary: Use an ephemeral workspace to build and test a Rust project on demand.
difficulty: intermediate
time_minutes: 40
tags: [workspaces, agents, harnesses]
---

## Goal

Treat the workspace as a one-shot build environment. An agent spins
up a workspace from a Rust toolchain template, clones the target
repo, runs `cargo test`, and reports the result.

## Prerequisites

- A Docker workspace provider registered under **Workspaces**.
- Access to the container image `rust:1.83-slim` from the Docker
  daemon the provider uses.

```ref:features/workspaces
Register a provider, create a template, and understand the
workspace lifecycle before following this recipe.
```

```ref:features/agents
Create the build agent and bind the tools it needs.
```

## Steps

### 1. Create the workspace template

1. Open **Workspaces** in the left nav.
2. Click **New workspace**; if no templates exist, click **Create a
   template now** inside the modal.
3. Fill in the template form:
   - **Name**: `rust-1.83`
   - **Provider**: your Docker provider
   - **Base image**: `rust:1.83-slim`
   - **TTL**: 20 (minutes) -- long enough for a full build cycle,
     short enough to avoid idle accumulation
   - **Environment variables**: add `CARGO_TERM_COLOR=never` so
     test output is plain text
   - **Init command**: `apt-get update && apt-get install -y git`
4. Click **Create template**.

```embed:workspace-template-form
```

```callout:tip
Set the TTL just longer than your heaviest typical build. Too short
and the workspace is torn down mid-build; too long and idle
workspaces accumulate between runs.
```

### 2. Create the build agent

1. Open **Agents** and click **New agent**.
2. Set the ID to `rust-builder`.
3. Pick an LLM provider and model (a mid-tier model is sufficient
   for a deterministic build script).
4. On the **Tools** tab, select the shell and filesystem tools from
   the system toolset.
5. On the **Advanced** tab, enter a system prompt such as:
   "Clone the repo at the given URL, check out the given SHA, run
   `cargo test`, and report pass/fail with the full test output."
6. Click **Create**.

### 3. Create a workspace instance

1. Click **New workspace** in the Workspaces filter bar.
2. In the modal, set **Name** to something like `rust-build-env`
   and select the `rust-1.83` template.
3. Click **Create**. Wait for the phase pill to turn to **running**.

### 4. Start a build session

1. Open **Sessions** and click **New session**.
2. Select the `rust-builder` agent.
3. In the input field, provide the repo URL and commit SHA, for
   example:
   `repo=https://github.com/example/my-crate sha=abc123`
4. Select the `rust-build-env` workspace.
5. Click **Start**.

```embed:sessions-list
```

## Verification

Open the new session in **Sessions** to watch the transcript. The
agent clones the repo, checks out the commit, runs `cargo test`,
and returns the output. A passing build ends with a message similar
to "test result: ok. N passed; 0 failed."

```embed:session-detail
```

The **Log** tab on the workspace detail page shows the git-backed
state history. Each agent write appears as a commit, giving you a
timestamped record of every build artifact the agent touched.

## Gotchas

```callout:warning
The Docker provider runs builds in containers. Resource caps (CPU,
memory) come from the template -- not from the agent configuration.
A build that needs more memory than the template allows will fail
silently and the agent sees a truncated test output. Match the
caps to the heaviest crate you build.
```

- Workspace network egress is provider-dependent. The local
  provider has full egress; Docker follows the daemon network
  config; Kubernetes follows the NetworkPolicy.
- For repeatable builds, pin the Rust toolchain in the repo via a
  `rust-toolchain.toml` file rather than relying on the base image
  version.
- Instances created from a template do not pick up template changes
  automatically. If you update the `rust-1.83` template, create a
  new workspace instance to use the updated recipe.

## Automate it

```ref:reference/api-workspaces
Create templates, provision instances, and start build sessions
programmatically via the REST API.
```
