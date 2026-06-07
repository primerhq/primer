---
slug: tool-approval
title: Tool approval
section: features
summary: Create and manage approval policies in the console -- required, Rego policy, or LLM-judge gates on specific tools, with a live queue for pending decisions.
---

## Overview

The Approvals page has two tabs: **Pending** and **Policies**. Policies define when a tool call must stop and wait for a decision. Pending shows every parked tool call waiting for your response right now. The page polls automatically every five seconds.

Tools with no policy row dispatch immediately. Adding a policy gates every matching `(toolset, tool)` call through the approval flow.

## Creating a policy

1. Open **Approvals** in the left nav and click the **Policies** tab.
2. Click **New policy** (top-right of the tab bar).
3. In the modal, pick an approval type:

   | Type | Behavior |
   |---|---|
   | Required | Every call parks and waits for a manual decision. |
   | Policy (Rego) | A Rego rule runs against the call; `required = true` triggers a hold. |
   | LLM judge | A configured LLM provider evaluates a judge prompt; the model decides. |

4. Enter a unique **id** (for example, `approve-stripe-refund`).
5. Pick the **toolset** from the dropdown or type a custom toolset id.
6. Enter the **tool name** (for example, `fs.delete`).
7. Optionally set a **timeout** in seconds. If omitted, the global yield cap applies.
8. For **Policy (Rego)**, paste your Rego into the editor. The policy must define a `required` boolean in the `primer.approval` package. A starter template is pre-filled.
9. For **LLM judge**, select a provider and model (from those already configured under LLM Providers), then write the judge prompt.
10. Click **Create policy**. The new row appears in the Policies table with the toggle enabled.

## Editing or disabling a policy

In the Policies table each row has an **edit** (pencil) button and a **delete** (trash) button. Click edit to reopen the modal with all fields pre-filled. The **id** field is locked after creation; every other field is editable. Use the **Enabled** toggle in the row to pause a policy without deleting it.

```callout:warning
Deleting a policy that currently has parked sessions does not auto-resolve those sessions. The parked calls stay parked until you decide them manually, then the session continues.
```

## Responding to a pending approval

When a tool call hits a `required` gate it parks the session and the call appears on the **Pending** tab. Each row shows:

- The tool name and toolset.
- Which session or chat it came from (click the link to jump to the detail view).
- How long the call has been parked and, if a timeout is set, how much time remains.
- The call arguments so you can review what the tool would do.

To decide:

1. Click **Approve** to release the call. The session resumes and the tool dispatches.
2. Click **Reject** to open the reason field. Type a rejection reason (required) and click **Send rejection**. The agent receives a clean error and can decide how to proceed.

You can also respond from inside the session or chat detail view. When a session is parked on an approval, an amber banner appears at the top of the transcript with the same Approve and Reject controls.

```callout:danger
Rejecting a tool call is not a retry. The agent receives an error message. If the agent prompt does not anticipate a rejection it may stall or end unexpectedly. Test the reject path in a development session before enabling a required policy in production.
```

## Automate this

```ref:reference/api-tool-approval
Full schema for approval policies and the respond endpoint, including the Rego policy shape and LLM judge fields.
```

## See also

```ref:concepts/tool-approval
How the gate mechanic works under the hood, including how policy and LLM-judge types interact with the session's parked state.
```
