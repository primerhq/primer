"""Compatibility of the new priority fields on durable, pre-cutover records."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from primer.model.agent import Agent
from primer.model.graph import Graph
from primer.model.session_creation import SessionCreationIntent
from primer.model.workspace_session import WorkspaceSession


@pytest.mark.parametrize(("model", "field", "legacy"), [
    (Agent, "default_priority", {"id": "a", "description": "agent", "model": {"profile_id": "p--m"}}),
    (Graph, "default_priority", {"id": "g", "description": "graph"}),
    (WorkspaceSession, "priority", {
        "id": "s", "workspace_id": "w", "created_at": datetime.now(UTC), "status": "created",
        "binding": {"kind": "agent", "agent_id": "a"},
    }),
    (SessionCreationIntent, "priority", {"id": "s", "workspace_id": "w", "state": "committed"}),
])
def test_legacy_priority_defaults_to_zero_and_explicit_values_are_strict(model, field, legacy):
    assert getattr(model.model_validate(legacy), field) == 0
    assert getattr(model.model_validate({**legacy, field: 1_000_000}), field) == 1_000_000
    for value in (None, False, True, -1, 1.0, "7", 1_000_001):
        with pytest.raises(ValidationError):
            model.model_validate({**legacy, field: value})
