---
title: "Collaboration"
summary: "How agents work together in one install: the file-based coordinator and sub-agents pattern, and the other coordination surfaces."
order: 180
---

Agents in one install coordinate with each other through the platform's existing surfaces, not through a new primitive. The lead pattern is file-based: a single coordinator session hands out tasks through a file and waits with a file watch, and each sub-agent session works from the file and answers in its own response file. Start with [Coordinator and sub-agents](collaboration/coordinator-pattern). For cross-session messaging, trigger events and in-turn delegation, read [Other collaboration patterns](collaboration/other-patterns).
