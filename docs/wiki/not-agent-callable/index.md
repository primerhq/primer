---
title: "Human and deployment boundaries"
summary: "Human sign-in, decisions, secrets and deployment steps that a discovered operation does not replace."
order: 160
---

The platform has generated tools for service, provider, account, event and worker operations as well as curated tools. Their references are linked from the owning feature pages. This page records the human or deployment boundary that remains; a generated operation never removes the route's cookie requirement, ownership check or a person's decision.

- **Human sign-in:** First registration, password login/logout, sign-out-everywhere and password changes, plus SSO login/link/callback, are the eight intentionally skipped operations. Complete them in the browser; they do not appear as target tools. See [accounts and tokens](start-here/accounts-and-tokens).
- **API tokens:** Token management remains cookie-only. A bearer credential cannot mint or administer another token, even when a route-backed target is visible. Copy plaintext once and never place it in a shared prompt or wiki page.
- **Single sign-on:** OIDC provider and SSO settings have admin operations, but the redirect that authenticates or links a person is a browser flow. An upstream MCP OAuth sign-in has its own callback-cookie and identity binding; see [upstream sign-in](toolsets/sign-in).
- **MCP exposure:** The read/catalogue and admin update operations exist. Exposure changes still require a real cookie session; discovering the generated update target does not let a bearer client enable itself. See [MCP exposure](toolsets/mcp-exposure).
- **Speech:** Provider/configuration and audio operations exist. Recording, reviewing a transcribed draft and playing a reply are human-facing edge transforms; agents stay text in and text out. See [speech and voice](providers/speech).
- **Services:** Service management, publishing and native asset/function operations exist. The planned separate curated publish_service tool and browser Primer.tool API are not implemented. See [published services](workspaces/services).
- **Event subscriptions:** Admin event-log subscription operations exist; an event-log sink subscription is different from a trigger subscription that starts work. See [triggers](triggers).
- **Workers and setup:** Observations and admin drain, purge, seed and reset operations exist. A reset is a deliberate mutation, not a health check. See [process operations](sessions/operations).
- **Deciding a gate:** A person decides whether to approve or reject, or answers an ask_user prompt. The authorized response routes enforce workspace access, nonce and approver rules. An agent cannot approve its own gate, and a peer message or queued steer is never an answer. Generated response targets do not grant an additional responder identity.
- **Secrets:** Provider, toolset and template secret fields are write-only. Reads mask them; sending a mask back preserves the stored secret. No operation turns masked credentials back into plaintext.
- **Channel listener activation after tool writes:** Saving channel/provider rows through system tools does not refresh or warm the API process's live adapter. An owner saves through the Channels console or its existing REST update, or an operator restarts the API process that owns inbound. Verify an actual message and answer; see [onboard a channel](runbooks/onboard-a-channel).

How to ask a person: describe the intended change, name the existing console or browser step, and wait for its actual outcome. Do not represent a precondition or a collected test as completed execution.
