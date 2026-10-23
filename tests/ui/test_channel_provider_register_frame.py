"""Unit tests for Channel Providers in the register overlay frame (plan 02, Task 19).

Tests that:
- Edit keeps a stored secret left blank, for each platform: the row the real router serves, the form's PUT body, the real router's 200 (audit G2-01);
- Edit keeps every field the form does not show (PC_mergeKeep);
- Create still requires the required secrets;
- The form renders inside a frame with 'Register' and 'Save' labels;
- A hosted create calls onSaved and does not navigate;
- The detail page stacks kit sections;
- Detail edit opens PC_RegisterOverlay in edit mode;
- Detail delete calls onDeleted.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.common.url_userinfo import MASK
from primer.model.channel import ChannelProvider
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from tests.ui._register_harness import COMPONENTS, base_code, context, js, settle, transpiled

CHANNELS_FILE = COMPONENTS / "channels.jsx"

STUBS = r"""
var NAVIGATED = [], LAST_FRAME = null, SAVED = null, DELETED = null;
window.primerApi.useRouter = function () {
  return {
    navigate: function (to) { NAVIGATED.push(to); },
    params: {},
  };
};
window.primerApi.useMutation = function (fn, opts) {
  var loadingState = React.useState(false);
  return {
    mutate: function (arg) {
      loadingState[1](true);
      return fn(arg).then(function (res) {
        loadingState[1](false);
        if (opts && opts.onSuccess) opts.onSuccess(res);
        return res;
      }, function (err) {
        loadingState[1](false);
        if (opts && opts.onError) opts.onError(err);
        throw err;
      });
    },
    loading: loadingState[0],
  };
};
window.primerApi.resourceState = function (r) {
  if (!r) return "loading";
  if (r.data !== undefined) return "ready";
  if (r.error) return "stuck";
  return r.loading ? "loading" : "ready";
};
window.primerApi.extraInstalled = function () { return true; };

