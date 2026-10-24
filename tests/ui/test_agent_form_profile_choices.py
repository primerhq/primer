"""A user-role account can create an agent from the console (audit G3-02).

``POST /v1/agents`` is user tier, but the agent form filled its required Model profile dropdown from the admin-only ``GET /v1/model_profiles``:
for a user-role account the list answered 403, the dropdown held only "No model profiles yet", Create stayed disabled for good, and the field
said "Could not load the model profiles: forbidden_role". The form now reads ``GET /v1/model_profiles/choices`` (user tier: id, description,
kind and context length) for every role, and says a failed load through the console's one reader of a refusal (``readRefusal``), so a code is
never printed.

The answers are the REAL routers' (``tests/_support/agent_form_server.py``: the choices router behind ``require_user``, the profile router behind
``require_admin`` and the agent router, as ``primer/api/_app_routes.py`` includes them), asked as a ``user`` account.
"""

from __future__ import annotations

from tests._support.agent_form_server import agent_form_server
from tests.ui._agent_form_harness import (
    PROFILE_CHOICES_KEY,
    PROFILE_ROWS,
    agent_form,
    body,
    calls,
    click,
    fail_resource,
    mount,
    options,
    prop,
    resource_request,
    set_resource,
    type_into,
)


def test_the_form_reads_the_profile_choices_route_for_every_role():
    with agent_form() as ctx:
        mount(ctx)
        assert resource_request(ctx, PROFILE_CHOICES_KEY) == [{"method": "GET", "path": "/model_profiles/choices?limit=200", "body": None}]
        assert calls(ctx) == [], "reading the fetcher's request leaves the form's own calls alone"


def test_a_user_role_account_creates_an_agent_on_a_profile_from_the_choices():
    with agent_form_server(profiles=PROFILE_ROWS) as server:
        refused_list, _ = server.request("GET", "/v1/model_profiles?limit=200", role="user")
        assert refused_list == 403, "the full profile list stays admin-only"
        status, page = server.request("GET", "/v1/model_profiles/choices?limit=200", role="user")
        assert status == 200, page

        with agent_form() as ctx:
            set_resource(ctx, PROFILE_CHOICES_KEY, {"data": page, "loading": False, "error": None})
            mount(ctx)
            assert options(ctx, "agent-model-profile") == [{"value": "fast", "text": "fast"}, {"value": "slow", "text": "slow"}]
            type_into(ctx, "agent-id", "refund-triage")
            type_into(ctx, "agent-description", "Triages refunds")
            assert prop(ctx, "agent-form-submit", "disabled") is False, "Create is enabled for a user-role account"
            click(ctx, "agent-form-submit")
            sent = body(ctx)

        assert sent["model"] == {"profile_id": "fast"}
        created, row = server.request("POST", "/v1/agents", sent, role="user")
        assert created == 201, row
        assert row["model"] == {"profile_id": "fast"}


def test_a_failed_profile_load_is_said_in_words_never_as_a_code():
    with agent_form_server(profiles=PROFILE_ROWS) as server:
        envelope = server.refusal("GET", "/v1/model_profiles?limit=200", role="user", status=403)
    assert envelope["extensions"]["error"] == "forbidden_role", envelope

    with agent_form() as ctx:
        fail_resource(ctx, PROFILE_CHOICES_KEY, envelope)
        mount(ctx)
        said = prop(ctx, "field:Model profile", "data-error")
        assert said == "Could not load the model profiles: Your role does not allow this."
        assert "forbidden_role" not in said
