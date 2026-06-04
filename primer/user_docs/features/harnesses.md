---
slug: harnesses
title: Harnesses
section: features
summary: Git-installed entity bundles that ship agents, toolsets, and triggers together for repeatable deploys.
---

## What harnesses solve

Without harnesses, deploying primer to a new environment means
running a sequence of REST calls (or console clicks) to create
each agent, define each toolset, wire each trigger. That is fine
for one operator on one host. It does not scale to two.

A harness is a git repo that ships a `manifest.yaml` plus the
code (Python toolsets, prompt templates, trigger configs) for an
opinionated bundle. Installing a harness creates every entity in
the manifest in one step.

## The install wizard

The install wizard walks four steps. The Manifest step shows
what is about to be created so you can confirm before any write
hits the database.

```mockup:harness-wizard-step
{ "step": 2 }
```

The other steps in order: Source picks the git URL; Bindings
attaches harness toolsets to harness agents (or to existing
agents); Confirm flips the trigger before the install runs.

## Installing from the CLI

The same wizard is reachable from the CLI for headless deploys:

```code-tabs:bash,python
--- bash
# Install from a public repo.
uv run primer harness install \
  --source https://github.com/codemug/harness-pr-reviewer \
  --branch main

# Inspect what would change without writing.
uv run primer harness install \
  --source https://github.com/codemug/harness-pr-reviewer \
  --dry-run
--- python
result = client.harnesses.install(
    source="https://github.com/codemug/harness-pr-reviewer",
    branch="main",
    dry_run=False,
)
for agent in result.created_agents:
    print("created", agent.name)
```

## Updates and uninstall

Harnesses pin a commit on install. To pull updates, run the
install command against the same harness id; primer fetches the
new commit and shows a diff before applying. Uninstall removes
every entity the harness created.

```callout:warning
Uninstall does not touch entities the harness created and the
operator subsequently modified. The wizard surfaces those as
'orphans' so you can decide whether to keep them. Modifying a
harness-installed entity is the supported way to fork its
behaviour without losing the upstream lineage.
```

## Where to bind toolsets

A harness's bindings step is where you decide whether the
harness's agents reach only the harness's toolsets, or also the
built-in toolsets the rest of your primer environment uses.

```ref:concepts/toolsets-and-tools
The concept page covers the two-level binding model.
```