function frameFn(parts) {
  LAST_FRAME = parts;
  return el("div", { "data-testid": "framed-root" },
    el("div", { "data-testid": "frame-body" }, parts.body),
    el("div", { "data-testid": "frame-left" }, parts.footerLeft),
    el("div", { "data-testid": "frame-right" }, parts.footerRight),
    parts.error ? el("div", { "data-testid": "frame-error" }, parts.error) : null
  );
}
"""


@pytest.fixture(scope="module")
def code() -> str:
    return base_code(with_overlay=True) + "\n" + transpiled(CHANNELS_FILE)


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    try:
        yield c
    finally:
        c.close()


# Audit G2-01: the edit is driven from a row the REAL router serves (the tokens arrive as the served mask) and the body the form
# builds is PUT back to the real router on a real SQLite store. The earlier version of this test mounted a hand-written row whose
# "masked" secrets were valid tokens, which hid that every channel edit answered 422.
_STORED = {
    "slack": {"app_token": "xapp-1-A0123-live-app-token", "bot_token": "xoxb-0123-live-bot-token", "signing_secret": "live-signing-secret"},
    "telegram": {"bot_token": "123456:abcdefghijklmnopqrstuvwxyz123456", "poll_timeout_seconds": 25},
    "discord": {"bot_token": "D" * 40, "enable_dms": True},
}
# platform -> (JS that changes the one non-secret field the form shows, the change expected in storage); Slack shows only secrets
_CHANGE = {
    "slack": ("", {}),
    "telegram": (
        'ALL("frame-body").forEach(function (n) { if (n.type === "input" && n.props.type === "number") n.props.onChange({ target: { value: "40" } }); });',
        {"poll_timeout_seconds": 40},
    ),
    "discord": (
        'ALL("frame-body").forEach(function (n) { if (n.type === "input" && n.props.type === "checkbox") n.props.onChange({ target: { checked: false } }); });',
        {"enable_dms": False},
    ),
}
_SECRETS = {"slack": ("app_token", "bot_token", "signing_secret"), "telegram": ("bot_token",), "discord": ("bot_token",)}


@pytest_asyncio.fixture
async def real_store(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "channels.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest_asyncio.fixture
async def real_router(real_store: SqliteStorageProvider) -> AsyncIterator[httpx.AsyncClient]:
    registry = ProviderRegistry(
        real_store,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    app = create_test_app(storage_provider=real_store, provider_registry=registry)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
        await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", sorted(_STORED))
async def test_edit_keeps_a_stored_secret_left_blank(ctx, real_router, real_store, platform: str) -> None:
    rid = f"cp-{platform}-1"
    created = await real_router.post("/v1/channel_providers", json={"id": rid, "provider": platform, "config": _STORED[platform]})
    assert created.status_code == 201, created.text
    served = (await real_router.get(f"/v1/channel_providers/{rid}")).json()
    assert all(served["config"][name] == MASK for name in _SECRETS[platform]), served

    ctx.eval(f"CALLS = []; MR.mount(window.NewChannelProviderModal, {{ existing: {json.dumps(served)}, onClose: function () {{}}, frame: frameFn }});")
    ctx.eval(_CHANGE[platform][0] + " MR.rerender();")
    ctx.eval('ALL("frame-right").filter(function (n) { return n.type === "button"; })[0].props.onClick();')
    settle(ctx)
    puts = [c for c in js(ctx, "CALLS") if c["method"] == "PUT"]
    assert [c["path"] for c in puts] == [f"/channel_providers/{rid}"], puts
    body = puts[0]["body"]
    assert all(body["config"][name] == MASK for name in _SECRETS[platform]), "a secret left blank is sent as the served mask"

    r = await real_router.put(f"/v1/channel_providers/{rid}", json=body)

    assert r.status_code == 200, r.text
    config = (await real_store.get_storage(ChannelProvider).get(rid)).config
    assert {name: getattr(config, name).get_secret_value() for name in _SECRETS[platform]} == {name: _STORED[platform][name] for name in _SECRETS[platform]}
    assert {name: getattr(config, name) for name in _CHANGE[platform][1]} == _CHANGE[platform][1]


def test_clearing_the_optional_channel_number_restores_its_default(ctx) -> None:
    ctx.eval("""
    CALLS = [];
    var row = { id: "cp-telegram", provider: "telegram", config: { bot_token: "123456:abcdefghijklmnopqrstuvwxyz", poll_timeout_seconds: 25 } };
    MR.mount(window.NewChannelProviderModal, { existing: row, onClose: function () {}, frame: frameFn });
    ALL("frame-body").forEach(function (n) {
      if (n.type === "input" && n.props.type === "number") n.props.onChange({ target: { value: "" } });
    });
    MR.rerender();
    ALL("frame-right").filter(function (n) { return n.type === "button"; })[0].props.onClick();
    """)
    settle(ctx)
    call = [c for c in js(ctx, "CALLS") if c["method"] == "PUT"][0]
    assert call["body"]["config"]["poll_timeout_seconds"] == 25
    # The body must be one the server accepts: a null here is refused (an int with a default).
    assert ChannelProvider.model_validate(call["body"])


def test_edit_round_trips_every_unshown_field(ctx) -> None:
    ctx.eval("""
    CALLS = [];
    var row = {
      id: "cp-slack-extra",
      provider: "slack",
      config: {
        app_token: "xapp-123456",
        bot_token: "xoxb-123456",
        signing_secret: "sec-123456",
        unshown_field: "survives_edit",
        another_custom: 99
      }
    };
    MR.mount(window.NewChannelProviderModal, {
      existing: row,
      onClose: function () {},
      frame: frameFn
    });
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    btn.props.onClick();
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    put_call = [c for c in calls if c["method"] == "PUT" and "cp-slack-extra" in c["path"]][0]
    cfg = put_call["body"]["config"]
    assert cfg["unshown_field"] == "survives_edit"
    assert cfg["another_custom"] == 99


def test_create_still_requires_the_required_secrets(ctx) -> None:
    ctx.eval("""
    CALLS = [];
    MR.mount(window.NewChannelProviderModal, {
      onClose: function () {},
      frame: frameFn
    });
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    BTN_DISABLED = !!btn.props.disabled;
    """)
    assert js(ctx, "BTN_DISABLED") is True

    # Fill app_token and bot_token
    ctx.eval("""
    ALL("frame-body").forEach(function (n) {
      if (n.type === "input" && n.props.type === "password") {
        if (n.props.placeholder && n.props.placeholder.indexOf("xapp") >= 0) {
          n.props.onChange({ target: { value: "xapp-test-token" } });
        }
        if (n.props.placeholder && n.props.placeholder.indexOf("xoxb") >= 0) {
          n.props.onChange({ target: { value: "xoxb-test-token" } });
        }
      }
    });
    MR.rerender();
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    BTN_DISABLED_AFTER = !!btn.props.disabled;
    """)
    assert js(ctx, "BTN_DISABLED_AFTER") is False


def test_the_form_renders_inside_a_frame_with_register_and_save_labels(ctx) -> None:
    ctx.eval("""
    LAST_FRAME = null;
    MR.mount(window.NewChannelProviderModal, { onClose: function () {}, frame: frameFn });
    """)
    assert js(ctx, "textOf(LAST_FRAME.footerRight)") == "Register"
    assert js(ctx, "textOf(LAST_FRAME.footerLeft)") == "Cancel"
    assert js(ctx, "MR.find('framed-root') != null")

    ctx.eval("""
    LAST_FRAME = null;
    MR.mount(window.NewChannelProviderModal, {
      existing: { id: "cp-1", provider: "slack", config: { app_token: "xapp-123456", bot_token: "xoxb-123456" } },
      onClose: function () {},
      frame: frameFn
    });
    """)
    assert js(ctx, "textOf(LAST_FRAME.footerRight)") == "Save"


