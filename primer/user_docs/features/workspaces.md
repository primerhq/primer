---
slug: workspaces
title: Workspaces
section: features
summary: Create and manage a workspace in the console -- pick a provider, create a template, spin up an instance, browse files, and run a diagnostic.
---

## Overview

A workspace is the isolated sandbox an agent lives and acts inside. It
gives the agent a real filesystem, a shell, and a git-backed state history.
The console covers the full lifecycle: registering a provider, authoring a
template, creating an instance, browsing files, and running diagnostics.

```ref:concepts/workspaces
Background on the three vocabulary levels (provider, template, instance),
the probe loop, and the state history git repo.
```

## Browse the workspaces list

Go to **Workspaces** in the left nav. The list shows every instance with
its name, id, template, provider, phase pill (running / pending / failed /
terminating), and creation time.

Use the filter bar to narrow by free text (matches name, id, template, or
provider) and by template or provider dropdown. Click any row to open the
workspace detail view.

## Create a template

Before you can create a workspace you need a template. If the "New
workspace" modal warns that no templates exist, click **Create a template
now** inside the modal to open the template form inline.

```embed:workspace-template-form
```

The template form collects:
- **Name** -- a unique identifier for this template (e.g. `python-3.13-default`).
- **Provider** -- the backend that will materialise instances from this template.
- **Base image or base path** -- the container image (Docker/Kubernetes) or host
  directory path (local backend).
- **TTL** -- how many minutes an instance may be idle before the probe loop
  flips it to failed.
- **Environment variables** -- key-value pairs injected into the workspace
  environment at materialisation time.
- **Init command** -- a shell command run once when the instance is first
  created (package installs do not belong here; they should be baked into
  the image).

Click **Create template**. The template row appears in the template list.
The provider validates that the image or base path is reachable but does not
spin up any instance yet.

```callout:info
A change to a template does not affect existing instances. Instances keep
the recipe they were created from. To pick up a template change, create a
new instance.
```

## Create a workspace instance

1. Click **New workspace** in the filter bar.
2. In the modal:
   - **Name** (optional) -- a human-readable label like "Investing research".
     The backend generates the unique id.
   - **Template** -- pick from the dropdown of registered templates. If the
     list is empty, use the inline link to create a template first.
3. Click **Create**. The console navigates to the new workspace detail page.

The workspace enters the `running` phase once the provider materialises the
sandbox. If materialisation fails, the phase flips to `failed` and a banner
with the failure reason appears at the top of the detail page.

## Browse and edit files

Open a workspace and click the **Files** tab. A two-pane layout appears:

- **Left pane (tree)** -- the workspace filesystem rooted at `/`. Folders
  expand on click; use the new-file and new-folder icons in the pane header
  to create entries. Hovering a row reveals a trash icon to delete it.
  Directories under `.state` and `.tmp` are shown but cannot be deleted --
  they are backend-managed.

- **Right pane (editor)** -- clicking a file in the tree loads its contents.
  For Markdown files a "Rendered" / "Raw" toggle switches between the rendered
  view and the source. Click **Edit** to enter edit mode, modify the text
  in the textarea, then click **Save**. Click **Download** to fetch the
  raw file. Click **Delete** to remove the file (with a confirmation modal).

```callout:warning
File deletes from the console are permanent and cannot be undone from the
console. The `.state/` git repo retains a history of agent-written changes,
but console deletes bypass that history.
```

## Check sessions on a workspace

The **Sessions** tab lists every session that has run or is running on this
workspace -- session id, agent, status, start time, and last activity.
Click a session row to open the session detail view. The tab badge shows the
live count.

## Read the state log

The **Log** tab shows the `git log` of the workspace's `.state/` git repo as
a timeline. Each commit row shows the short SHA, operation kind, agent id,
session suffix, and timestamp. Click a row to expand the file diff for that
commit.

Use **Load more** at the bottom to page through older commits (up to 500).

## Run a diagnostic

Click **Run diagnostic** in the workspace detail header to send a diagnostic
probe to the workspace. The diagnostic modal shows stdout / stderr from the
probe command and reports whether the workspace runtime is healthy. Use this
to confirm the workspace is reachable after a failed phase.

```embed:workspaces
```

## Rename a workspace

In the workspaces list, click the edit (pencil) icon on any row to open a
rename modal. Change the name and click **Save**. Clear the field to remove
the label. The underlying id is unchanged.

## Pause and resume

The **Pause** and **Resume** buttons are reserved and not yet active. Watch
the release notes for availability.

```ref:reference/api-workspaces
Automate this -- the API reference covers providers, templates, workspace
instances, the file sub-API, the diagnostic exec endpoint, and pause/resume.
```
