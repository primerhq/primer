---
slug: install
title: Install primer
section: getting-started
summary: Install primer locally with uv or via the published Docker image.
---

## Requirements

Primer targets Python 3.13 on Linux and macOS. Windows works via
WSL2. A working `git` install is required for the workspace
features that drive sandboxed environments.

```callout:warning
Allocate at least 4 GB of free memory before starting primer. The
workspace pool and the LLM call buffer share the same address space,
and tight memory shows up as flaky tool calls long before it shows
up as an out-of-memory crash.
```

## Pick an install path

Two supported install paths. Pick the one that matches how you
already run Python services.

```code-tabs:bash,docker
--- bash
git clone https://github.com/codemug/primer.git
cd primer
uv sync
uv run primer api
--- docker
docker pull ghcr.io/codemug/primer:latest
docker run -p 8000:8000 \
  -v $HOME/.primer:/data \
  ghcr.io/codemug/primer:latest
```

After startup the console listens on port 8000. Open
`http://localhost:8000/console/` to land on the dashboard.
