"""The Workspace templates page (platform revamp Plan 08, Task 11; design 14.2): ``WT_TemplatesPage``, the custom Platform page of nav ``templates``.

One kit card per template (the backend's icon, the id, the description, and the provider, image and size as facts), every template listed however many
there are (Review Focus 3), a filter over the id, description, provider, kind and image, and "+ Template" opening the template overlay. A card opens its
template in the overlay; its menu has Delete, except on the built-in template and, for a non-admin, on a template that holds admin-only settings (the
server would refuse both: Review Focus 5). The palette verb asks a page that is not mounted yet to open the create overlay when it mounts.

The context is the overlay's (``tests/ui/test_wt_template_overlay.py``: the real kit, form field, shared components and ``templates.jsx``), plus the
real ``NV_countText`` and ``NV_emptyText`` of ``nv-platform.jsx`` and a ``window`` event stand-in. ``PK_PageHeader`` and ``PK_Card`` are wrapped to
record their props.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.ui._register_harness import ROOT, base_code, context, js, settle, transpiled
from tests.ui._wt_envelopes import envelope
from tests.ui.test_wt_helpers import CHOICES
from tests.ui.test_wt_template_overlay import SHARED_FILE, STUBS, TEMPLATES_FILE

PLATFORM = (ROOT / "ui" / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    start = src.index("function " + name + "(")
    return src[start:src.index("\n}\n", start) + 3]


PAGE_STUBS = r"""
var LISTENERS = {};
window.addEventListener = function (t, f) { (LISTENERS[t] = LISTENERS[t] || []).push(f); };
window.removeEventListener = function (t, f) { LISTENERS[t] = (LISTENERS[t] || []).filter(function (g) { return g !== f; }); };
window.dispatchEvent = function (ev) { (LISTENERS[ev.type] || []).slice().forEach(function (f) { f(ev); }); return true; };
function Event(type) { this.type = type; }
var HEADER = null, CARDS = {};
var __PKH = window.PK_PageHeader;
window.PK_PageHeader = function (p) { HEADER = p; return __PKH(p); };
var __PKC = window.PK_Card;
window.PK_Card = function (p) { CARDS[p.id] = p; return __PKC(p); };
function shownCards() { return MR.findAll("pk-card:").map(function (e) { return e.props["data-testid"].slice("pk-card:".length); }); }
"""

PYTHON_DEV = {"id": "python-dev", "description": "Python 3.12 with uv", "provider_id": "k3s",
              "backend": {"kind": "kubernetes", "image": "rt:1.4", "pvc_size": "10Gi"}, "resources": {"cpu_cores": 2, "memory_bytes": 4294967296}}
BUILT_IN = {"id": "local-default", "description": "Default minimal local workspace template (auto-bootstrapped).", "provider_id": "local",
            "backend": {"kind": "local"}}
PODMAN = {"id": "node-box", "description": "Node 20", "provider_id": "pod", "backend": {"kind": "container", "image": "node:20", "cpu_cores": 1}}
HELD = {"id": "gpu-held", "description": "Tolerates the GPU nodes", "provider_id": "k3s",
        "backend": {"kind": "kubernetes", "image": "rt:1.4", "tolerations": [{"key": "gpu", "operator": "Exists"}]}}


@pytest.fixture(scope="module")
def code() -> str:
    helpers = "\n".join(_fn(PLATFORM, n) for n in ("NV_emptyText", "NV_countText", "NV_nounOf"))
    exports = "\nwindow.NV_emptyText = NV_emptyText; window.NV_countText = NV_countText;\n"
    return base_code() + "\n" + transpiled(SHARED_FILE, TEMPLATES_FILE) + "\n" + helpers + exports


@pytest.fixture
def ctx(code: str):
    c = context(code)
    c.eval(STUBS)
    c.eval(PAGE_STUBS)
    choices = [CHOICES["dock"], CHOICES["k3s"], CHOICES["loc"], {"id": "pod", "kind": "container", "label": "Podman", "default_image": "x"}]
    c.eval("serve('ws:provider-choices', { items: " + json.dumps(choices) + " });")
    try:
        yield c
    finally:
        c.close()


def _mount(ctx, rows: list | None, role: str = "admin") -> None:
    if rows is not None:
        ctx.eval("serve('ws:templates', { items: " + json.dumps(rows) + ", total: " + str(len(rows)) + " });")
    # A fresh page: the root is emptied first, so the page's state (an open overlay) does not carry over from the last mount.
    ctx.eval("MR.mount(function Nothing() { return null; }, {});")
    ctx.eval(f"ROLE = {json.dumps(role)}; CARDS = {{}}; HEADER = null; LAST_OVERLAY = null; MR.mount(window.WT_TemplatesPage, {{}});")


# ---- the cards ----------------------------------------------------------------------------------------------------------------------------------------


def test_cards_show_the_id_description_and_three_facts(ctx):
    _mount(ctx, [PYTHON_DEV])
    card = js(ctx, "CARDS['python-dev']")
    assert (card["title"], card["subtitle"]) == ("python-dev", "Python 3.12 with uv")
    assert card["facts"] == [["Provider", "k3s · Kubernetes"], ["Image", "rt:1.4"], ["Size", "2 CPU · 4 GiB · 10 GiB disk"]]
    assert card["icon"] == {"path": js(ctx, "WT_KIND_ICONS.kubernetes")}
    assert [(m["label"], m["danger"], m["testid"]) for m in card["menu"]] == [("Delete", True, "wt-delete:python-dev")]
    assert not card.get("badge")


def test_the_label_is_the_providers_own(ctx):
    _mount(ctx, [PODMAN])
    assert js(ctx, "CARDS['node-box'].facts[0]") == ["Provider", "pod · Podman"]
    assert js(ctx, "CARDS['node-box'].icon.path") == js(ctx, "WT_KIND_ICONS.container")


def test_the_built_in_template_has_a_badge_and_no_menu(ctx):
    _mount(ctx, [BUILT_IN, PYTHON_DEV])
    assert js(ctx, "CARDS['local-default'].badge") == "built in"
    assert ctx.eval("!CARDS['local-default'].menu") is True
    assert ctx.eval("MR.find('pk-card-menu:local-default') === null")


def test_a_user_gets_no_delete_on_a_template_holding_admin_only_settings(ctx):
    _mount(ctx, [HELD, PYTHON_DEV], role="user")
    assert ctx.eval("!CARDS['gpu-held'].menu") is True
    assert js(ctx, "CARDS['python-dev'].menu[0].testid") == "wt-delete:python-dev"
    _mount(ctx, [HELD], role="admin")
    assert js(ctx, "CARDS['gpu-held'].menu[0].testid") == "wt-delete:gpu-held"


def test_the_page_root_is_not_the_frames_test_id(ctx):
    """Lead ruling 14: the Platform frame (NV_CustomPlatPage) carries nv-plat-page:templates; two elements with one test id break strict locators."""
    _mount(ctx, [PYTHON_DEV])
    assert ctx.eval("!!MR.find('wt-templates-page')") and ctx.eval("MR.find('nv-plat-page:templates') === null")
    assert '"nv-plat-page:' not in TEMPLATES_FILE.read_text(encoding="utf-8"), "no element of the page carries the frame's test id"


# ---- delete ---------------------------------------------------------------------------------------------------------------------------------------------

DELETE_ENV = (
    "var DIALOGS = [], DELS = [], TOLD = [], REFETCHED = 0, ANSWER = true, OUT = null; var REFUSE = null;"
    "var env = { confirmDialog: function (o) { DIALOGS.push(o); return Promise.resolve(ANSWER); },"
    " apiFetch: function (m, p) { DELS.push([m, p]); return REFUSE ? Promise.reject(REFUSE) : Promise.resolve(null); },"
    " toast: function (m, x) { TOLD.push([m, x || null]); }, refetch: function () { REFETCHED++; } };"
)


def test_delete_is_in_the_card_menu_and_promises_no_409(ctx):
    ctx.eval(DELETE_ENV + "WT_deleteTemplate(env, { id: 'tpl-a' }).then(function (r) { OUT = r; });")
    dialog = js(ctx, "DIALOGS[0]")
    assert dialog["title"] == "Delete tpl-a" and dialog["danger"] is True
    assert dialog["message"] == (
        "Permanently delete tpl-a? New workspaces can no longer be made from it; existing workspaces keep the copy of the template they were made from."
    )
    assert "409" not in dialog["message"] and "Referenced" not in dialog["message"]
    assert js(ctx, "DELS") == [["DELETE", "/workspace_templates/tpl-a"]]
    assert js(ctx, "TOLD") == [["Deleted tpl-a", None]] and js(ctx, "REFETCHED") == 1 and js(ctx, "OUT") is True


def test_a_refused_delete_says_why_and_a_cancelled_one_sends_nothing(ctx):
    status, refused = envelope("DELETE", "/v1/workspace_templates/local-default")
    assert status == 403
    ctx.eval(DELETE_ENV + "REFUSE = new window.primerApi.ApiError(" + json.dumps(refused) + ");"
             " WT_deleteTemplate(env, { id: 'local-default' }).then(function (r) { OUT = r; });")
    told = js(ctx, "TOLD")
    assert told[0][0] == "Delete refused: " + js(ctx, "window.primerApi.readRefusal(REFUSE).message")
    assert "reserved workspace template" in told[0][0]
    assert told[0][1] == {"kind": "error", "requestId": refused["extensions"]["request_id"]}
    assert js(ctx, "OUT") is False and js(ctx, "REFETCHED") == 0
    ctx.eval(DELETE_ENV + "ANSWER = false; WT_deleteTemplate(env, { id: 'tpl-b' }).then(function (r) { OUT = r; });")
    assert js(ctx, "DELS") == [] and js(ctx, "OUT") is False


def test_the_card_menu_runs_the_delete(ctx):
    _mount(ctx, [PYTHON_DEV])
    ctx.eval("CARDS['python-dev'].menu[0].onClick();")
    settle(ctx)
    assert js(ctx, "CONFIRMS[0].title") == "Delete python-dev"
    assert [c for c in js(ctx, "CALLS") if c["method"] == "DELETE"] == [{"method": "DELETE", "path": "/workspace_templates/python-dev", "body": None}]
    assert js(ctx, "TOASTS") == [["Deleted python-dev", None]] and js(ctx, "RES['ws:templates'].refetched") == 1


# ---- every template, the count and the filter -----------------------------------------------------------------------------------------------------------


def test_more_than_200_templates_are_all_listed(ctx):
    rows = [{"id": f"t-{i:03d}", "description": f"row {i}", "provider_id": "loc", "backend": {"kind": "local"}} for i in range(250)]
    _mount(ctx, rows)
    header = js(ctx, "HEADER")
    assert header["pager"]["total"] == 250 and header["pager"]["pages"] == 21
    assert header["count"] == "250 workspace templates"
    assert header["title"] == "Workspace templates" and header["action"]["label"] == "+ Template" and header["action"]["testid"] == "wt-new"
    assert header["filter"]["label"] == "Filter workspace templates" and header["filter"]["testid"] == "wt-filter"
    assert len(js(ctx, "shownCards()")) == 12


def test_the_list_is_read_past_the_first_page(ctx):
    """The page's resource reads every page (WT_fetchAllTemplates), not the first 200."""
    _mount(ctx, [])
    ctx.eval(
        "HANDLER = function (m, path) { var o = Number(/offset=(\\d+)/.exec(path)[1]); var n = Math.max(0, Math.min(200, 230 - o)); var items = [];"
        " for (var i = 0; i < n; i++) items.push({ id: 'x' + (o + i) }); return Promise.resolve({ items: items }); };"
        "var GOT = null; RES['ws:templates'].fetcher(null).then(function (r) { GOT = r; });"
    )
    assert len(js(ctx, "GOT.items")) == 230
    assert [c["path"] for c in js(ctx, "CALLS") if c["method"] == "GET"] == [
        "/workspace_templates?limit=200&offset=0&order_by=id:asc", "/workspace_templates?limit=200&offset=200&order_by=id:asc"]


