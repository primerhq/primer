"""Providers > Artifact Store: the "Store new files in" selector (plan 09, Task 11; seam I-4), ``ui/components/artifact-store-selector.jsx``.

It reads ``GET /artifact_storage_active_config`` and every page of ``GET /artifact_storage_providers`` and writes ``PUT /artifact_storage_active_config``,
three admin-only routes. The lead's rulings (2026-10-24) it is held to:

1. Admin only: an admin gets the selector, anyone else nothing (no control and no request).
2. A refused selection shows the server's words under the field (``readRefusal``), never a code, and the select shows the stored store again.
3. While a selection is out the select is busy (``aria-busy``), never natively disabled, and a second change is ignored (no second request).
4. Loading, a load error with Retry (``as-active-store-retry``) and the empty case (only the database store) are all shown.
5. ``ui/foundation/capabilities.js`` maps the ``s3`` kind to the ``s3`` extra, so the provider form gives the install hint when it is missing.

Everything runs for real in V8 (``tests/ui/_register_harness.py``: the real ``api.js``, ``shared.jsx``, form field and kit) with only the network
and the console context stubbed. What the server answers is never hand-typed: ``served`` mounts the REAL artifact-storage routers on a FastAPI
app over the suite's in-memory storage and records their answers, the refusals included: a filesystem root whose marker is gone (a process
without the shared volume), an S3 row with half a key pair stored before the rule, a store deleted by someone else after the list was read, a
store deleted while it was checked, and a selection written by someone else meanwhile. The one envelope a test changes (the refusal with no
sentence) is the real one with its sentence taken out, and says so.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.ui._mini_react import transpile
from tests.ui._register_harness import COMPONENTS, UI, base_code, context, js, settle, transpiled, types_of

FILE = COMPONENTS / "artifact-store-selector.jsx"
SPEECH_ACTIVE = COMPONENTS / "providers" / "speech-active.jsx"
CAPABILITIES = UI / "foundation" / "capabilities.js"
ACTIVE_KEY = "as:active-store"
STORES_KEY = "as:stores"
PROVIDERS = "/v1/artifact_storage_providers"
ACTIVE = "/v1/artifact_storage_active_config"
DEFAULT = "artifact-storage-default"
LABEL = "Store new files in"
HELP = "New files go here. Files stored earlier stay where they are and can still be read."
NO_OTHER = "No other artifact store is registered. Register a filesystem or S3 store to send new files somewhere else."
NONE = "No artifact stores are registered."
REFUSED_TITLE = "Could not switch the artifact store"
LOAD_FAILED = "Could not load the artifact stores: "

REFUSALS = ("unusable_root", "half_pair", "unknown", "deleted", "changed")

STUBS = r"""
var CONSOLE = { role: "admin" };
function NV_useConsole() { return CONSOLE; }
function mount() { MR.mount(AS_ActiveStoreSelector, { rows: [], reload: function () {} }); }
function select() { return MR.find("as-active-store-select"); }
function choose(value) {
  var node = select();
  if (!node) throw new Error("no select");
  SETTLED = "pending";
  var out = node.props.onChange({ target: { value: value }, preventDefault: function () {}, stopPropagation: function () {} });
  Promise.resolve(out).then(function () { SETTLED = "fulfilled"; }, function () { SETTLED = "rejected"; });
  MR.rerender();
}
function options() {
  return ALL("as-active-store-select").filter(function (n) { return n.type === "option"; }).map(function (n) { return [n.props.value, textOf(n)]; });
}
function puts() { return CALLS.filter(function (c) { return c.method === "PUT"; }); }
var HELD = null;
function holdPut() {
  HANDLER = function () { return new Promise(function (resolve, reject) { HELD = { resolve: resolve, reject: reject }; }); };
}
function answerWith(data) { HANDLER = function () { return Promise.resolve(JSON.parse(JSON.stringify(data))); }; }
function refuseWith(envelope) { HANDLER = function () { return refuse(envelope); }; }
"""


# ---- what the real routes answer ---------------------------------------------------------------------------------------------------------


async def _serve(tmp: Path) -> dict[str, Any]:
    import httpx
    from fastapi import FastAPI

    from primer.api.errors import register_error_handlers
    from primer.api.registries import ProviderRegistry
    from primer.api.routers.artifact_storage import artifact_storage_active_config_router, artifact_storage_router
    from primer.artifact.filesystem import ROOT_MARKER
    from primer.artifact.probe import ArtifactProbeResult
    from primer.model.provider import ArtifactStorageProvider
    from primer.model.providers.artifact import ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, ActiveArtifactStorageConfig
    from tests.conftest import _FakeStorageProvider

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(artifact_storage_router, prefix="/v1")
    app.include_router(artifact_storage_active_config_router, prefix="/v1")
    sp = _FakeStorageProvider()
    app.state.storage_provider = sp
    app.state.provider_registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type]
    )
    rows = sp.get_storage(ArtifactStorageProvider)
    selection = sp.get_storage(ActiveArtifactStorageConfig)
    await rows.create(ArtifactStorageProvider(id=DEFAULT, provider="db"))  # the row the lifespan seeds

    async def deleted_meanwhile(row, *, storage_provider):
        await storage_provider.get_storage(ArtifactStorageProvider).delete(row.id)
        return ArtifactProbeResult(ok=True)

    async def changed_meanwhile(row, *, storage_provider):
        await storage_provider.get_storage(ActiveArtifactStorageConfig).update(
            ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id="fs-1"),
        )
        return ArtifactProbeResult(ok=True)

    async def database_down(*args, **kwargs):
        raise RuntimeError("the database is down")

    out: dict[str, Any] = {}

    async def call(c, method: str, path: str, want: int, body: dict | None = None) -> Any:
        r = await c.request(method, path, json=body)
        assert r.status_code == want, f"{method} {path}: {r.status_code} {r.text}"
        return r.json() if r.content else None

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://primer") as c:
        out["fresh_active"] = await call(c, "GET", ACTIVE, 200)
        out["only_default"] = await call(c, "GET", f"{PROVIDERS}?limit=200", 200)
        await call(c, "POST", PROVIDERS, 201, {"id": "fs-1", "provider": "filesystem", "config": {"root": str(tmp / "fs-1")}})
        await call(c, "POST", PROVIDERS, 201, {"id": "s3-1", "provider": "s3", "config": {
            "bucket": "media", "access_key": "AKIAEXAMPLE1234", "secret_key": "wJalrXUtnFEMI5678"}})
        out["active_fs"] = await call(c, "PUT", ACTIVE, 200, {"provider_id": "fs-1"})
        out["stores"] = await call(c, "GET", f"{PROVIDERS}?limit=200", 200)
        out["switched"] = await call(c, "PUT", ACTIVE, 200, {"provider_id": DEFAULT})
        out["active_default"] = await call(c, "GET", ACTIVE, 200)

        # The refusals. A row with half an S3 key pair cannot be written any more, so it is stored directly, as a row written before the rule.
        await rows.create(ArtifactStorageProvider.model_validate(
            {"id": "s3-half", "provider": "s3", "config": {"bucket": "media", "access_key": "AKIAHALFPAIR"}}))
        for store in ("gone", "db-2", "db-3"):
            await call(c, "POST", PROVIDERS, 201, {"id": store, "provider": "db", "config": {}})
        out["stale_list"] = await call(c, "GET", f"{PROVIDERS}?limit=200", 200)
        await call(c, "DELETE", f"{PROVIDERS}/gone", 204)
        (tmp / "fs-1" / ROOT_MARKER).unlink()  # the root as a process without the shared volume finds it
        out["unusable_root"] = await call(c, "PUT", ACTIVE, 422, {"provider_id": "fs-1"})
        out["half_pair"] = await call(c, "PUT", ACTIVE, 422, {"provider_id": "s3-half"})
        out["unknown"] = await call(c, "PUT", ACTIVE, 422, {"provider_id": "gone"})
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("primer.artifact.active.probe_artifact_provider", deleted_meanwhile)
            out["deleted"] = await call(c, "PUT", ACTIVE, 409, {"provider_id": "db-2"})
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("primer.artifact.active.probe_artifact_provider", changed_meanwhile)
            out["changed"] = await call(c, "PUT", ACTIVE, 409, {"provider_id": "db-3"})
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(selection, "get", database_down)
            out["load_failure"] = await call(c, "GET", ACTIVE, 500)

        for row in (await call(c, "GET", f"{PROVIDERS}?limit=200", 200))["items"]:
            await rows.delete(row["id"])  # the reserved row too, which only a direct delete can remove
        out["no_rows"] = await call(c, "GET", f"{PROVIDERS}?limit=200", 200)
    out["targets"] = {"unusable_root": "fs-1", "half_pair": "s3-half", "unknown": "gone", "deleted": "db-2", "changed": "db-3"}
    return out


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return asyncio.run(_serve(tmp_path_factory.mktemp("artifact-roots")))


@pytest.fixture(scope="module")
def code() -> str:
    return base_code() + "\n" + transpiled(FILE)


@pytest.fixture
def ctx(code):
    made = context(code)
    made.eval(STUBS)
    try:
        yield made
    finally:
        made.close()


def _serve_key(ctx, key: str, data: Any) -> None:
    ctx.eval(f"serve({json.dumps(key)}, {json.dumps(data)});")


def _load(ctx, active: Any, stores: Any) -> None:
    _serve_key(ctx, ACTIVE_KEY, active)
    _serve_key(ctx, STORES_KEY, stores)
    ctx.eval("mount();")


def _row(ctx) -> list[dict]:
    """The selector's kit field, in order: its label, the control(s), then the help and the error."""
    return js(ctx, "fieldRow('as-active-store', 'as-active-store-select')")


