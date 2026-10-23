"""Unit tests for Semantic Search Storage in the register overlay frame (plan 02, Task 17).

Tests that:
- SSPListPage create opens PC_RegisterOverlay with initialClass="ssp" (replacing PC_RegisterDropdown);
- SSPCreateModal inside frame renders "Register" on create and "Save" on edit;
- Hosted create calls onSaved and does not navigate;
- Edit keeps every field the form does not show (Review Focus 2: pgvectorscale unshown fields);
- Lance edit keeps its stored hnsw_ef_search (not 40);
- Edit needs no password and keeps the stored masked password;
- SSPDetail stacks kit sections without tabs;
- Detail edit opens PC_RegisterOverlay in edit mode;
- Detail delete calls onDeleted;
- Built-in lance offers no delete.
"""

from __future__ import annotations

from pathlib import Path
import pytest

from tests.ui._register_harness import COMPONENTS, base_code, context, js, settle, transpiled

SSP_FILE = COMPONENTS / "semantic-search.jsx"

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
    return base_code(with_overlay=True) + "\n" + transpiled(SSP_FILE)


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    try:
        yield c
    finally:
        c.close()


def test_ssp_creation_opens_the_register_overlay() -> None:
    src = SSP_FILE.read_text(encoding="utf-8")
    assert "PC_RegisterOverlay" in src
    assert 'initialClass="ssp"' in src or 'initialClass: "ssp"' in src
    assert "PC_RegisterDropdown" not in src


def test_the_form_renders_inside_a_frame_with_register_and_save_labels(ctx) -> None:
    ctx.eval("""
    LAST_FRAME = null;
    MR.mount(window.SSPCreateModal, { onClose: function () {}, frame: frameFn });
    """)
    assert js(ctx, "textOf(LAST_FRAME.footerRight)") == "Register"
    assert js(ctx, "textOf(LAST_FRAME.footerLeft)") == "Cancel"
    assert js(ctx, "MR.find('framed-root') != null")

    ctx.eval("""
    LAST_FRAME = null;
    MR.mount(window.SSPCreateModal, {
        existing: { id: "ssp-1", provider: "pgvector", config: { hostname: "localhost" } },
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
    MR.mount(window.SSPCreateModal, {
        onClose: function () {},
        onSaved: function (row) { SAVED = row; },
        frame: frameFn
    });
    // Set required fields for pgvector
    ALL("framed-root").forEach(function (n) {
      if (n.type === "input" && n.props.placeholder === "pg-prod-main") {
        n.props.onChange({ target: { value: "ssp-created" } });
      }
      if (n.type === "input" && n.props.placeholder === "pg-prod.internal") {
        n.props.onChange({ target: { value: "pg.host" } });
      }
      if (n.type === "input" && n.props.placeholder === "primer_rw") {
        n.props.onChange({ target: { value: "rw_user" } });
      }
      if (n.type === "input" && n.props.type === "password") {
        n.props.onChange({ target: { value: "secret123" } });
      }
    });
    MR.rerender();
    // Press the primary submit button in frame footerRight
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    btn.props.onClick();
    """)
    settle(ctx)
    saved = js(ctx, "SAVED")
    assert saved is not None
    assert saved["id"] == "ssp-created"
    assert js(ctx, "NAVIGATED") == []


