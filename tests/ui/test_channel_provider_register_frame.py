"""Unit tests for Channel Providers in the register overlay frame (plan 02, Task 19).

Tests that:
- Edit keeps a stored secret left blank (a slack row with a masked signing_secret: the PUT carries the mask);
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
from pathlib import Path
import pytest

from primer.model.channel import ChannelProvider

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


def test_edit_keeps_a_stored_secret_left_blank(ctx) -> None:
    row = ChannelProvider.model_validate({
        "id": "cp-slack-1",
        "provider": "slack",
        "config": {
            "app_token": "xapp-real-token",
            "bot_token": "xoxb-real-token",
            "signing_secret": "real-signing-secret",
        },
    }).model_dump(mode="json")
    ctx.eval("var REAL_CHANNEL_ROW = " + json.dumps(row) + ";")
    ctx.eval("""
    CALLS = [];
    var row = REAL_CHANNEL_ROW;
    MR.mount(window.NewChannelProviderModal, {
      existing: row,
      onClose: function () {},
      frame: frameFn
    });
    // Click Save without typing any secrets
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    btn.props.onClick();
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    put_calls = [c for c in calls if c["method"] == "PUT" and "cp-slack-1" in c["path"]]
    assert len(put_calls) == 1
    cfg = put_calls[0]["body"]["config"]
    assert cfg["signing_secret"] == "**********"
    assert cfg["app_token"] == "**********"
    assert cfg["bot_token"] == "**********"


def test_edit_can_clear_the_optional_channel_number(ctx) -> None:
    ctx.eval("""
    CALLS = [];
    var row = { id: "cp-telegram", provider: "telegram", config: { bot_token: "**********", poll_timeout_seconds: 25 } };
    MR.mount(window.NewChannelProviderModal, { existing: row, onClose: function () {}, frame: frameFn });
    ALL("frame-body").forEach(function (n) {
      if (n.type === "input" && n.props.type === "number") n.props.onChange({ target: { value: "" } });
    });
    MR.rerender();
    ALL("frame-right").filter(function (n) { return n.type === "button"; })[0].props.onClick();
    """)
    settle(ctx)
    call = [c for c in js(ctx, "CALLS") if c["method"] == "PUT"][0]
    assert call["body"]["config"]["poll_timeout_seconds"] is None


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