def _texts(ctx) -> str:
    return ctx.eval("textOf(MR.find('as-active-store'))")


def _field_error(ctx) -> str | None:
    alerts = js(ctx, "ALL('as-active-store').filter(function (n) { return n.props && n.props.role === 'alert'; }).map(function (n) { return textOf(n); })")
    assert len(alerts) <= 1, alerts
    return alerts[0] if alerts else None


# ---- what it shows ----------------------------------------------------------------------------------------------------------------------


def test_it_shows_the_active_store_and_every_store_with_its_kind(ctx, served) -> None:
    _load(ctx, served["active_fs"], served["stores"])
    assert ctx.eval("select().props.value") == "fs-1"
    assert js(ctx, "options()") == [
        [DEFAULT, f"{DEFAULT} (database)"],
        ["fs-1", "fs-1 (filesystem)"],
        ["s3-1", "s3-1 (S3)"],
    ]
    row = _row(ctx)
    assert [part["type"] for part in row] == ["label", "select", "div"]
    assert row[0]["text"] == LABEL and row[2]["text"] == HELP, "the kit field: the label alone, the select, the help under it"
    assert ctx.eval("select().props.disabled === undefined || select().props.disabled === false") is True
    assert _field_error(ctx) is None


def test_the_fetchers_read_the_active_store_and_the_store_list(ctx, served) -> None:
    _load(ctx, served["active_fs"], served["stores"])
    ctx.eval(f"RES[{json.dumps(ACTIVE_KEY)}].fetcher(null); RES[{json.dumps(STORES_KEY)}].fetcher(null);")
    assert js(ctx, "CALLS.map(function (c) { return [c.method, c.path]; })") == [
        ["GET", "/artifact_storage_active_config"],
        ["GET", "/artifact_storage_providers?limit=200&offset=0"],
    ]


