---
title: "Harnesses"
summary: "Install bundles of agents, graphs and documents from a git repository."
order: 130
---

A harness is a git repository of agents, graphs, collections and toolsets that primer installs as a group and keeps in sync. Anyone can list harnesses; changing them needs an admin. Start with [What a harness is](harnesses/what-a-harness-is).

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`harnesses__build_harness`](reference/tools/harnesses/build_harness) | Enqueue BUILD for an outbound harness (202). | user |
| [`harnesses__create_harness`](reference/tools/harnesses/create_harness) | Create a new harness (DRAFT). | user |
| [`harnesses__delete_harness`](reference/tools/harnesses/delete_harness) | Enqueue UNINSTALL for a harness (202). | user |
| [`harnesses__download_harness_bundle`](reference/tools/harnesses/download_harness_bundle) | Download an outbound harness bundle as a gzipped tarball. | user |
| [`harnesses__get_access`](reference/tools/harnesses/get_access) | Who Harness is shared with. | user |
| [`harnesses__get_harness`](reference/tools/harnesses/get_harness) | Get a harness by id. | user |
| [`harnesses__push_harness`](reference/tools/harnesses/push_harness) | Enqueue PUSH for an outbound harness (202). | user |
| [`harnesses__put_access`](reference/tools/harnesses/put_access) | Change who Harness is shared with (owners only). | user |
| [`harnesses__put_harness_overrides`](reference/tools/harnesses/put_harness_overrides) | Set harness overrides (validates against cached schema). | user |
| [`harnesses__put_tracked_entities`](reference/tools/harnesses/put_tracked_entities) | Replace tracked_entities on an outbound harness. | user |
