"""The caution about admin-held templates names every privilege a user inherits by instantiating one (lead ruling 16 of plan 08,
from the review of #782).

The held-template rule stops a non-admin from REWRITING or deleting a template that holds admin-only settings. It does not stop
them creating a workspace FROM it, and the workspace pod runs as the template's service account, so the user's code holds that
account's token and permissions. An admin must therefore not put "host-privileged mounts, secrets or a service account with
real permissions" in a template ordinary users may instantiate. The sentence is written in the module that implements the
rule, in the developer doc, in the agent-usage doc and in the console's admin note; this pins that none of the four drifts
back to naming only the mounts and the secrets.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_SENTENCE = "host-privileged mounts, secrets or a service account with real permissions"
_SITES = (
    "primer/workspace/template_privilege.py",
    "docs/dev/subsystems/workspaces.md",
    "docs/agents/workspaces.md",
    "ui/components/workspaces/templates.jsx",
)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_every_site_of_the_caution_names_a_service_account_with_real_permissions():
    """Line breaks differ between a docstring, a markdown paragraph and a JS string, so whitespace is squashed."""
    for path in _SITES:
        assert _SENTENCE in _squash((ROOT / path).read_text("utf-8")), path


def test_no_site_still_warns_about_only_mounts_or_secrets():
    old = re.compile(r"host-privileged\s+mounts\s+or\s+secrets")
    for path in _SITES:
        assert not old.search((ROOT / path).read_text("utf-8")), path
