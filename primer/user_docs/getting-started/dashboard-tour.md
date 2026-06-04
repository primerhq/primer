---
slug: dashboard-tour
title: Dashboard tour
section: getting-started
summary: What every panel on the primer dashboard means and what to act on.
---

## The top bar

The top bar is global; it appears on every console page. From left
to right: the brand mark and host, the Cmd+K search box, the worker
status badge, optional toggles, and the user avatar.

```mockup:topbar
{ "workers": "6/8", "inFlight": "3 in flight", "showIcBell": true }
```

The IC bell (amber) appears only when at least one chat or session
is parked waiting for operator approval. Click it to jump to the
approvals queue.

## The four counters

The dashboard's hero row shows four real-time counters: workers,
sessions, chats, and channels. Each counter polls the corresponding
list endpoint every five seconds. The number is the total; the
sub-text is a recent-activity hint.

```callout:info
Counters update independently. A momentarily zero count for one
panel does not mean the subsystem is broken; it just means the
poll happened to land between writes. Refresh after a few seconds
to confirm.
```

## The activity feed

Below the counters is a chronological list of the latest activity
across sessions and chats. Each row links to the corresponding
detail page.

## The empty state

On a freshly bootstrapped instance, sessions, chats, and triggers
all start at zero. The sessions panel surfaces this as:

```mockup:sessions-list-empty
{ "emptyLine": "Nothing has run yet", "ctaLabel": "New session" }
```

Hit New session to open the agent picker.

## Where to next

The next stop depends on what you came to do:

```ref:getting-started/first-agent
Build your first agent (5 minute speedrun).
```

```ref:getting-started/configuration
Configure storage, auth, and observability.
```
