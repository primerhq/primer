"""The content the platform ships as prime's agents.md (pivot plan P4, task 22): the management-plane text.

``test_prime_agents_md.py`` pins the mechanics (seeded when absent, never overwritten, the reset rewrites it). This file pins
the text itself: short enough for every prime agent's context on every turn, the sections of the design in order, and every
tool id, wiki path and call the text shows valid against the live registry and the live args schemas.
"""

from __future__ import annotations

import json
import re

from jsonschema import Draft202012Validator

from primer.bootstrap.operator_defaults import OPERATOR_TOOLS
from primer.bootstrap.prime import shipped_prime_agents_md
from tests.docs._wiki import repo_wiki, tool_entries

TEXT = shipped_prime_agents_md().decode("utf-8")
SECTIONS = ["Taking a task", "The task ledger", 'Answering "how is X going?"', "Coordinating", "Finishing", "Rules", "Where to look"]


def _calls(text: str) -> list[dict]:
    """The six call blocks of the file, as parsed JSON: the five ``system__call_tool`` calls and the one direct call."""
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", text, flags=re.S)]


def _invalid_calls(text: str) -> list[str]:
    """The calls the file shows that the live registry or the live args schemas would refuse, named by tool and cause."""
    entries = {entry.scoped_id: entry for entry in tool_entries()}
    invalid: list[str] = []
    for call in _calls(text):
        scoped = call["toolset_id"] + "__" + call["tool_name"] if "toolset_id" in call else call["name"]
        entry = entries.get(scoped)
        if entry is None:
            invalid.append(f"{scoped}: not registered")
            continue
        errors = sorted(
            Draft202012Validator(entry.tool.args_schema).iter_errors(call.get("arguments", {})),
            key=lambda error: list(error.path),
        )
        invalid.extend(f"{scoped}: {list(error.path)} {error.message}" for error in errors)
        # the args schemas are open by default, so name an undeclared top-level argument the validator would let through
        declared = set(entry.tool.args_schema.get("properties", {}))
        invalid.extend(f"{scoped}: undeclared argument {name!r}" for name in sorted(set(call.get("arguments", {})) - declared))
    return invalid


def test_the_file_is_short_and_clean():
    assert len(TEXT.encode()) <= 6000 and chr(0x2014) not in TEXT       # it is in every prime agent's context on every turn


def test_it_has_the_sections_of_the_design_in_order():
    outside_code = re.sub(r"```.*?```", "", TEXT, flags=re.S)       # the ledger shape holds a "## Log" line of its own
    assert [h[3:] for h in outside_code.splitlines() if h.startswith("## ")] == SECTIONS


def test_every_tool_it_names_exists():
    live = {e.scoped_id for e in tool_entries()}
    named = set(re.findall(r"`([a-z_]+__[a-z0-9_]+)`", TEXT))
    assert named and sorted(named - live) == []


def test_every_call_it_shows_validates_against_the_live_schema():
    # the five call_tool blocks and the one direct block: resolve the entry (toolset_id + "__" + tool_name, or name),
    # run jsonschema.Draft202012Validator(entry.tool.args_schema) over "arguments"; no undeclared top-level argument
    assert _invalid_calls(TEXT) == [] and len(_calls(TEXT)) == 6


def test_every_wiki_path_it_names_resolves():
    paths = set(re.findall(r"`([a-z0-9-]+(?:/[a-z0-9-]+)+)`", TEXT))
    assert paths and sorted(p for p in paths if p not in repo_wiki().pages) == []


def test_the_rules_forbid_destructive_actions_without_asking():
    rules = TEXT.split("## Rules")[1].split("## Where to look")[0]
    assert "Never delete a workspace" in rules and "without asking the user first" in rules


def test_the_ledger_has_the_fields_the_design_names():
    ledger = TEXT.split("## The task ledger")[1].split("## Answering")[0]
    assert all(token in ledger for token in ("goal:", "workspace:", "status:", "sessions:", "## Log", "tasks/<slug>.md"))


def test_the_calls_it_makes_directly_are_in_the_operators_kit():
    direct = {c["name"] for c in _calls(TEXT) if "name" in c}
    assert all(n in OPERATOR_TOOLS or n.startswith("workspace__") for n in direct)
