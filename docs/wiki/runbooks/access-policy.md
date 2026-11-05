---
title: Change the access policy on an entity
summary: Who an entity is shared with; owners, grants and public, applied in one verifiable write and proven with a members diff.
audience: admin
tools: [system__get_entity_access, system__set_entity_access, system__lookup_users]
related: [purpose-map, start-here/roles-and-approvals, runbooks/conventions]
---

## Goal
Change who an entity is shared with (its owners and grants, and whether it is public), in one verifiable write.

## Preconditions
- the entity is named: its kind and id.
  - check: `system__get_entity_access` ([reference](reference/tools/system/get_entity_access)) on the entity -> expected: `{owners, grants, public, public_tools, rev, your_level}`; the read returns `your_level`, the caller's own access level.
- a wrong grant is the kind of thing a dictated flow must double-check: state the change in user words and confirm with `system__ask_user` before the write when the request is ambiguous (who, which level, which entity).
- the grantee ids exist as users.
  - check: `system__lookup_users` ([reference](reference/tools/system/lookup_users)) with a username prefix for each new grantee -> expected: the user id. A grant names a user id, not a username; only full, enabled users are grantable.

## Roles and gates
- runner: `admin` - the write is owners-only: `system__set_entity_access` answers type=forbidden for any non-owner caller. Sharing a model profile or a provider is USE-sharing: owners stays empty, every grant is read, and only admins change it.
- gated steps:
  - step 2: `system__set_entity_access` - shipped gate: none (the access model itself is the gate: owners only); approver: none; card shows: n/a.

## Steps
### 1. Read the current access
Call `system__get_entity_access` ([reference](reference/tools/system/get_entity_access)):

```json
{"toolset_id": "system", "tool_name": "get_entity_access", "arguments": {"kind": "agent", "id": "agent-3f9a1c8d"}}
```

Expected: the current `{owners, grants, public, public_tools, rev}`. Keep the `rev`: the write is fenced on it.

### 2. State the change, then apply it
State the whole desired state in user words (who owns it, who gets read, who gets full, is it public), confirm when ambiguous, and call `system__set_entity_access` ([reference](reference/tools/system/set_entity_access)) with the WHOLE desired state and the `rev` just read:

```json
{"toolset_id": "system", "tool_name": "set_entity_access", "arguments": {"kind": "agent", "id": "agent-3f9a1c8d", "owners": ["user-1"], "grants": {"user-2": "read"}, "public": false, "rev": 0}}
```

Expected: the new sharing, exactly as `get_entity_access` returns it. A stale `rev` answers type=conflict.

### 3. What the levels mean (for the user, plainly)
`read` lets a person view the entity and read it; `full` lets them write it and drive it (for a workspace: read = view its files and sessions; full = write its files and drive its sessions). `public` opens the entity to every user at the level the `public_tools`/sharing rules name.

## Verification
- Re-read with `system__get_entity_access` -> expected: the owners, grants, public and public_tools exactly as stated in step 2, and a bumped `rev`.
- The members diff: compare the `members` the read reports (the owners and grantees, resolved) against the change that was stated - no grantee gained or lost a level that was not part of the change.
- Where the flow names a specific person ("give Alice read"), the verification names the resolution: `system__lookup_users` on the username, then the re-read showing that user id at the stated level.

## Refusal and failure
- type=forbidden -> the caller is not an owner (or a model profile / provider, which only admins change); say who may run this and stop.
- type=conflict -> a stale `rev`: someone edited the sharing between the read and the write; re-read (step 1) and re-state the change against the new state.
- type=validation-error -> the named field: a grantee that is not a full enabled user, an empty owners list, or a level that is not read/full.
- type=not-found -> the entity id or kind is wrong; the read that shows it is the step-1 call.

## Related
- [start-here/roles-and-approvals](start-here/roles-and-approvals): the access model the flow applies.
- [runbooks/approval-policy](runbooks/approval-policy): the other policy surface (who may decide a gated tool call).