def test_the_active_store_is_an_option_even_when_the_list_lacks_it(ctx, served) -> None:
    _load(ctx, served["active_fs"], served["only_default"])
    assert ctx.eval("select().props.value") == "fs-1"
    assert [value for value, _ in js(ctx, "options()")] == ["fs-1", DEFAULT]


def test_focusing_the_select_reads_the_store_list_again(ctx, served) -> None:
    """A store registered on the page shows up without a reload."""
    _load(ctx, served["active_fs"], served["stores"])
    ctx.eval("select().props.onFocus({}); MR.rerender();")
    assert ctx.eval(f"__entry({json.dumps(STORES_KEY)}).refetched") == 1
    assert ctx.eval(f"__entry({json.dumps(ACTIVE_KEY)}).refetched") == 0


def test_the_select_fits_a_phone(ctx, served) -> None:
    _load(ctx, served["active_fs"], served["stores"])
    assert js(ctx, "select().props.style") == {"width": "100%"}


# ---- ruling 4: loading, a load error with Retry, and the empty case -------------------------------------------------------------------


def test_while_loading_the_select_is_disabled_and_says_so(ctx) -> None:
    ctx.eval("mount();")
    assert ctx.eval("select().props.disabled") is True, "loading is not a request of its own: nothing to choose yet"
    assert js(ctx, "options()") == [["", "Loading stores"]]
    assert _row(ctx)[0]["text"] == LABEL
    assert ctx.eval("MR.find('as-active-store-retry')") is None


