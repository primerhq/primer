# The operator-first revamp

Primer's `main` branch is mid-revamp. This page is the one-screen summary of
what is changing, what is stable today, and when the change completes.

## What is changing

Primer stops being a console over subsystems and becomes an agent you talk to.
The work is nine approved specs, landing in this order:

| Spec | Name | What it changes |
|---|---|---|
| S1 | Core session model | Sessions become agent-independent workstreams with switchable bindings, rewind, compaction and queued steers. Plain chat is removed. |
| S2 | Collections v2 | Knowledge becomes a wiki hierarchy of text documents, addressed by path, always greppable, with semantic search as a per-collection opt-in. |
| S4 | Provider platform | Speech-to-text and text-to-speech join the provider classes; one catalog replaces the per-class provider pages. |
| S3 | Client tools | Agents drive the UI through a new notifying tool class delivered to attached clients. |
| S6 | Triggers and channels | Channels fold into triggers; channel triggers map a platform thread to a session. |
| S7 | Observability | Per-worker metrics and a trace view derived from the on-disk session record. |
| S5 | Bootstrap and operator | A two-step first-run wizard, plus seeded operator and builder agents that navigate the platform for you. |
| S8 | Fresh shell | One IDE-style workspace shell replaces both the classic console and the studio2 trial. |
| S9 | Cutover | Docs, website, packaging and the v2.0.0 release. |

## What is stable today

**v0.6.x is the last stable pre-revamp release.** If you want a version whose
behaviour matches the published documentation, pin v0.6.x.

There are no transition releases between v0.6.x and v2.0.0. The revamp lands as
one branch and one pull request, so v0.6.x stays the newest tag for the whole
programme and the next tag is v2.0.0. What changes in between changes all at
once: plain chat disappears, the console is replaced, and collections change
shape.

## What happens at the end

The revamp completes at **v2.0.0**. At that tag the transition banner comes
off, the documentation and the website describe the agent-first product, and
the `primectl` CLI is gone (the REST API and the MCP server are the supported
programmatic surfaces).

## Compatibility

The programme is a clean break. There is no migration code: v2 starts empty and
existing installs re-run the first-run wizard.
