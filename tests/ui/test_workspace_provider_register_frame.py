"""Unit tests for Workspace Providers in the register overlay frame (plan 02, Task 18).

Tests that:
- A gateway provider round-trips unchanged on edit (section 20 Q5);
- A gateway provider can be created with Gateway API HTTPRoute reachability;
- Switching the reachability kind replaces the reachability object;
- Edit keeps every field the form does not show (PC_mergeKeep);
- The form renders inside a frame with 'Register' and 'Save' labels;
- A hosted create calls onSaved and does not navigate;
- The detail page stacks kit sections without tabs;
- Detail edit opens PC_RegisterOverlay in edit mode;
- Detail delete calls onDeleted;
- The local provider offers neither edit nor delete.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from primer.model.workspace import WorkspaceProvider

from tests.ui._register_harness import COMPONENTS, base_code, context, js, settle, transpiled

WP_FILE = COMPONENTS / "workspaces" / "providers.jsx"
SHARED_FILE = COMPONENTS / "workspaces" / "shared.jsx"

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
    return base_code(with_overlay=True) + "\n" + transpiled(SHARED_FILE, WP_FILE)


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    try:
        yield c
    finally:
        c.close()


def test_a_gateway_provider_round_trips_unchanged_on_edit(ctx) -> None:
    ctx.eval("""
    CALLS = [];
    var row = {
      id: "wp-gw-edit",
      provider: "kubernetes",
      config: {
        kind: "kubernetes",
        variant: "system",
        namespace: "primer",
        connection: { kind: "in_cluster" },
        reachability: {
          kind: "gateway_httproute",
          gateway: { name: "my-gw", namespace: "gw-ns", section_name: "https" },
          routing: { kind: "path_prefix", hostname: "ws.example.com", path_template: "/ws/{workspace_id}" },
          scheme: "wss",
          external_port: 443,
          backend_port: 5959
        }
      }
    };
    MR.mount(window.WorkspaceProviderCreateModal, {
      existing: row,
      onClose: function () {},
      frame: frameFn
    });
    // Change namespace
    ALL("frame-body").forEach(function (n) {
      if (n.type === "input" && n.props.value === "primer") {
        n.props.onChange({ target: { value: "primer-dev" } });
      }
    });
    MR.rerender();
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    btn.props.onClick();
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    put_call = [c for c in calls if c["method"] == "PUT" and "wp-gw-edit" in c["path"]][0]
    reach = put_call["body"]["config"]["reachability"]
    assert reach["kind"] == "gateway_httproute"
    assert reach["gateway"] == {"name": "my-gw", "namespace": "gw-ns", "section_name": "https"}
    assert reach["routing"] == {"kind": "path_prefix", "hostname": "ws.example.com", "path_template": "/ws/{workspace_id}"}
    assert reach["scheme"] == "wss"
    assert reach["external_port"] == 443
    assert reach["backend_port"] == 5959
    assert put_call["body"]["config"]["namespace"] == "primer-dev"


