---
title: "A graph that asks a person"
summary: "Park a graph on ask_user, parse its actual answer, and route approval, rejection or timeout."
order: 6
related: [graphs/create-and-edit, human-in-the-loop/ask-user, approvals/policies]
---

## What it is

A graph can ask a person in the middle of a run. The question is a yielding tool_call node; answering it resumes that node and routes on its structured result. A peer message is not the answer to the parked question.

## Concepts

`system__ask_user` returns an object with `response`; on timeout it returns a timeout object. The tool_call node's `output_schema` fills `parsed`. Without that schema a json_path router sees no parsed answer and falls through to its default branch, even when the person approved. The timeout-compatible schema below does not require `response`, so a timeout safely takes the rejected branch.

## How to

1. Store the graph below, with real planner and deployer agents available to its execution principal. The create is approval-gated separately from the question the graph later asks.
2. Run it in a workspace session, observe the actual pending question, and answer through the existing authorized question surface. The run keeps status running with parked_status set while it waits; the pending question is the evidence of a park.
3. Verify approve runs deployer and ends at end_ok; reject or timeout goes to end_no without deploying. A question is a person's decision, not an agent approving its own tool gate.

## Tool calls

### Create the human-routed deployment graph

```json
{
  "toolset_id": "crud",
  "tool_name": "create_graph",
  "arguments": {
    "entity": {
      "id": "deploy-with-approval",
      "description": "Plan a deploy, ask a human to approve, then act on the answer.",
      "nodes": [
        {
          "id": "start",
          "kind": "begin"
        },
        {
          "id": "plan",
          "kind": "agent",
          "agent_id": "deploy-planner",
          "input_template": "Draft the deploy plan for: {{ initial_input.change }}"
        },
        {
          "id": "confirm",
          "kind": "tool_call",
          "tool_id": "system__ask_user",
          "arguments": {
            "prompt": "Approve this deploy plan? Reply approve or reject.\n\n{{ nodes.plan.text }}",
            "response_schema": {
              "type": "string",
              "enum": [
                "approve",
                "reject"
              ]
            }
          },
          "output_schema": {
            "type": "object",
            "properties": {
              "response": {
                "type": "string"
              }
            }
          }
        },
        {
          "id": "do_deploy",
          "kind": "agent",
          "agent_id": "deployer",
          "input_template": "Execute the approved plan:\n{{ nodes.plan.text }}"
        },
        {
          "id": "end_ok",
          "kind": "end",
          "output_template": "deployed: {{ nodes.do_deploy.text }}"
        },
        {
          "id": "end_no",
          "kind": "end",
          "output_template": "rejected by operator"
        }
      ],
      "edges": [
        {
          "kind": "static",
          "from_node": "start",
          "to_node": "plan"
        },
        {
          "kind": "static",
          "from_node": "plan",
          "to_node": "confirm"
        },
        {
          "kind": "conditional",
          "from_node": "confirm",
          "router": {
            "kind": "json_path",
            "branches": [
              {
                "conditions": [
                  {
                    "path": "response",
                    "op": "eq",
                    "value": "approve"
                  }
                ],
                "to_node": "do_deploy"
              }
            ],
            "default_to": "end_no"
          }
        },
        {
          "kind": "static",
          "from_node": "do_deploy",
          "to_node": "end_ok"
        }
      ]
    }
  }
}
```

Reference: [`crud__create_graph`](reference/tools/crud/create_graph), [`system__ask_user`](reference/tools/system/ask_user).

## Gotchas

- Route on the parsed `response` field, not an empty path or unparsed text.
- Preserve the default rejection branch for a timeout or an unexpected answer.
- A required tool approval and ask_user are different parked operations; settling one does not settle the other.

## Related

[Create and edit a graph](graphs/create-and-edit), [ask a person](human-in-the-loop/ask-user), [approval policies](approvals/policies).