@pytest.mark.parametrize("q,shown", [("python-dev", ["python-dev"]), ("node 20", ["node-box"]), ("pod", ["node-box"]), ("podman", ["node-box"]),
                                     ("kubernetes", ["python-dev", "gpu-held"]), ("rt:1.4", ["python-dev", "gpu-held"]), ("TOLERATES", ["gpu-held"])])
def test_the_filter_matches_id_description_provider_kind_and_image(ctx, q, shown):
    _mount(ctx, [PYTHON_DEV, PODMAN, HELD])
    ctx.eval(f"HEADER.filter.onChange({json.dumps(q)}); MR.rerender();")
    assert js(ctx, "shownCards()") == shown
    assert js(ctx, "HEADER.count") == f"{len(shown)} of 3 workspace templates"


# ---- states -------------------------------------------------------------------------------------------------------------------------------------------


def test_loading_error_and_empty_states(ctx):
    _mount(ctx, None)
    assert ctx.eval("textOf(MR.find('wt-loading'))") == "Loading workspace templates…"
    ctx.eval("fail('ws:templates', { type: '/errors/internal', title: 'Internal Server Error', status: 500, detail: 'the store is down' });")
    _mount(ctx, None)
    text = ctx.eval("textOf(MR.find('wt-list-error'))")
    assert "Couldn't load workspace templates" in text and "the store is down" in text
    ctx.eval("MR.click('wt-retry');")
    assert js(ctx, "RES['ws:templates'].refetched") == 1
    _mount(ctx, [])
    assert ctx.eval("textOf(MR.find('wt-empty'))").startswith("No workspace templates yet.")
    create = js(ctx, "MR.find('wt-empty-create').props")
    assert create["data-verb"] == "template.create"
    _mount(ctx, [PYTHON_DEV])
    ctx.eval("HEADER.filter.onChange('zzz'); MR.rerender();")
    assert ctx.eval("textOf(MR.find('wt-empty'))").startswith('No workspace templates match "zzz".')
    ctx.eval("MR.click('wt-clear-filter');")
    assert js(ctx, "shownCards()") == ["python-dev"]