def test_edit_round_trips_every_unshown_field(ctx) -> None:
    # Review Focus 2: a pgvectorscale row with distance_metric "l2", reindex_cron "0 3 * * *",
    # hnsw_ef_search 80, diskann_storage_layout "plain", diskann_max_alpha 1.4,
    # diskann_num_bits_per_dimension 2 and the masked password; change hnsw_m;
    # the PUT config equals the row's except hnsw_m
    ctx.eval("""
    CALLS = [];
    var row = {
      id: "ssp-scale",
      provider: "pgvectorscale",
      config: {
        hostname: "pg.internal",
        port: 5432,
        username: "primer",
        password: "**********",
        database: "primer_db",
        db_schema: "public",
        hnsw_m: 16,
        hnsw_ef_construction: 64,
        use_halfvec: false,
        enable_diskann: false,
        distance_metric: "l2",
        reindex_cron: "0 3 * * *",
        hnsw_ef_search: 80,
        diskann_storage_layout: "plain",
        diskann_max_alpha: 1.4,
        diskann_num_bits_per_dimension: 2
      }
    };
    MR.mount(window.SSPCreateModal, {
      existing: row,
      onClose: function () {},
      frame: frameFn
    });
    // Find hnsw_m input and change to 32
    ALL("frame-body").forEach(function (n) {
      if (n.type === "input" && n.props.type === "number" && n.props.value === 16) {
        n.props.onChange({ target: { value: 32 } });
      }
    });
    MR.rerender();
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    btn.props.onClick();
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    put_call = [c for c in calls if c["method"] == "PUT" and "ssp-scale" in c["path"]][0]
    config = put_call["body"]["config"]
    assert config["hnsw_m"] == 32
    assert config["distance_metric"] == "l2"
    assert config["reindex_cron"] == "0 3 * * *"
    assert config["hnsw_ef_search"] == 80
    assert config["diskann_storage_layout"] == "plain"
    assert config["diskann_max_alpha"] == 1.4
    assert config["diskann_num_bits_per_dimension"] == 2
    assert config["password"] == "**********"


def test_lance_edit_keeps_its_hnsw_ef_search(ctx) -> None:
    ctx.eval("""
    CALLS = [];
    var row = {
      id: "ssp-lance",
      provider: "lance",
      config: {
        path: "/data/lance",
        hnsw_m: 16,
        hnsw_ef_construction: 64,
        hnsw_ef_search: 120,
        index_min_rows: 1000
      }
    };
    MR.mount(window.SSPCreateModal, {
      existing: row,
      onClose: function () {},
      frame: frameFn
    });
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    btn.props.onClick();
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    put_call = [c for c in calls if c["method"] == "PUT" and "ssp-lance" in c["path"]][0]
    assert put_call["body"]["config"]["hnsw_ef_search"] == 120


def test_lance_fallback_hnsw_ef_search_is_100(ctx) -> None:
    ctx.eval("""
    CALLS = [];
    var row = {
      id: "ssp-lance",
      provider: "lance",
      config: {
        path: "/data/lance",
        hnsw_m: 16,
        hnsw_ef_construction: 64,
        index_min_rows: 1000
      }
    };
    MR.mount(window.SSPCreateModal, {
      existing: row,
      onClose: function () {},
      frame: frameFn
    });
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    btn.props.onClick();
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    put_call = [c for c in calls if c["method"] == "PUT" and "ssp-lance" in c["path"]][0]
    assert put_call["body"]["config"]["hnsw_ef_search"] == 100



def test_edit_needs_no_password(ctx) -> None:
    ctx.eval("""
    CALLS = [];
    var row = {
      id: "ssp-pg",
      provider: "pgvector",
      config: {
        hostname: "pg.internal",
        port: 5432,
        username: "primer",
        password: "**********",
        database: "primer_db"
      }
    };
    MR.mount(window.SSPCreateModal, {
      existing: row,
      onClose: function () {},
      frame: frameFn
    });
    // Password input starts empty in edit mode; submit directly without typing password
    var btn = ALL("frame-right").filter(function (n) { return n.type === "button"; })[0];
    btn.props.onClick();
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    put_calls = [c for c in calls if c["method"] == "PUT" and "ssp-pg" in c["path"]]
    assert len(put_calls) == 1
    assert put_calls[0]["body"]["config"]["password"] == "**********"


def test_the_detail_page_stacks_kit_sections_without_tabs(ctx) -> None:
    ctx.eval("""
    serve("ssp-detail:ssp-1", {
      id: "ssp-1",
      provider: "pgvector",
      config: { hostname: "pg.internal", port: 5432, username: "primer", database: "primer_db" }
    });
    serve("ssp-detail:ssp-1:collections", { items: [] });
    MR.mount(window.SSPDetail, { sspId: "ssp-1" });
    """)
    assert js(ctx, "MR.find('ssp-section-overview') != null")
    assert js(ctx, "MR.find('ssp-section-config') != null")
    assert js(ctx, "MR.find('ssp-section-collections') != null")
    assert "‹ Semantic Search Storage" in js(ctx, "textOf(MR.find('ssp-detail-back'))")


def test_detail_edit_opens_the_register_overlay_in_edit_mode(ctx) -> None:
    ctx.eval("""
    serve("ssp-detail:ssp-1", {
      id: "ssp-1",
      provider: "pgvector",
      config: { hostname: "pg.internal", port: 5432, username: "primer", database: "primer_db" }
    });
    serve("ssp-detail:ssp-1:collections", { items: [] });
    MR.mount(window.SSPDetail, { sspId: "ssp-1" });
    press("ssp-detail-edit");
    """)
    assert js(ctx, "MR.find('pc-register') != null")


def test_detail_delete_calls_on_deleted(ctx) -> None:
    ctx.eval("""
    DELETED = null;
    CALLS = [];
    serve("ssp-detail:ssp-1", {
      id: "ssp-1",
      provider: "pgvector",
      config: { hostname: "pg.internal", port: 5432, username: "primer", database: "primer_db" }
    });
    serve("ssp-detail:ssp-1:collections", { items: [] });
    MR.mount(window.SSPDetail, {
      sspId: "ssp-1",
      onDeleted: function (id) { DELETED = id; }
    });
    press("ssp-detail-delete");
    // Confirm delete in the danger dialog
    var deleteBtn = ALL("modal").filter(function (n) {
      return n.type === "button" && textOf(n).indexOf("Delete provider") >= 0;
    })[0];
    deleteBtn.props.onClick();
    """)
    settle(ctx)
    calls = js(ctx, "CALLS")
    del_calls = [c for c in calls if c["method"] == "DELETE" and "ssp-1" in c["path"]]
    assert len(del_calls) == 1
    assert js(ctx, "DELETED") == "ssp-1"


def test_the_built_in_lance_offers_no_delete(ctx) -> None:
    ctx.eval("""
    serve("ssp-detail:lance", {
      id: "lance",
      provider: "lance",
      config: { path: "/var/data/lance" }
    });
    serve("ssp-detail:lance:collections", { items: [] });
    MR.mount(window.SSPDetail, { sspId: "lance" });
    """)
    assert js(ctx, "MR.find('ssp-detail-delete') == null")