@pytest.mark.parametrize("which", [ACTIVE_KEY, STORES_KEY], ids=["active", "stores"])
def test_a_load_failure_says_why_and_offers_retry(ctx, served, which: str) -> None:
    other = STORES_KEY if which == ACTIVE_KEY else ACTIVE_KEY
    _serve_key(ctx, other, served["stores"] if other == STORES_KEY else served["active_fs"])
    ctx.eval(f"fail({json.dumps(which)}, {json.dumps(served['load_failure'])}); mount();")
    assert _field_error(ctx) == LOAD_FAILED + served["load_failure"]["detail"]
    assert ctx.eval("select()") is None, "no control that cannot work"
    retry = js(ctx, "nativeButton('as-active-store-retry') && textOf(nativeButton('as-active-store-retry'))")
    assert retry == "Retry"
    ctx.eval("press('as-active-store-retry');")
    assert ctx.eval(f"__entry({json.dumps(ACTIVE_KEY)}).refetched") == 1
    assert ctx.eval(f"__entry({json.dumps(STORES_KEY)}).refetched") == 1


def test_a_failed_reload_keeps_the_select_and_offers_retry(ctx, served) -> None:
    """The list was read, then reading it again (a focus) failed: the select still works, and the failure and Retry are shown."""
    _load(ctx, served["active_fs"], served["stores"])
    ctx.eval(f"fail({json.dumps(STORES_KEY)}, {json.dumps(served['load_failure'])}); MR.rerender();")
    assert ctx.eval("select().props.value") == "fs-1"
    assert _field_error(ctx) == LOAD_FAILED + served["load_failure"]["detail"]
    ctx.eval("press('as-active-store-retry');")
    assert ctx.eval(f"__entry({json.dumps(STORES_KEY)}).refetched") == 1


def test_with_only_the_database_store_it_says_there_is_no_other(ctx, served) -> None:
    _load(ctx, served["fresh_active"], served["only_default"])
    assert ctx.eval("select().props.value") == DEFAULT
    assert js(ctx, "options()") == [[DEFAULT, f"{DEFAULT} (database)"]]
    assert _row(ctx)[-1]["text"] == NO_OTHER


def test_with_no_stores_at_all_it_says_none_are_registered(ctx, served) -> None:
    assert served["no_rows"]["items"] == []
    _load(ctx, served["fresh_active"], served["no_rows"])
    assert js(ctx, "options()") == [[DEFAULT, DEFAULT]], "the active id is still named"
    assert _row(ctx)[-1]["text"] == NONE


# ---- switching ----------------------------------------------------------------------------------------------------------------------------


def test_choosing_a_store_saves_it_and_says_where_new_files_go(ctx, served) -> None:
    _load(ctx, served["active_fs"], served["stores"])
    ctx.eval(f"answerWith({json.dumps(served['switched'])}); choose({json.dumps(DEFAULT)});")
    assert settle(ctx) == "fulfilled"
    assert js(ctx, "puts()") == [{"method": "PUT", "path": "/artifact_storage_active_config", "body": {"provider_id": DEFAULT}}]
    assert js(ctx, "TOASTS") == [{"kind": "success", "title": f"New files now go to {DEFAULT}"}]
    assert ctx.eval(f"__entry({json.dumps(ACTIVE_KEY)}).refetched") == 1
    assert ctx.eval("select().props.value") == DEFAULT, "the server's answer, before the refetch lands"
    assert _field_error(ctx) is None


def test_choosing_the_store_already_in_use_sends_nothing(ctx, served) -> None:
    _load(ctx, served["active_fs"], served["stores"])
    ctx.eval("choose('fs-1');")
    assert js(ctx, "puts()") == []


@pytest.mark.parametrize("refusal", REFUSALS)
def test_a_refused_switch_shows_the_servers_words_and_the_stored_store(ctx, served, refusal: str) -> None:
    envelope = served[refusal]
    target = served["targets"][refusal]
    words = envelope["extensions"]["message"]
    _load(ctx, served["active_default"], served["stale_list"])
    assert target in [value for value, _ in js(ctx, "options()")], "the refused store was on offer"
    ctx.eval(f"refuseWith({json.dumps(envelope)}); choose({json.dumps(target)});")
    assert settle(ctx) == "fulfilled", "the component handles the refusal"

    assert js(ctx, "puts()") == [{"method": "PUT", "path": "/artifact_storage_active_config", "body": {"provider_id": target}}]
    assert ctx.eval("select().props.value") == DEFAULT, "the select shows the stored store again"
    row = _row(ctx)
    assert row[-1]["role"] == "alert" and row[-1]["text"] == words, "the server's words, under the field"
    assert envelope["extensions"]["error"] not in _texts(ctx), "never the code"
    assert js(ctx, "TOASTS") == [{"kind": "error", "title": REFUSED_TITLE, "detail": words}]
    assert ctx.eval(f"__entry({json.dumps(ACTIVE_KEY)}).refetched") == 1, "read the stored store again (someone else may have changed it)"
    assert ctx.eval(f"__entry({json.dumps(STORES_KEY)}).refetched") == 1, "read the list again (the store may be gone)"
    assert ctx.eval("select().props['aria-busy'] === undefined") is True