def test_a_load_error_with_rows_keeps_the_rows(ctx):
    _mount(ctx, [PYTHON_DEV])
    ctx.eval("fail('ws:templates', { type: '/errors/internal', title: 'Internal Server Error', status: 500, detail: 'blip' }); MR.rerender();")
    assert js(ctx, "shownCards()") == ["python-dev"] and ctx.eval("MR.find('wt-list-error') === null")


# ---- opening the overlay ------------------------------------------------------------------------------------------------------------------------------


def test_plus_template_and_the_verb_open_the_create_overlay(ctx):
    _mount(ctx, [PYTHON_DEV])
    assert ctx.eval("LAST_OVERLAY === null")
    ctx.eval("MR.click('wt-new');")
    assert js(ctx, "LAST_OVERLAY.title") == "New workspace template"
    ctx.eval("LAST_OVERLAY.onClose(); MR.rerender(); LAST_OVERLAY = null; MR.rerender();")
    assert ctx.eval("LAST_OVERLAY === null"), "closed"
    ctx.eval("MR.mount(function Elsewhere() { return null; }, {}); WT_requestCreate();")
    assert ctx.eval("WT_CREATE_REQUEST.pending") is True, "no page heard it: the request waits"
    _mount(ctx, [PYTHON_DEV])
    assert js(ctx, "LAST_OVERLAY.title") == "New workspace template", "a request made before the page mounted opens it on mount"
    assert ctx.eval("WT_consumeCreateRequest()") is False, "and is taken once"
    _mount(ctx, [PYTHON_DEV])
    assert ctx.eval("LAST_OVERLAY === null")
    ctx.eval("WT_requestCreate(); MR.rerender();")
    assert js(ctx, "LAST_OVERLAY.title") == "New workspace template", "a request while it is mounted opens it at once"
    assert ctx.eval("WT_consumeCreateRequest()") is False


