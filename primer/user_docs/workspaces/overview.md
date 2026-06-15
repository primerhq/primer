---
slug: workspaces-overview
title: Workspaces
section: workspaces
summary: A workspace is a provider, a template, a materialised instance, and the sessions that run on it; here is how those four pieces fit together.
---

## The four pieces

A workspace gives an agent a real place to work: a filesystem it can read and write, a shell it can run commands in, and a git-backed `.state/` history. Four concepts share the workspace namespace:

- **Provider**: the backend configuration: which runtime (local filesystem, container daemon, Kubernetes cluster) and how to reach it.
- **Template**: the materialisation recipe that references a provider: which image or base path, environment variables, initial files, and init commands.
- **Instance**: the live, materialised sandbox created from a template. Many instances can come from one template.
- **Sessions**: the runs hosted on an instance. One instance hosts many sessions at once.

The **workspace toolset** lets agents manage all of the above programmatically, and **yielding tools** let a session park on an external event and resume when it fires.

```ref:workspaces/workspace-providers
Register local, container, and Kubernetes workspace providers.
```

```ref:workspaces/workspace-templates
Author templates that materialise workspaces from a provider.
```

```ref:workspaces/workspaces-and-sessions
How a workspace instance hosts sessions, and the session lifecycle.
```

```ref:workspaces/workspace-toolset
The workspace toolset agents use to manage providers, templates, workspaces, and sessions.
```

```ref:workspaces/yielding-tools
How a session parks on a tool call and resumes when the event fires.
```