def test_a_refusal_without_a_sentence_never_shows_its_code(ctx, served) -> None:
    """The real 422 with its sentence taken out (no ``message``, no ``detail``): what is left is the code and the title, and only the title is said."""
    envelope = copy.deepcopy(served["unusable_root"])
    del envelope["extensions"]["message"]
    del envelope["detail"]
    _load(ctx, served["active_default"], served["stale_list"])
    ctx.eval(f"refuseWith({json.dumps(envelope)}); choose('fs-1');")
    assert settle(ctx) == "fulfilled"
    shown = _field_error(ctx)
    assert shown == envelope["title"]
    assert "artifact_store_unusable" not in _texts(ctx) and "artifact_store_unusable" not in json.dumps(js(ctx, "TOASTS"))


def test_a_later_switch_clears_the_refusal(ctx, served) -> None:
    _load(ctx, served["active_default"], served["stale_list"])
    ctx.eval(f"refuseWith({json.dumps(served['unknown'])}); choose('gone');")
    settle(ctx)
    assert _field_error(ctx) == served["unknown"]["extensions"]["message"]
    ctx.eval(f"answerWith({json.dumps(served['active_fs'])}); choose('fs-1');")
    assert settle(ctx) == "fulfilled"
    assert _field_error(ctx) is None
    assert ctx.eval("select().props.value") == "fs-1"


# ---- ruling 3: busy, not disabled; a second change while one is out is ignored -------------------------------------------------------


def test_while_a_switch_is_out_the_select_is_busy_not_disabled_and_ignores_a_second_change(ctx, served) -> None:
    _load(ctx, served["active_fs"], served["stores"])
    ctx.eval(f"holdPut(); choose({json.dumps(DEFAULT)});")
    assert len(js(ctx, "puts()")) == 1
    assert ctx.eval("select().props.disabled === undefined || select().props.disabled === false") is True, "a natively disabled select drops the focus"
    assert ctx.eval("select().props['aria-busy']") == "true"
    assert ctx.eval("select().props.value") == DEFAULT, "it shows the choice being saved"

    ctx.eval("choose('s3-1');")
    assert len(js(ctx, "puts()")) == 1, "no second request while one is out"
    assert ctx.eval("select().props.value") == DEFAULT

    ctx.eval(f"HELD.resolve({json.dumps(served['switched'])});")
    settle(ctx)
    assert ctx.eval("select().props['aria-busy'] === undefined") is True
    assert ctx.eval("select().props.value") == DEFAULT
    ctx.eval("answerWith({}); choose('s3-1');")
    assert len(js(ctx, "puts()")) == 2, "the next change is sent once the first is answered"


# ---- ruling 1: admin only -----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("console", [{"role": "user"}, {"role": "restricted"}, None], ids=["user", "restricted", "no-console"])
def test_anyone_but_an_admin_gets_nothing_and_no_request_is_made(ctx, served, console) -> None:
    ctx.eval(f"CONSOLE = {json.dumps(console)};")
    _load(ctx, served["active_fs"], served["stores"])
    assert ctx.eval("MR.find('as-active-store')") is None
    assert ctx.eval("select()") is None
    assert [key for key in js(ctx, "KEYS") if str(key).startswith("as:")] == [], "the admin-only routes are never asked"
    assert js(ctx, "CALLS") == []


def test_an_admin_gets_the_selector(ctx, served) -> None:
    _load(ctx, served["active_fs"], served["stores"])
    assert ctx.eval("MR.find('as-active-store') !== null") is True
    assert ctx.eval(f"KEYS.indexOf({json.dumps(ACTIVE_KEY)}) >= 0 && KEYS.indexOf({json.dumps(STORES_KEY)}) >= 0") is True