def test_the_create_verb_leaves_an_open_edit_alone(ctx):
    """Round 2 of #886, item 4 (the reviewer's probe): the palette's Create Workspace Template while a template is open in the overlay used to replace it
    with the create overlay, and the unsaved edit was gone. An overlay that is open stays; the request is taken, so it does not open later either."""
    _mount(ctx, [PYTHON_DEV])
    ctx.eval("MR.click('pk-card-open:python-dev'); setValue('ws-template-description', 'unsaved'); WT_requestCreate(); MR.rerender();")
    assert js(ctx, "LAST_OVERLAY.title") == "Edit workspace template"
    assert js(ctx, "MR.find('ws-template-description').props.value") == "unsaved"
    assert ctx.eval("WT_consumeCreateRequest()") is False
    ctx.eval("LAST_OVERLAY.onClose(); MR.rerender(); LAST_OVERLAY = null; MR.rerender();")
    assert ctx.eval("LAST_OVERLAY === null"), "closing the edit does not open a create nobody asked for any more"


def test_the_page_stops_listening_when_it_goes(ctx):
    _mount(ctx, [PYTHON_DEV])
    assert js(ctx, "(LISTENERS['wt:create'] || []).length") == 1
    ctx.eval("MR.mount(function Empty() { return null; }, {});")
    assert js(ctx, "(LISTENERS['wt:create'] || []).length") == 0


def test_open_shows_the_template_overlay_for_that_row(ctx):
    _mount(ctx, [BUILT_IN, PYTHON_DEV])
    ctx.eval("MR.click('pk-card-open:python-dev');")
    assert js(ctx, "LAST_OVERLAY.title") == "Edit workspace template"
    assert js(ctx, "MR.find('ws-template-description').props.value") == "Python 3.12 with uv"
    ctx.eval("LAST_OVERLAY.onClose(); MR.rerender();")
    assert ctx.eval("MR.find('wt-overlay') === null")
    ctx.eval("MR.click('pk-card-open:local-default');")
    assert js(ctx, "LAST_OVERLAY.title") == "Workspace template" and ctx.eval("!!MR.find('wt-readonly-note')")


def test_the_page_reads_no_old_list(ctx):
    source = TEMPLATES_FILE.read_text(encoding="utf-8")
    page = source[source.index("function WT_TemplatesPage("):source.index("window.WT_TemplatesPage = WT_TemplatesPage;")]
    assert "WT_fetchAllTemplates" in page and "PK_usePagedFilter" in page and "usePagedList" not in page
    assert re.search(r"pollMs:\s*15000", page)