def test_a_gateway_provider_can_be_created(ctx) -> None:
    ctx.eval("""
    CALLS = [];
    MR.mount(window.WorkspaceProviderCreateModal, {
      onClose: function () {},
      frame: frameFn
    });
    // Set backend to kubernetes
    ALL("frame-body").forEach(function (n) {
      if (n.type === "input" && n.props.placeholder === "local-dev") {
        n.props.onChange({ target: { value: "wp-gw-new" } });
      }
      if (n.type === "select" && n.props.value === "local") {
        n.props.onChange({ target: { value: "kubernetes" } });
      }
    });
    MR.rerender();
    // Switch reachability to gateway_httproute
    ALL("frame-body").forEach(function (n) {
      if (n.type === "select" && n.props.value === "in_cluster") {
        n.props.onChange({ target: { value: "gateway_httproute" } });
      }
    });
    MR.rerender();
    // Set required gateway fields
    ALL("frame-body").forEach(function (n) {
      if (n.type === "input" && n.props.placeholder === "prod-gw") {
        n.props.onChange({ target: { value: "my-gateway" } });
      }
      if (n.type === "input" && n.props.placeholder === "{workspace_id}.ws.local") {
        n.props.onChange({ target: { value: "{workspace_id}.ws.prod" } });
      }
      if (n.type === "input" && n.props.placeholder === "80") {
        GW_EXTERNAL_PORT_WAS = n.props.value;
        n.props.onChange({ target: { value: "443" } });
      }
    });
    MR.rerender();
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    btn.props.onClick();
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    assert js(ctx, "GW_EXTERNAL_PORT_WAS") == ""
    post_call = [c for c in calls if c["method"] == "POST" and "workspace_providers" in c["path"]][0]
    reach = post_call["body"]["config"]["reachability"]
    assert reach["kind"] == "gateway_httproute"
    assert reach["gateway"]["name"] == "my-gateway"
    assert reach["routing"] == {"kind": "hostname", "hostname_template": "{workspace_id}.ws.prod"}
    assert reach["external_port"] == 443


def test_switching_the_reachability_kind_replaces_the_object(ctx) -> None:
    ctx.eval("""
    CALLS = [];
    var row = {
      id: "wp-switch",
      provider: "kubernetes",
      config: {
        kind: "kubernetes",
        variant: "system",
        namespace: "primer",
        connection: { kind: "in_cluster" },
        reachability: { kind: "in_cluster" }
      }
    };
    MR.mount(window.WorkspaceProviderCreateModal, {
      existing: row,
      onClose: function () {},
      frame: frameFn
    });
    ALL("frame-body").forEach(function (n) {
      if (n.type === "select" && n.props.value === "in_cluster") {
        n.props.onChange({ target: { value: "gateway_httproute" } });
      }
    });
    MR.rerender();
    ALL("frame-body").forEach(function (n) {
      if (n.type === "input" && n.props.placeholder === "prod-gw") {
        n.props.onChange({ target: { value: "switched-gw" } });
      }
        if (n.type === "input" && n.props.placeholder === "{workspace_id}.ws.local") {
          n.props.onChange({ target: { value: "{workspace_id}.example.test" } });
        }
        if (n.type === "input" && n.props.placeholder === "80") {
          n.props.onChange({ target: { value: "443" } });
        }
    });
    MR.rerender();
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    btn.props.onClick();
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    put_call = [c for c in calls if c["method"] == "PUT" and "wp-switch" in c["path"]][0]
    reach = put_call["body"]["config"]["reachability"]
    assert reach["kind"] == "gateway_httproute"
    assert reach["gateway"]["name"] == "switched-gw"
    assert "in_cluster" not in reach


def test_edit_round_trips_every_unshown_field(ctx) -> None:
    row = WorkspaceProvider.model_validate({
        "id": "wp-complete",
        "provider": "kubernetes",
        "config": {
            "kind": "kubernetes",
            "variant": "system",
            "namespace": "primer",
            "connection": {"kind": "in_cluster"},
            "reachability": {"kind": "in_cluster"},
            "image_pull_secrets": ["registry-one"],
        },
    }).model_dump(mode="json")
    ctx.eval("var REAL_WORKSPACE_ROW = " + json.dumps(row) + ";")
    ctx.eval("""
    CALLS = [];
    var row = REAL_WORKSPACE_ROW;
    MR.mount(window.WorkspaceProviderCreateModal, {
      existing: row,
      onClose: function () {},
      frame: frameFn
    });
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    btn.props.onClick();
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    put_call = [c for c in calls if c["method"] == "PUT" and "wp-complete" in c["path"]][0]
    cfg = put_call["body"]["config"]
    assert WorkspaceProvider.model_validate(put_call["body"])
    assert cfg["image_pull_secrets"] == ["registry-one"]


def test_edit_can_clear_optional_gateway_fields(ctx) -> None:
    ctx.eval("""
    CALLS = [];
    var row = {
      id: "wp-clear",
      provider: "kubernetes",
      config: {
        kind: "kubernetes", variant: "system", namespace: "primer",
        connection: { kind: "in_cluster" },
        reachability: {
          kind: "gateway_httproute",
          gateway: { name: "gw", namespace: "gw-ns", section_name: "https" },
          routing: { kind: "hostname", hostname_template: "{workspace_id}.example.test" },
          scheme: "wss", external_port: 443, backend_port: 5959
        }
      }
    };
    MR.mount(window.WorkspaceProviderCreateModal, { existing: row, onClose: function () {}, frame: frameFn });
    ALL("frame-body").forEach(function (n) {
      if (n.type === "input" && (n.props.value === "gw-ns" || n.props.value === "https")) {
        n.props.onChange({ target: { value: "" } });
      }
    });
    MR.rerender();
    ALL("frame-right").filter(function (n) { return n.type === "button"; })[0].props.onClick();
    """)
    settle(ctx)
    call = [c for c in js(ctx, "CALLS") if c["method"] == "PUT"][0]
    assert call["body"]["config"]["reachability"]["gateway"] == {
        "name": "gw", "namespace": None, "section_name": None,
    }


def test_gateway_422_error_is_mapped_to_the_visible_port_field(ctx) -> None:
    ctx.eval("""
    var row = {
      id: "wp-errors", provider: "kubernetes",
      config: {
        kind: "kubernetes", variant: "system", namespace: "primer",
        connection: { kind: "in_cluster" },
        reachability: { kind: "gateway_httproute", gateway: { name: "gw" },
          routing: { kind: "hostname", hostname_template: "{workspace_id}.example.test" },
          scheme: "wss", external_port: 443, backend_port: 5959 }
      }
    };
    HANDLER = function () { return refuse({ status: 422, title: "Validation Error", detail: "invalid",
      extensions: { errors: [{ loc: ["body", "config", "kubernetes", "reachability", "gateway_httproute", "external_port"], msg: "must be at most 65535" }] } }); };
    MR.mount(window.WorkspaceProviderCreateModal, { existing: row, onClose: function () {}, frame: frameFn });
    ALL("frame-right").filter(function (n) { return n.type === "button"; })[0].props.onClick();
    """)
    settle(ctx)
    assert "must be at most 65535" in js(ctx, "textOf(MR.find('frame-body'))")


def test_the_form_renders_inside_a_frame_with_register_and_save_labels(ctx) -> None:
    ctx.eval("""
    LAST_FRAME = null;
    MR.mount(window.WorkspaceProviderCreateModal, { onClose: function () {}, frame: frameFn });
    """)
    assert js(ctx, "textOf(LAST_FRAME.footerRight)") == "Register"
    assert js(ctx, "textOf(LAST_FRAME.footerLeft)") == "Cancel"
    assert js(ctx, "MR.find('framed-root') != null")

    ctx.eval("""
    LAST_FRAME = null;
    MR.mount(window.WorkspaceProviderCreateModal, {
        existing: { id: "wp-1", provider: "local", config: { root_path: "/data" } },
        onClose: function () {},
        frame: frameFn
    });
    """)
    assert js(ctx, "textOf(LAST_FRAME.footerRight)") == "Save"


def test_a_hosted_create_calls_on_saved_and_does_not_navigate(ctx) -> None:
    ctx.eval("""
    SAVED = null;
    NAVIGATED = [];
    CALLS = [];
    MR.mount(window.WorkspaceProviderCreateModal, {
        onClose: function () {},
        onSaved: function (row) { SAVED = row; },
        frame: frameFn
    });
    // Set required fields for local provider
    ALL("framed-root").forEach(function (n) {
      if (n.type === "input" && n.props.placeholder === "local-dev") {
        n.props.onChange({ target: { value: "wp-created" } });
      }
      if (n.type === "input" && n.props.placeholder === "/var/lib/primer/workspaces") {
        n.props.onChange({ target: { value: "/data/ws" } });
      }
    });
    MR.rerender();
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    btn.props.onClick();
    """)
    settle(ctx)
    saved = js(ctx, "SAVED")
    assert saved is not None
    assert saved["id"] == "wp-created"
    assert js(ctx, "NAVIGATED") == []


def test_the_detail_page_stacks_kit_sections_without_tabs(ctx) -> None:
    ctx.eval("""
    serve("ws:provider:wp-1", {
      id: "wp-1",
      provider: "kubernetes",
      config: { kind: "kubernetes", namespace: "primer" }
    });
    serve("ws:provider:wp-1:templates", { items: [] });
    MR.mount(window.WorkspaceProviderDetail, { providerId: "wp-1" });
    """)
    assert js(ctx, "MR.find('wp-section-overview') != null")
    assert js(ctx, "MR.find('wp-section-config') != null")
    assert js(ctx, "MR.find('wp-section-templates') != null")
    assert "‹ Workspace Providers" in js(ctx, "textOf(MR.find('wp-detail-back'))")
    assert js(ctx, "MR.find('wp-detail-back').type") == "button"


def test_template_section_distinguishes_loading_failure_and_empty(ctx) -> None:
    ctx.eval("""
    serve("ws:provider:wp-state", { id: "wp-state", provider: "kubernetes", config: { kind: "kubernetes", namespace: "primer" } });
    MR.mount(window.WorkspaceProviderDetail, { providerId: "wp-state" });
    """)
    assert js(ctx, "MR.find('wp-templates-loading') != null")
    ctx.eval("fail('ws:provider:wp-state:templates', { status: 500, title: 'Failed', detail: 'templates unavailable' }); MR.rerender();")
    assert "templates unavailable" in js(ctx, "textOf(MR.find('wp-section-templates'))")
    ctx.eval("serve('ws:provider:wp-state:templates', { items: [] }); MR.rerender();")
    assert "No templates bound" in js(ctx, "textOf(MR.find('wp-section-templates'))")


def test_detail_edit_opens_the_register_overlay_in_edit_mode(ctx) -> None:
    ctx.eval("""
    serve("ws:provider:wp-1", {
      id: "wp-1",
      provider: "kubernetes",
      config: { kind: "kubernetes", namespace: "primer" }
    });
    serve("ws:provider:wp-1:templates", { items: [] });
    MR.mount(window.WorkspaceProviderDetail, { providerId: "wp-1" });
    press("wp-detail-edit");
    """)
    assert js(ctx, "MR.find('pc-register') != null")


def test_detail_delete_calls_on_deleted(ctx) -> None:
    ctx.eval("""
    DELETED = null;
    CALLS = [];
    serve("ws:provider:wp-1", {
      id: "wp-1",
      provider: "kubernetes",
      config: { kind: "kubernetes", namespace: "primer" }
    });
    serve("ws:provider:wp-1:templates", { items: [] });
    MR.mount(window.WorkspaceProviderDetail, {
      providerId: "wp-1",
      onDeleted: function (id) { DELETED = id; }
    });
    press("wp-detail-delete");
    var deleteBtn = ALL("modal").filter(function (n) {
      return n.type === "button" && textOf(n).indexOf("Delete provider") >= 0;
    })[0];
    deleteBtn.props.onClick();
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    del_calls = [c for c in calls if c["method"] == "DELETE" and "wp-1" in c["path"]]
    assert len(del_calls) == 1
    assert js(ctx, "DELETED") == "wp-1"


def test_the_local_provider_offers_neither_edit_nor_delete(ctx) -> None:
    ctx.eval("""
    serve("ws:provider:local", {
      id: "local",
      provider: "local",
      config: { kind: "local", root_path: "/var/lib/primer" }
    });
    serve("ws:provider:local:templates", { items: [] });
    MR.mount(window.WorkspaceProviderDetail, { providerId: "local" });
    """)
    assert js(ctx, "MR.find('wp-detail-edit') == null")
    assert js(ctx, "MR.find('wp-detail-delete') == null")