# ---- registration and loading -------------------------------------------------------------------------------------------------------------


def test_it_registers_as_the_artifact_storage_extra_without_replacing_another() -> None:
    """Loaded after the speech selectors (``ui/index.html`` order), and on top of an entry another file made first: every entry stays."""
    made = context(base_code() + "\n" + transpiled(SPEECH_ACTIVE, FILE))
    try:
        assert js(made, "Object.keys(window.PC_TYPE_EXTRAS).sort()") == ["artifact_storage", "stt", "tts"]
        assert made.eval("window.PC_TYPE_EXTRAS.artifact_storage === AS_ActiveStoreSelector") is True
        assert made.eval("window.AS_ActiveStoreSelector === AS_ActiveStoreSelector") is True
        assert js(made, "AS_KIND_LABELS") == {"db": "database", "filesystem": "filesystem", "s3": "S3"}
    finally:
        made.close()
    made = context(base_code() + "\nwindow.PC_TYPE_EXTRAS = { tts: function ExistingExtra() {} };\n" + transpiled(FILE))
    try:
        assert made.eval("window.PC_TYPE_EXTRAS.tts.name") == "ExistingExtra"
        assert made.eval("window.PC_TYPE_EXTRAS.artifact_storage === AS_ActiveStoreSelector") is True
    finally:
        made.close()


def test_index_html_loads_it_after_the_kit_and_right_before_the_catalog() -> None:
    scripts = re.findall(r'<script type="text/babel" src="([^"]+)"></script>', (UI / "index.html").read_text(encoding="utf-8"))
    at = scripts.index("components/artifact-store-selector.jsx")
    assert scripts[at + 1] == "components/provider-catalog.jsx"
    assert scripts.index("components/shared/platform-kit.jsx") < at
    assert scripts.index("components/shared.jsx") < at
    assert scripts.count("components/artifact-store-selector.jsx") == 1


# ---- ruling 5: the s3 kind names the s3 extra -------------------------------------------------------------------------------------------


def test_the_s3_kind_names_the_s3_extra_the_server_reports() -> None:
    from primer.common.optional import EXTRA_MODULES

    made = context(base_code())
    made.eval(transpile(CAPABILITIES))  # after the harness's stubs of the map and the hint, which it replaces with the real ones
    try:
        assert js(made, "window.primerApi.EXTRA_FOR_PROVIDER_TYPE") == {
            "huggingface": "huggingface", "lance": "lance", "container": "docker", "kubernetes": "kubernetes", "s3": "s3",
        }
    finally:
        made.close()
    assert "s3" in EXTRA_MODULES, "GET /v1/capabilities reports only EXTRA_MODULES: an extra it does not report reads as missing, and the hint would never go away"


@pytest.mark.parametrize("framed", [False, True], ids=["form", "register-overlay-frame"])
@pytest.mark.parametrize("installed", [False, True], ids=["missing", "installed"])
def test_the_provider_form_gives_the_install_hint_for_s3_only_when_it_is_missing(installed: bool, framed: bool) -> None:
    """The form draws the hint as a Banner's ``detail``: as ``children`` (which Banner never draws) only the title "Optional dependency required" showed, and
    no pip command, for every kind with an extra."""
    made = context(base_code())
    made.eval(transpile(CAPABILITIES))
    frame = ", frame: function (parts) { return el('div', { 'data-testid': 'frame' }, parts.body); }, classKey: 'artifact_storage', kindSelect: true" if framed else ""
    try:
        made.eval(
            f"window.primerApi.useCapabilities = function () {{ return {{ data: {{ extras: {{ s3: {{ installed: {json.dumps(installed)} }} }} }} }}; }};"
            f"serve(PC_typesKey('artifact_storage_providers'), {json.dumps(types_of('artifact_storage_providers'))});"
            "MR.mount(PC_ProviderForm, { plural: 'artifact_storage_providers', value: { id: 's3-new', provider: 's3', config: { bucket: 'media' } },"
            f" onChange: function () {{}}, onSubmit: function () {{ return Promise.resolve(); }}, onCancel: function () {{}}{frame} }});"
        )
        text = made.eval("textOf(MR.find('provider-form-artifact_storage_providers'))")
        hint = made.eval("window.primerApi.capabilityHint('s3')")
        assert "pip install 'primer-ai[s3]'" in hint
        assert (hint in text) is (not installed)
        assert ("Optional dependency required" in text) is (not installed)
    finally:
        made.close()
