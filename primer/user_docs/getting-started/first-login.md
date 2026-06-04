---
slug: first-login
title: First login
section: getting-started
summary: What the console shows on your first visit and how to sign in.
---

## The top bar

On a fresh install the top bar carries the brand mark on the left,
a global search on the right, and a worker-status badge that turns
green once the worker pool is ready.

```mockup:topbar
{ "workers": "0/4", "inFlight": "0 in flight", "showThemeToggle": true }
```

## Auth

Primer ships an in-process session cookie backed by an HMAC secret.
On first login you set an admin password; subsequent visits read the
secret-signed cookie.

```callout:info
Auth secrets are generated on first start and persisted under
$PRIMER_DATA/secrets/session.key. Back this file up if you want
sticky sessions across redeploys.
```

## The empty session list

Right after install no sessions exist yet. Hitting the Sessions
entry in the sidebar lands on this empty state:

```mockup:sessions-list-empty
{ "emptyLine": "No sessions yet", "ctaLabel": "New session" }
```

The blue New session button opens the agent picker.

## Next steps

Browse the Features section in the left nav to learn how to wire
agents, channels, and triggers.