def test_a_hosted_create_calls_on_saved(ctx) -> None:
    ctx.eval("""
    SAVED = null;
    NAVIGATED = [];
    CALLS = [];
    MR.mount(window.NewChannelProviderModal, {
      onClose: function () {},
      onSaved: function (row) { SAVED = row; },
      frame: frameFn
    });
    ALL("frame-body").forEach(function (n) {
      if (n.type === "input" && n.props.placeholder === "auto-generated") {
        n.props.onChange({ target: { value: "cp-created" } });
      }
      if (n.type === "input" && n.props.type === "password") {
        if (n.props.placeholder && n.props.placeholder.indexOf("xapp") >= 0) {
          n.props.onChange({ target: { value: "xapp-created-token" } });
        }
        if (n.props.placeholder && n.props.placeholder.indexOf("xoxb") >= 0) {
          n.props.onChange({ target: { value: "xoxb-created-token" } });
        }
      }
    });
    MR.rerender();
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    btn.props.onClick();
    """)
    settle(ctx)
    saved = js(ctx, "SAVED")
    assert saved is not None
    assert saved["id"] == "cp-created"
    assert js(ctx, "NAVIGATED") == []


def test_the_detail_page_stacks_kit_sections(ctx) -> None:
    ctx.eval("""
    serve("channel-provider-detail:cp-1", {
      id: "cp-1",
      provider: "slack",
      created_at: "2026-10-22T00:00:00Z",
      config: { app_token: "xapp-...", bot_token: "xoxb-..." }
    });
    serve("channels:channels", { items: [] });
    MR.mount(window.ChannelProviderDetail, { providerId: "cp-1" });
    """)
    assert js(ctx, "MR.find('ch-section-overview') != null")
    assert js(ctx, "MR.find('ch-section-config') != null")
    assert js(ctx, "MR.find('ch-section-channels') != null")
    assert "‹ Channel Providers" in js(ctx, "textOf(MR.find('ch-detail-back'))")
    assert js(ctx, "MR.find('ch-detail-back').type") == "button"


def test_channels_section_distinguishes_loading_failure_and_empty(ctx) -> None:
    ctx.eval("""
    serve("channel-provider-detail:cp-state", { id: "cp-state", provider: "slack", config: {}, created_at: "2026-10-22T00:00:00Z" });
    MR.mount(window.ChannelProviderDetail, { providerId: "cp-state" });
    """)
    assert js(ctx, "MR.find('ch-channels-loading') != null")
    ctx.eval("fail('channels:channels', { status: 500, title: 'Failed', detail: 'channels unavailable' }); MR.rerender();")
    assert "channels unavailable" in js(ctx, "textOf(MR.find('ch-section-channels'))")
    ctx.eval("serve('channels:channels', { items: [] }); MR.rerender();")
    assert "No channels" in js(ctx, "textOf(MR.find('ch-section-channels'))")


def test_detail_edit_opens_the_register_overlay_in_edit_mode(ctx) -> None:
    ctx.eval("""
    serve("channel-provider-detail:cp-1", {
      id: "cp-1",
      provider: "slack",
      created_at: "2026-10-22T00:00:00Z",
      config: { app_token: "xapp-...", bot_token: "xoxb-..." }
    });
    serve("channels:channels", { items: [] });
    MR.mount(window.ChannelProviderDetail, { providerId: "cp-1" });
    press("ch-detail-edit");
    """)
    assert js(ctx, "MR.find('pc-register') != null")


def test_detail_delete_calls_on_deleted(ctx) -> None:
    ctx.eval("""
    DELETED = null;
    CALLS = [];
    serve("channel-provider-detail:cp-1", {
      id: "cp-1",
      provider: "slack",
      created_at: "2026-10-22T00:00:00Z",
      config: { app_token: "xapp-...", bot_token: "xoxb-..." }
    });
    serve("channels:channels", { items: [] });
    MR.mount(window.ChannelProviderDetail, {
      providerId: "cp-1",
      onDeleted: function (id) { DELETED = id; }
    });
    press("ch-detail-delete");
    var deleteBtn = ALL("modal").filter(function (n) {
      return n.type === "button" && textOf(n).indexOf("Delete provider") >= 0;
    })[0];
    deleteBtn.props.onClick();
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    del_calls = [c for c in calls if c["method"] == "DELETE" and "cp-1" in c["path"]]
    assert len(del_calls) == 1
    assert js(ctx, "DELETED") == "cp-1"
