"""Event redaction + role-tiered reads (wiring plan P6 T14).

Storage deliberately unwraps SecretStr before persisting and CRUD
events carry the row's own dump, so /v1/events was shipping provider
keys and template env values shielded only by the admin gate. Now every
read is redacted (the admin's included) and non-admins get a
workspace-scoped feed of safe lifecycle kinds only.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.auth.passwords import hash_password
from primer.events.redaction import (
    MASK,
    redact_payload,
    secret_field_names,
)
from primer.model.user import User
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401


async def _login_admin(client) -> None:
    reg = await client.post(
        "/v1/auth/register",
        json={"username": "redadmin", "password": "redadminpass1"},
    )
    assert reg.status_code == 200, reg.text


async def _login_as_user(client, app) -> None:
    storage = app.state.storage_provider.get_storage(User)
    await storage.create(User(
        id="user-red", username="redu",
        password_hash=await hash_password("redupass1"),
        created_at=datetime.now(timezone.utc), role="user",
    ))
    login = await client.post(
        "/v1/auth/login", json={"username": "redu", "password": "redupass1"},
    )
    assert login.status_code == 200, login.text


def test_redaction_unit() -> None:
    # The model registry sees SecretStr through containers: env is
    # dict[str, SecretStr] on WorkspaceTemplateOverrides.
    names = secret_field_names()
    assert {"api_key", "env", "bot_token", "git_token"} <= names

    out = redact_payload({
        "id": "pv-1",
        "api_key": "sk-live-123",
        "env": {"GH_TOKEN": "gho_abc", "REGION": "eu"},
        "nested": {"signing_secret": "shh", "note": "keep"},
        "custom_dsn": "postgres://user:pw@db/x",
        "unset_token": None,
        "kinds": ["a", "b"],
    })
    assert out["id"] == "pv-1"
    assert out["api_key"] == MASK
    # dict-valued secrets keep their keys, lose their values.
    assert out["env"] == {"GH_TOKEN": MASK, "REGION": MASK}
    assert out["nested"] == {"signing_secret": MASK, "note": "keep"}
    # Key-name defense catches names no model declares.
    assert out["custom_dsn"] == MASK
    # "unset" is not a secret; masking it would fake one.
    assert out["unset_token"] is None
    assert out["kinds"] == ["a", "b"]


@pytest.mark.asyncio
async def test_admin_reads_are_redacted(client, app) -> None:
    await _login_admin(client)
    store = app.state.storage_provider.get_event_store()
    base = await store.max_id()
    await store.append(
        event_type="llm_provider.created", entity_kind="llm_provider",
        entity_id="pv-red",
        payload={"id": "pv-red", "api_key": "sk-live-42", "kind": "anthropic"},
    )
    resp = await client.get(f"/v1/events?after_id={base}")
    assert resp.status_code == 200, resp.text
    (item,) = resp.json()["items"]
    assert item["payload"]["api_key"] == MASK
    assert item["payload"]["kind"] == "anthropic"


@pytest.mark.asyncio
async def test_user_reads_are_scoped_and_allowlisted(client, app) -> None:
    await _login_admin(client)
    store = app.state.storage_provider.get_event_store()
    base = await store.max_id()
    await store.append(
        event_type="agent.created", entity_kind="agent", entity_id="a-red",
        workspace_id="ws-red",
        payload={"id": "a-red", "prompt": "internal"},
    )
    await store.append(
        event_type="session.started", entity_kind="session",
        entity_id="s-red", workspace_id="ws-red",
        payload={"session_id": "s-red"},
    )

    await _login_as_user(client, app)
    # The platform log is not theirs: a workspace filter is mandatory.
    bare = await client.get(f"/v1/events?after_id={base}")
    assert bare.status_code in (400, 422), bare.text

    scoped = await client.get(
        f"/v1/events?after_id={base}&workspace_id=ws-red"
    )
    assert scoped.status_code == 200, scoped.text
    types = [e["event_type"] for e in scoped.json()["items"]]
    # Entity CRUD (whose payloads mirror stored rows) never reaches a
    # non-admin; the session lifecycle does.
    assert types == ["session.started"]


@pytest.mark.asyncio
async def test_restricted_role_is_rejected(client, app) -> None:
    await _login_admin(client)
    storage = app.state.storage_provider.get_storage(User)
    await storage.create(User(
        id="user-redr", username="redr",
        password_hash=await hash_password("redrpass1"),
        created_at=datetime.now(timezone.utc), role="restricted",
    ))
    login = await client.post(
        "/v1/auth/login", json={"username": "redr", "password": "redrpass1"},
    )
    assert login.status_code == 200, login.text
    resp = await client.get("/v1/events?workspace_id=ws-red")
    assert resp.status_code == 403, resp.text
