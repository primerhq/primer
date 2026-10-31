"""Code text that agents and maintainers read states the Phase 2 rules, not the ones they replaced (access-control P2 gap 5).

Tool descriptions reach the model, so a tool that says it is refused outside prime, or finds only the caller's own workspace, misleads
it; field and function docs that describe the reach or rank rule mislead the next maintainer. The unused pieces of those rules (the
create-workspace refusal text, the destroy hook the delete's rank scan ran in) are gone.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import primer.toolset.workspaces as workspaces_toolset
from primer.api.registries.workspace_registry import WorkspaceRegistry
from primer.bootstrap.prime import ensure_main_operator_session
from primer.model.trigger import Subscription

ROOT = Path(__file__).resolve().parents[2]


def test_the_workspace_tools_do_not_claim_prime_only():
    source = inspect.getsource(workspaces_toolset)
    assert "outside the default workspace (prime)" not in source, "a tool still tells the model a session outside prime is refused or limited"
    assert not hasattr(workspaces_toolset, "REFUSE_CREATE_WORKSPACE")


def test_the_subscription_reach_field_says_it_is_no_longer_used():
    description = Subscription.model_fields["reach"].description or ""
    assert "may start or feed" not in description and "is refused (target_out_of_reach)" not in description
    assert "no longer" in description


def test_the_registry_destroy_has_no_rank_scan_hook():
    assert "before_teardown" not in inspect.signature(WorkspaceRegistry.destroy).parameters
    assert "rank scan" not in (WorkspaceRegistry.destroy.__doc__ or "")


def test_the_main_session_doc_and_the_events_route_name_the_access_rule():
    doc = ensure_main_operator_session.__doc__ or ""
    assert "may_drive" not in doc and "own_session" not in doc
    events = (ROOT / "primer" / "api" / "routers" / "events.py").read_text(encoding="utf-8")
    assert "Sessions are a shared read" not in events
