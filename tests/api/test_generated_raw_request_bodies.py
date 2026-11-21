"""Generated tools must reach the same raw-body handlers and guards as REST."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json

import pytest

from primer.access.acting import acting_as
from primer.access.person import Person
from primer.model.harness import Harness
from primer.model.user import User
from primer.model.webhook_delivery import WebhookDelivery
from tests.api.test_svc_gateway import _publish_fn_app
from tests.api.test_webhook_endpoint import _create_webhook_trigger, _make_hmac_sig


async def _call(app, scoped_id, arguments, *, role="admin"):
    users = app.state.storage_provider.get_storage(User)
    uid = f"raw-body-{role}"
    if await users.get(uid) is None:
        await users.create(User(
            id=uid, username=uid, password_hash="!test", role=role,
            created_at=datetime(2026, 11, 21, tzinfo=timezone.utc),
        ))
    toolset, tool = scoped_id.split("__", 1)
    with acting_as(Person(uid, role)):
        return await app.state.generated_route_toolsets.providers[toolset].call(
            tool_name=tool, arguments=arguments,
        )


def _output(result, status=None):
    payload = json.loads(result.output)
    if status is None:
        assert not result.is_error, payload
    else:
        assert result.is_error, payload
        assert payload["status"] == status, payload
    return payload


async def _harness(sp):
    row = Harness(
        id="hns_raw", slug="raw", name="Raw inputs", git_url="https://example.com/unused.git",
        created_at=datetime(2026, 11, 21, tzinfo=timezone.utc),
        overrides_schema={"type": "object", "properties": {"count": {"type": "integer"}},
                          "required": ["count"], "additionalProperties": False},
    )
    await sp.get_storage(Harness).create(row)
    return row


async def test_generated_overrides_update_and_validation(app, fake_storage_provider):
    row = await _harness(fake_storage_provider)
    args = {"harness_id": row.id, "body": {"count": 7}}
    result = _output(await _call(app, "harnesses__put_harness_overrides", args))
    assert result["overrides"] == {"count": 7}
    assert result["overrides_hash"]
    store = fake_storage_provider.get_storage(Harness)
    before = (await store.get(row.id)).model_dump()
    for invalid in ({"count": "invalid"}, [1, 2], None):
        _output(await _call(app, "harnesses__put_harness_overrides", {**args, "body": invalid}), 422)
        assert (await store.get(row.id)).model_dump() == before
    _output(await _call(app, "harnesses__put_harness_overrides", {**args, "body": []}, role="user"), 403)
    _output(await _call(app, "harnesses__put_harness_overrides", {**args, "harness_id": "absent", "body": []}), 404)
    assert (await store.get(row.id)).model_dump() == before


async def test_generated_function_arguments_execute_and_validate(app, client, raw_client):
    await _publish_fn_app(client, "generated-function")
    args = {"name": "generated-function", "fn": "add", "body": {"a": 19, "b": 23}}
    assert _output(await _call(app, "svc__svc_gateway_function", args)) == 42
    _output(await _call(app, "svc__svc_gateway_function", {**args, "body": {"a": "bad", "b": 23}}), 422)
    _output(await _call(app, "svc__svc_gateway_function", {**args, "body": []}), 422)
    _output(await _call(app, "svc__svc_gateway_function", {**args, "fn": "absent"}), 404)
    # Authentication still precedes body parsing for a console-gated service.
    denied = await raw_client.post("/svc/generated-function/_gateway/functions/add", content=b"not json")
    assert denied.status_code == 401


async def test_generated_webhook_preserves_signed_bytes_and_query(app, fake_storage_provider):
    secret = "test-secret"
    trigger = await _create_webhook_trigger(fake_storage_provider, slug="raw-signed", hmac_secret=secret)
    body = b'{ "message": "caf\\u00e9", "n": 2 }\r\n'
    args = {"token": trigger.config.token, "body": base64.b64encode(body).decode(),
            "signature": _make_hmac_sig(secret, body),
            "query": {"event": "a&b=c", "space": "two words", "unicode": "caf\u00e9"}}
    payload = _output(await _call(app, "webhooks__receive_webhook", args))
    row = await fake_storage_provider.get_storage(WebhookDelivery).get(payload["delivery_id"])
    assert row is not None
    assert row.extra_context["webhook_body"] == body.decode()
    assert row.extra_context["webhook_query"] == args["query"]
    assert "x-primer-signature" not in row.extra_context["webhook_headers"]
    assert row.extra_context["webhook_method"] == "POST"
    for invalid in ({"signature": "sha256=bad"}, {"body": base64.b64encode(b"{}").decode()}):
        _output(await _call(app, "webhooks__receive_webhook", {**args, **invalid}), 401)
    unsigned = {key: value for key, value in args.items() if key != "signature"}
    _output(await _call(app, "webhooks__receive_webhook", unsigned), 401)
    # Rejected signatures must not enqueue or persist a delivery.
    assert len(fake_storage_provider.get_storage(WebhookDelivery)._data) == 1


async def test_generated_webhook_unsigned_binary_and_limits(app, fake_storage_provider):
    from primer.model.except_ import ConfigError

    trigger = await _create_webhook_trigger(fake_storage_provider, slug="raw-binary")
    body = bytes(range(256))
    args = {"token": trigger.config.token, "body": base64.b64encode(body).decode()}
    payload = _output(await _call(app, "webhooks__receive_webhook", args))
    row = await fake_storage_provider.get_storage(WebhookDelivery).get(payload["delivery_id"])
    assert row.extra_context["webhook_body"] == body.decode(errors="replace")
    _output(await _call(app, "webhooks__receive_webhook", {**args, "body": base64.b64encode(b"x" * (1024 * 1024 + 1)).decode()}), 413)
    _output(await _call(app, "webhooks__receive_webhook", {**args, "token": "absent"}), 404)
    disabled = await _create_webhook_trigger(fake_storage_provider, slug="raw-disabled", enabled=False)
    _output(await _call(app, "webhooks__receive_webhook", {**args, "token": disabled.config.token}), 403)
    with pytest.raises(ConfigError, match="base64"):
        await _call(app, "webhooks__receive_webhook", {**args, "body": "invalid!"})
    assert _output(await _call(app, "webhooks__receive_webhook", {"token": trigger.config.token}))["status"] == "accepted"


def test_generated_raw_body_schemas(app):
    operations = {f"{op.tool.toolset_id}__{op.tool.id}": op.tool for op in app.state.generated_route_toolsets.operations}
    for scoped in ("harnesses__put_harness_overrides", "svc__svc_gateway_function"):
        schema = operations[scoped].args_schema
        assert "body" in schema["required"]
        assert schema["properties"]["body"]["type"] == "object"
        assert schema["properties"]["body"]["additionalProperties"] is True
    assert operations["harnesses__put_harness_overrides"].required_role == "admin"
    schema = operations["webhooks__receive_webhook"].args_schema
    assert schema["properties"]["body"]["contentEncoding"] == "base64"
    assert "body" not in schema["required"]
    assert schema["properties"]["signature"]["type"] == "string"
    assert schema["properties"]["query"]["additionalProperties"] == {"type": "string"}


async def test_raw_body_tools_keep_default_approval_requirements(app, fake_storage_provider):
    from primer.agent.approval import ApprovalResolver
    from primer.bootstrap.seed import ensure_crud_approval_policies
    from primer.model.tool_approval import ToolApprovalPolicy

    await ensure_crud_approval_policies(fake_storage_provider)
    resolver = ApprovalResolver(fake_storage_provider.get_storage(ToolApprovalPolicy))
    for toolset, tool in (("harnesses", "put_harness_overrides"), ("svc", "svc_gateway_function"), ("webhooks", "receive_webhook")):
        policy = await resolver.find(toolset_id=toolset, tool_name=tool)
        assert policy is not None and policy.approval.type.value == "required"


async def test_generated_non_ascii_signature_preserves_hmac_refusal_and_guard_order(app, fake_storage_provider):
    trigger = await _create_webhook_trigger(fake_storage_provider, slug="raw-non-ascii", hmac_secret="test-secret")
    args = {"token": trigger.config.token, "body": base64.b64encode(b"{}").decode()}
    for signature in ("sha256=" + "\xff" * 3, "sha256=\u2603", "\ud800"):
        refused = _output(await _call(app, "webhooks__receive_webhook", {**args, "signature": signature}), 401)
        assert refused["extensions"]["code"] == "hmac_mismatch"
    # Malformed signature text must not short-circuit the earlier REST guards.
    malformed = {**args, "signature": "sha256=\xff"}
    _output(await _call(app, "webhooks__receive_webhook", {**malformed, "token": "absent"}), 404)
    disabled = await _create_webhook_trigger(fake_storage_provider, slug="raw-non-ascii-disabled", enabled=False, hmac_secret="test-secret")
    _output(await _call(app, "webhooks__receive_webhook", {**malformed, "token": disabled.config.token}), 403)
    oversized = base64.b64encode(b"x" * (1024 * 1024 + 1)).decode()
    _output(await _call(app, "webhooks__receive_webhook", {**malformed, "body": oversized}), 413)
    assert fake_storage_provider.get_storage(WebhookDelivery)._data == {}
    # The same path still accepts an unchanged ordinary ASCII signature.
    accepted = _output(await _call(app, "webhooks__receive_webhook", {**args, "signature": _make_hmac_sig("test-secret", b"{}")}))
    assert accepted["status"] == "accepted"
    assert len(fake_storage_provider.get_storage(WebhookDelivery)._data) == 1
