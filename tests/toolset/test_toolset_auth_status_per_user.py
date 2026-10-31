"""``system__get_toolset_auth_status`` reports the run's person's sign-in (access-control plan P5 Task 3).

A browser sign-in is a person's own: the tool answers the status of the run's person, never another person's. A run whose person may not use the
toolset gets the answer a missing toolset gets; a person whose only access is a public tool of it may read it (they sign in to use that tool).
"""

from __future__ import annotations

from primer.access.person import Person
from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.provider import Toolset
from primer.model.yield_ import ToolContext
from tests.toolset.oauth._service_helpers import make_toolset, target_of, token_row
from tests.toolset.test_system_toolset_auth_status import URL, _call, world  # noqa: F401


def _ctx(user_id: str) -> ToolContext:
    return ToolContext(tool_call_id="tc", session_id="s", workspace_id="w",
                       initiated_by=PrincipalRef(type="user", id=user_id, display=user_id, role="user", source="local", user_id=user_id))


async def test_status_tool_reports_the_runs_person(world) -> None:  # noqa: F811
    system, storage_provider, oauth = world
    row = make_toolset(URL, oauth={})
    row.access = Access(owners=["u-owner"], grants={"u-bob": "read"}, public_tools=["echo"])
    await storage_provider.get_storage(Toolset).create(row)
    await oauth.store.replace_tokens(token_row(oauth.store, target_of(row, Person("u-owner", "user"))))

    answers = {who: await _call(system, "get_toolset_auth_status", {"toolset_id": "kb"}, ctx=_ctx(f"u-{who}")) for who in ("owner", "bob", "carol")}

    assert {who: (a["state"], a["per_user"]) for who, (_, a) in answers.items()} == {
        "owner": ("connected", True), "bob": ("needs_sign_in", True), "carol": ("needs_sign_in", True),
    }, "each person reads their own sign-in; carol may use the public tool"


async def test_a_run_that_may_not_use_the_toolset_gets_the_missing_answer(world) -> None:  # noqa: F811
    system, storage_provider, _ = world
    row = make_toolset(URL, oauth={})
    row.access = Access(owners=["u-owner"])
    await storage_provider.get_storage(Toolset).create(row)

    result, answer = await _call(system, "get_toolset_auth_status", {"toolset_id": "kb"}, ctx=_ctx("u-stranger"))
    missing, missing_answer = await _call(system, "get_toolset_auth_status", {"toolset_id": "nope"}, ctx=_ctx("u-stranger"))

    assert result.is_error and missing.is_error
    assert answer == {**missing_answer, "message": missing_answer["message"].replace("nope", "kb")}
