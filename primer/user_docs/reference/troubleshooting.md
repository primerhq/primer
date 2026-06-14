---
slug: troubleshooting
title: Troubleshooting
section: reference
summary: "How to reason about the four most common failure patterns: a stuck run, an inaccessible tool, an unreachable provider, and a workspace that failed to materialise."
---

## How to think about problems

Most problems in Primer belong to one of four categories. Identifying the
category narrows the diagnosis immediately.

- **A run that is parked and not moving.** The run is waiting on an event
  that has not arrived, or on an approval that has not been answered.
- **A tool the agent says it cannot use.** The tool exists but is not
  bound to the agent's toolset, or the toolset itself is not bound to the
  agent.
- **A provider the platform cannot reach.** The LLM, embedder, or web
  search backend is unreachable or misconfigured.
- **A workspace that failed to materialise.** The workspace backend (local
  filesystem, container, or Kubernetes) encountered an error during setup.

Each section below describes what causes the symptom and how to
distinguish it from look-alike problems.

## A run that is parked and not moving

A parked run is normal. A run parks whenever a yielding tool is active:
`ask_user`, `subscribe_to_trigger`, `sleep`, `watch_files`, or a tool
approval gate. Parking releases the worker lease; no worker is consumed
while the run waits. This is intended behaviour.

A run that stays parked longer than expected usually has one of three
causes:

**The awaited event has not fired.** For `ask_user`, the question is
waiting for a reply. For `subscribe_to_trigger`, the trigger has not
ticked yet, is disabled, or is past its one-shot window. For a timed
sleep, the duration has not elapsed. These are not errors; the run will
resume when the event arrives.

**The approval gate has not been answered.** A required approval policy
parks the run until an operator responds. If the approvals queue is full
or unmonitored, runs accumulate. Each parked approval occupies no worker
slot, but if enough approvals pile up and nothing is ever resolved, new
work may queue behind them in a different way: the overall backlog grows.
Approve, reject, or cancel the pending approvals to unblock the queue.

**The approval gate timed out and the run is in an unexpected branch.**
When a required approval gate reaches its timeout, the gate closes and
the call is denied. The run unparks and the agent receives a denial error.
If the agent does not handle the denial gracefully, it may loop or
terminate unexpectedly. Check whether the agent's prompt handles timeout
and denial as distinct outcomes.

The workers and health view shows each worker's in-flight count and which
runs are currently parked. A high parked count with low in-flight activity
is the signature of an approval backlog.

## An agent denied a tool

When an agent reports that a tool is unavailable, the most likely cause is
that the toolset containing the tool is not bound to the agent, or that the
tool does not exist under any toolset the agent can see.

Toolsets are named collections of tools. An agent only has access to the
tools inside toolsets that have been explicitly bound to it. A tool that
was added to a different toolset, or a toolset that was created but never
bound, is invisible to the agent even if the tool's implementation is
perfectly healthy.

A secondary cause is a policy that is actively denying the tool. If a Rego
or LLM-judge policy evaluates the call and returns a block decision, the
agent receives a denial error that looks similar to the tool not existing.
The distinction is that a policy denial happens after the tool is
dispatched (the call reached the gate), whereas a missing toolset binding
means the call was never formed in the first place.

To distinguish the two: if the agent has never attempted to call the tool,
the toolset binding is missing. If the agent attempted the call and
received a denial, an approval policy is blocking it.

## A provider the platform cannot reach

The platform talks to external model providers: LLM providers for
inference, embedding providers for vector operations, web search backends
for retrieval. Each provider is an entity with an endpoint and,
optionally, an API key.

When a provider is unreachable the platform returns a structured error
rather than a generic 500. The error identifies the provider and the
underlying failure (connection refused, bad key, rate limit, timeout).

Common causes:

**Wrong endpoint or port.** A locally-running model server (such as
LM Studio or Ollama) defaults to a specific port. If the provider was
configured with a different port, every call will fail with a connection
error. Check the base URL on the provider against the actual port the
server is bound to.

**Expired or rotated key.** Hosted providers (OpenAI, Anthropic,
OpenRouter) issue per-account API keys. A rotated or deleted key causes
immediate 401 or 403 errors from the upstream. The platform surfaces the
upstream HTTP status in the error detail.

**Fallback chain ordering.** When a web search active config is set to
aggregated mode with multiple providers, the platform tries each in order
and falls through to the next on failure. If the first provider fails
silently (for example, a timeout rather than a 4xx), the fallback adds
latency on every request. Move the working provider to the first position
to eliminate the delay.

**Rate limits.** A rate-limited provider returns a 429 from the upstream.
The platform propagates the error to the session turn. The run does not
automatically retry against the same provider; the agent receives the
error and can decide whether to retry on its next turn.

## A workspace that failed to materialise

A workspace is a sandboxed filesystem environment. Materialising a
workspace means the platform sets up that environment according to the
template: creating directories, seeding files, and, for container and
Kubernetes backends, pulling images and starting the runtime.

Materialisation can fail at several points:

**Template misconfiguration.** If the template references a backend
provider that does not exist or uses an incompatible configuration, the
workspace creation fails immediately with a validation error. The error is
visible on the workspace row.

**Container backend failures.** For Docker-backed workspaces, common
causes are: the Docker daemon is not running or not reachable by the
platform process, the image does not exist or cannot be pulled, or the
container exits immediately because of a failing entrypoint. A container
that starts but then crashes during an agent tool call will leave the
workspace in a failed state.

**Kubernetes backend failures.** For cluster-backed workspaces, the most
common cause is a missing credential (the platform needs a valid
kubeconfig or in-cluster service account). A Pod that is stuck in
`Pending` because no node has capacity, or that fails its readiness probe,
will also cause the workspace to appear unhealthy.

**Permission and git errors.** The local backend writes to a directory
controlled by the platform process. If the directory is not writable, or
if `git` is not available on the host, the workspace cannot be initialised.
The error appears on the workspace entity and in the platform logs.

In all cases the workspace entity records a status and a last-error field.
The platform also logs the specific failure at the point it occurs.

```ref:features/workers
The workers view shows current worker capacity, in-flight runs,
and the parked-run breakdown that helps distinguish an approval backlog
from a provider outage.
```
