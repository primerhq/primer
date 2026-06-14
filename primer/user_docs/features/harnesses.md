---
slug: harnesses
title: Harnesses
section: features
summary: Package a working set of agents, graphs, collections, and toolsets into a versioned git bundle, then install or update it in one operation.
---

## Concept

A harness is a git repository that ships a bundle of primer entities - agents, graphs, collections, documents, and toolsets - as a single versioned artifact. Installing a harness creates all those entities in one operation. Updating to a new commit is a two-step Fetch and Sync.

Think of a harness as "Helm for primer": your configuration becomes a versioned artifact that a teammate or customer can drop into their own primer instance and run immediately. The upstream repo is the source of truth; the installed entities on disk follow it.

Harnesses solve two problems:

1. **Repeatability.** A tuned configuration - carefully-worded system prompts, a specific graph topology, a curated collection of documents - is easy to share as a URL instead of as a set of screenshots and manual steps.
2. **Manageability.** When the upstream repo updates (an improved prompt, a new graph node, a revised document), a Fetch and Sync pulls it in without touching entities the operator has edited locally.

### Lifecycle states

A harness moves through these states:

| Status | Meaning |
|---|---|
| `draft` | Registered but not yet fetched. No bundle is loaded. |
| `ready` | Fetched but not yet installed. Bundle and overrides schema cached. |
| `installed` | Applied. Managed entities are live. |
| `outdated` | Installed, but the upstream repo has a newer commit. |
| `error` | The last fetch, install, or sync failed. Error details shown on the detail page. |

### Overrides schema

A harness can declare a JSON Schema for overrides - a set of deployment-specific values (API keys, base URLs, tenant identifiers) that the bundle renders at install time. Operators fill in the override values before installing; they are validated against the schema before the INSTALL operation proceeds. If no schema is declared, no override step is needed.

### Managed entities and local edits

Every entity the harness creates is tracked. Entities you edit after install are still listed under Managed objects but are flagged as locally modified. A Sync does not overwrite locally-modified entities - they retain your edits. An Uninstall shows the modified entities and asks whether to keep or remove them.

## Configuration

### Harness fields

| Field | Notes |
|---|---|
| **Name** | Human-readable label. Editable after creation. |
| **Slug** | Kebab-case identifier. Must match `^[a-z][a-z0-9-]{1,63}$`. Unique. Auto-derived from name. Used as the entity id prefix for all managed entities. |
| **Git URL** | HTTPS URL of the repository (for example, `https://github.com/org/repo`). |
| **Ref** | Branch, tag, or commit SHA to track. Defaults to `main`. Changing this after install marks overrides dirty. |
| **Subpath** | Optional subdirectory within the repo that contains `manifest.yaml`. Leave blank for the repo root. |
| **Git token** | Optional personal access token for private repos. Stored encrypted. |
| **Description** | Optional. |
| **Overrides** | The deployment-specific values dict. Validated against the harness's overrides schema before install. |

## Walkthrough

```embed:harness
```

### Registering a harness

1. Navigate to **Harnesses** in the left nav.
2. Click **Register from git**.
3. In the **Register harness** dialog, fill in the **Source** step: name, slug, Git URL, ref, subpath (optional), git token (optional for private repos).
4. Click **Fetch**. Primer creates the harness record and pulls bundle metadata from the remote. A progress indicator shows while the fetch runs.
5. If the harness declares an overrides schema, an **Overrides** step appears. Fill in the required fields and click **Create**.
6. Once the install operation completes, the status shows **INSTALLED** and the console navigates to the harness detail page.

```callout:warning
If the fetch step returns an error, check that the Git URL is correct and that the branch or tag exists. For private repos, verify the Git token has read access to the repository. The error message is shown inline in the dialog.
```

### Updating to a new commit

When the upstream repo has new commits, the harness status changes to **OUTDATED**.

1. Open the harness detail view.
2. Click **Fetch** to pull the latest commit metadata. The Metadata panel updates the **Available commit** field once the fetch completes.
3. Click **Sync** to re-apply the bundle from the new commit. Status returns to **INSTALLED** when done.

Fetch and Sync are separate so you can inspect the incoming commit before applying it. Both buttons are disabled while any pending operation is in progress.

### Inspecting managed objects

The **Managed objects** panel on the harness detail page lists every entity the harness created, grouped by type: Agents, Graphs, Collections, Documents, Toolsets.

Each group shows the entity ids. Locally-modified entities are flagged. A Sync re-applies the upstream bundle but does not overwrite flagged entities.

### Uninstalling a harness

1. Open the harness detail view.
2. Click **Uninstall** and confirm.
3. The worker cascade-deletes every entity the harness created that has not been locally modified. Locally-modified entities are listed in the confirmation dialog so you can decide whether to keep or remove them.

```callout:warning
Uninstall removes the harness record and all unmodified managed entities. This cannot be undone. Entities you edited after install appear in the confirmation dialog - you decide whether to keep or delete them.
```

## What happens after

Once a harness is installed, the managed entities behave identically to entities you created by hand. Agents appear in the Agents list and can be used in chats or sessions. Graphs appear in the Graphs list and can be run as sessions. Collections and documents appear in their respective pages and are queryable. Toolsets registered by the harness are available in the toolset picker.

The harness record links them all back to the source bundle. Any of them can be edited without breaking the link, though edits do orphan those entities from future Sync operations.

When the harness repository receives a new commit, the outdated indicator appears. Fetch then Sync pulls the new commit and re-applies to unmodified entities, leaving locally-edited ones untouched.

Agents and graphs are built from live snapshots at install time, not pinned snapshots. If the harness manifest references a model or provider that has since been removed from your primer instance, the install or sync will fail with an error shown on the detail page.

```ref:features/agents
Agents and their configuration fields.
```

```ref:features/toolsets-system
How a harness attaches toolsets to agents using the two-level binding model.
```

```ref:reference/api-harnesses
Full harness resource schema, register, fetch, sync, and uninstall endpoints.
```
