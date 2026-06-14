---
slug: harnesses
title: Harnesses
section: features
summary: Register a git-backed bundle of agents, graphs, collections, documents, and toolsets, sync it to a new commit, inspect managed objects, and uninstall.
---

## Overview

A harness is a git repository that ships a bundle of agents, graphs, collections, documents, and toolsets together. Registering a harness creates all of those entities in one operation. When the upstream repo is updated, a Fetch followed by a Sync pulls the new commit and re-applies the bundle. The Harnesses page lists all registered harnesses and links to each detail view.

## Registering a harness

1. Navigate to **Harnesses** in the left nav.
2. Click **Register from git** (top-right of the filter bar).
3. The **Register harness** dialog opens at **Step 1: Source**. Fill in:
   - **Name** -- a human-readable label. The slug is auto-derived from the name but you can edit it. The slug is used as an ID prefix for all entities the harness creates.
   - **Git URL** -- HTTPS URL of the repository (for example, `https://github.com/org/repo`).
   - **Ref** -- branch, tag, or full SHA to track. Defaults to `main`.
   - **Subpath** -- optional subdirectory inside the repo that contains the `manifest.yaml`. Leave blank if the manifest is at the repo root.
   - **Git token** -- optional personal access token for private repos. Stored encrypted.
4. Click **Fetch**. The console creates the harness record and pulls metadata from the remote. A progress indicator shows while the fetch runs.
5. If the harness has an overrides schema, **Step 2: Overrides** appears. Fill in any required configuration fields and click **Create**.
6. The harness is installed. The console navigates to the harness detail view and the status shows **INSTALLED**.

```callout:warning
If the fetch step returns an error, check that the Git URL is correct and that the branch or tag exists. For private repos, verify that the Git token has read access to the repository. The error message is shown inline in the dialog.
```

## Fetching and syncing to a new commit

When the upstream repo has new commits, the harness status changes to **OUTDATED** and the card in the list shows an amber drift indicator.

1. Open the harness detail view by clicking its card.
2. Click **Fetch** to pull the latest commit metadata from the remote. The Metadata panel updates the **Available commit** field once the fetch completes.
3. Click **Sync** to re-apply the bundle from the fetched commit. The status returns to **INSTALLED** when the sync finishes.

Fetch and Sync are separate steps so you can inspect what commit is available before applying it. Both buttons are disabled while a pending operation is in progress.

## Inspecting managed objects

The harness detail view shows a **Managed objects** panel that lists every entity the harness created, grouped by type: Agents, Graphs, Collections, Documents, and Toolsets.

Each group shows the count of managed entities and their IDs. Entities that the harness created but that you have since modified are still listed here. Any such modifications are preserved across a Sync -- the harness does not overwrite hand-edited entities unless you explicitly uninstall and re-install.

## Uninstalling a harness

1. Open the harness detail view.
2. Click **Uninstall** (top-right of the action bar).
3. Confirm in the dialog. The worker cascades-deletes every entity the harness created and removes the harness row once the operation completes.

```callout:warning
Uninstall removes all entities the harness created that have not been individually modified. Entities you edited after install are flagged as orphans in the confirmation dialog -- you decide whether to keep or delete them. This action cannot be undone.
```

## See also

```ref:features/toolsets-system
The two-level binding model used when a harness attaches toolsets to agents.
```

## Automate this

```ref:reference/api-harnesses
Full harness resource schema, register, fetch, sync, and uninstall endpoints.
```
