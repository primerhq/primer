"""The built-in prompts of agent memory (first versions; the reflection, topics and curator tasks refine them).

The reflection prompt is the owner's to read and replace (``AgentMemory.reflection_prompt``). Whatever it says, the model's answer is validated against
``ReflectionOutput`` and scrubbed, and the daily caps apply, so a custom prompt cannot widen what is written.
"""

from __future__ import annotations

DEFAULT_REFLECTION_PROMPT = """\
You keep a playbook for an AI agent: short, reusable notes about how to do its kind of work well. You are given the transcript of one session, how it
ended, the agent's existing notes (an index, and the full text of the ones most related to this session), the topics worth capturing and the topics
never to capture.

Decide whether the session teaches something that will help in a future, different session. Most sessions teach nothing: then return no deltas.

Rules:
- Return at most 3 deltas. Each is one of: add (a new note), patch (item-level edits to an existing note), credit (a verdict on an existing note
  that was used in this session).
- Capture generalized lessons that fall inside the capture topics. Never capture anything inside the do-not-capture list.
- Generalize: replace people, ids, emails, hostnames, paths and other specifics with placeholders such as <user>, <repo>, <path>. Never copy a
  secret, a credential or personal data.
- Distill failures into contrastive pitfalls: what looked right, what went wrong, and what to do instead.
- Prefer a patch over an add when an existing note covers the same ground. Patch single items (steps, pitfalls, example), never rewrite a note.
  A note name must not repeat an existing note.
- Credit an existing note only if it was used in this session: helpful if it led to a better result, harmful if it misled the agent.
- A note has a name, a one-line description, ordered steps, pitfalls, an optional example and topics.
- Set run_succeeded to true only if you judge that the session achieved what the user asked for.
Answer with the JSON object the schema describes and nothing else.
"""

DEFAULT_TOPICS_PROMPT = """\
You are given an AI agent's system prompt and the list of tools it can call. Say what this agent is for, so a later reviewer knows which lessons are worth keeping.

Return two lists:
- capture_topics: 3 to 10 short phrases, each a kind of task or area of knowledge the agent works on (for a support agent, for example: "diagnosing billing errors", "using the ticket API"). Base them on the prompt and the tools, not on guesses about users.
- do_not_capture: things never to store. Always include: personal data about anyone (names, emails, phone numbers, addresses, ids), credentials and secrets, the contents of user documents and messages, one-off facts about a single customer or a single run.

Answer with JSON only, matching the schema you were given.
"""

DEFAULT_MERGE_PROMPT = """\
You are given two playbook entries that look like near-duplicates. Decide whether they teach the same lesson.

If they do, return merged=true and the merged entry: keep the older entry's name, a single description saying when it applies, and the union of the steps and pitfalls with repeats removed and the wording tightened. Keep the entry short; do not add anything neither entry says.

If they differ in a way that matters (a different tool, a different cause, a different fix), return merged=false and leave both as they are.

Answer with JSON only, matching the schema you were given.
"""

__all__ = ["DEFAULT_MERGE_PROMPT", "DEFAULT_REFLECTION_PROMPT", "DEFAULT_TOPICS_PROMPT"]
