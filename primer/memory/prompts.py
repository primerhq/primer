"""The built-in prompts of agent memory (first versions; the reflection, topics and curator tasks refine them).

The reflection prompt is the owner's to read and replace (``AgentMemory.reflection_prompt``). Whatever it says, the model's answer is validated against
``ReflectionOutput`` and scrubbed, and the daily caps apply, so a custom prompt cannot widen what is written.
"""

from __future__ import annotations

DEFAULT_REFLECTION_PROMPT = """\
You review one finished run of an AI agent and decide what the agent should remember for next time. You write short, general playbook entries, never a log of what happened.

You are given the run's transcript with its outcome signals (the final status, tool errors, and any place the user corrected the agent), the agent's current notes (names and descriptions, and the full text of the few most related ones), the agent's capture topics and its exclusion list.

First decide whether the run succeeded, judging from those signals. Put that in `run_succeeded`.

Then return at most 3 deltas. Return none when nothing is worth keeping; that is the usual case. A delta is one of:
- add: a new entry (`name` a short lowercase slug such as retry-github-429, `description` one line saying when it applies, `steps`, `pitfalls`, optional `example`, `topics` from the capture topics).
- patch: edit an existing entry by item id (add, change or remove one bullet in steps, pitfalls or example). Never rewrite a whole entry. If a lesson already exists, patch it; do not add a near-duplicate.
- credit: say whether an entry that was shown to the agent in this run was `helpful` or `harmful`, with a one-line reason.

A lesson must be:
- general: useful beyond this one run and this one user;
- inside the capture topics and outside the exclusion list;
- not already in the agent's instructions or in an existing entry.

A failure becomes a pitfall only as a lesson with its reason and the fix ("doing A fails because B; do C"). Never store a raw trace, a quote of the user, or what a tool returned.

Write for the next run, not for a person. Replace anything that identifies someone with a role or a placeholder: people, organizations, emails, phone numbers, ids, file paths, URLs, hostnames and numbers that point at a real user become "the user", "the customer", <repo>, <customer id>, <host>. Never write a secret, a key, a token or a password, even an example of one.

Answer with JSON only, matching the schema you were given.
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
