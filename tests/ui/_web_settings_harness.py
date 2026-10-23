"""MiniRacer harness for Platform Web settings page and section unit tests.

Loads in order:
- ui/foundation/api.js (raw)
- ui/foundation/use-resource.js (raw)
- ui/components/shared.jsx (transpiled)
- ui/components/shared/form-field.jsx (transpiled)
- ui/components/shared/pager.jsx (transpiled)
- ui/components/shared/platform-kit.jsx (transpiled)
- Spy snippet 1: wraps kit components
- ui/components/web-settings.jsx (transpiled)
- Spy snippet 2: wraps WEB_ActiveConfigSection
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from primer.api.errors import register_error_handlers
from primer.api.routers.web_search import web_search_active_config_router
from tests.toolset.test_system import _SP
from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"


def _make_test_client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(web_search_active_config_router, prefix="/v1")
    app.state.storage_provider = _SP()
    return TestClient(app)


def real_unknown_ids_envelope(ids: list[str]) -> dict[str, Any]:
    """422: PUT /v1/web_search_active_config with aggregated ids and empty storage."""
    client = _make_test_client()
    resp = client.put(
        "/v1/web_search_active_config",
        json={"config": {"mode": "aggregated", "provider_ids": ids}},
    )
    assert resp.status_code == 422
    return resp.json()


def real_not_bootstrapped_envelope() -> dict[str, Any]:
    """503: GET /v1/web_search_active_config with no row."""
    client = _make_test_client()
    resp = client.get("/v1/web_search_active_config")
    assert resp.status_code == 503
    return resp.json()


PRELUDE = r"""
var __layers = [];
var MOBILE = false;
var ROLE = "admin";
var CALLS = [];
var TOASTS = [];
var NAV = [];
var MANAGE = [];
var ASKED_KEYS = [];
var __RESOURCES = {};
var __REFETCHES = {};
var __ANSWERS = {};
var __HOLD = {};
var __SPY = {};

var document = {
  addEventListener: function () {},
  removeEventListener: function () {},
  body: { style: {} },
  activeElement: null
};
window.addEventListener = function () {};
window.removeEventListener = function () {};

function __setResource(key, data, errorEnvelope, loading) {
  var err = errorEnvelope ? new window.primerApi.ApiError(errorEnvelope) : null;
  __RESOURCES[key] = {
    data: data !== undefined ? data : undefined,
    error: err,
    loading: !!loading,
    degraded: false
  };
}

function __resource(key) {
  return {
    get data() {
      var r = __RESOURCES[key];
      return r !== undefined ? r.data : undefined;
    },
    get error() {
      var r = __RESOURCES[key];
      return r ? r.error : null;
    },
    get loading() {
      var r = __RESOURCES[key];
      return r ? r.loading : false;
    },
    get degraded() {
      var r = __RESOURCES[key];
      return r ? r.degraded : false;
    },
    refetch: function () {
      __REFETCHES[key] = (__REFETCHES[key] || 0) + 1;
    }
  };
}

function __recordSpy(name, key, props) {
  if (!__SPY[name]) __SPY[name] = {};
  __SPY[name][key] = props;
}

function __wrapSpy(name, orig) {
  return function (props) {
    var p = props || {};
    var key = p.testid || p.id || p.label || p.title || p.kind || "_";
    __recordSpy(name, key, p);
    return orig.apply(this, arguments);
  };
}

function __cleanProps(obj, seen) {
  if (obj === null || obj === undefined) return obj;
  if (typeof obj === "function") return undefined;
  if (typeof obj === "object" && obj.__el === true) return "<element>";
  if (typeof obj !== "object") return obj;
  if (!seen) seen = [];
  if (seen.indexOf(obj) >= 0) return "<circular>";
  seen.push(obj);
  if (Array.isArray(obj)) {
    return obj.map(function (x) { return __cleanProps(x, seen); });
  }
  var res = {};
  for (var k in obj) {
    if (Object.prototype.hasOwnProperty.call(obj, k)) {
      var val = __cleanProps(obj[k], seen);
      if (val !== undefined) res[k] = val;
    }
  }
  return res;
}

function __getSpyProps(name, key) {
  if (!__SPY[name] || !__SPY[name][key]) return null;
  return __cleanProps(__SPY[name][key]);
}

function __callSpy(name, key, handlerPath, args) {
  var props = __SPY[name] && __SPY[name][key];
  if (!props) throw new Error("no spy props for " + name + " " + key);
  var parts = handlerPath.split(".");
  var target = props;
  for (var i = 0; i < parts.length - 1; i++) {
    target = target[parts[i]];
    if (!target) throw new Error("cannot resolve " + parts[i] + " in " + handlerPath);
  }
  var fn = target[parts[parts.length - 1]];
  if (typeof fn !== "function") throw new Error("handler " + handlerPath + " is not a function");
  return fn.apply(null, args);
}

function textOf(node) {
  var out = [];
  (function collect(n) {
    if (n == null || typeof n === "boolean") return;
    if (typeof n === "string" || typeof n === "number") { out.push(String(n)); return; }
    if (Array.isArray(n)) { n.forEach(collect); return; }
    if (n.__el) collect(typeof n.type === "function" && n.type !== React.Fragment ? n.out : n.children);
  })(node);
  return out.join("");
}

function __getOptions(testid) {
  var node = MR.find(testid);
  if (!node) return [];
  var out = [];
  function collect(n) {
    if (!n || typeof n !== "object") return;
    if (Array.isArray(n)) { n.forEach(collect); return; }
    if (n.type === "option") {
      out.push({
        value: (n.props && n.props.value != null) ? String(n.props.value) : "",
        text: textOf(n)
      });
      return;
    }
    if (n.__el) {
      if (typeof n.type === "function" && n.type !== React.Fragment) collect(n.out);
      else collect(n.children);
    }
  }
  collect(node.children);
  return out;
}

function __findEl(testid) {
  var node = MR.find(testid);
  if (!node) return null;
  var p = node.props || {};
  return {
    type: typeof node.type === "string" ? node.type : "component",
    value: p.value !== undefined ? p.value : null,
    disabled: !!p.disabled,
    "aria-busy": p["aria-busy"] !== undefined ? String(p["aria-busy"]) : null,
    "aria-label": p["aria-label"] || null,
    className: p.className || "",
    testid: p["data-testid"] || null,
  };
}

function __change(testid, val) {
  var el = MR.find(testid);
  if (!el) throw new Error("no element with data-testid " + testid);
  if (!el.props || typeof el.props.onChange !== "function") throw new Error("element has no onChange: " + testid);
  el.props.onChange({
    target: { value: val },
    preventDefault: function () {},
    stopPropagation: function () {},
  });
}
"""

KIT_SPY = r"""
PK_PageHeader = __wrapSpy("PK_PageHeader", PK_PageHeader);
PK_Section = __wrapSpy("PK_Section", PK_Section);
PK_Grid = __wrapSpy("PK_Grid", PK_Grid);
PK_Field = __wrapSpy("PK_Field", PK_Field);
PK_Segmented = __wrapSpy("PK_Segmented", PK_Segmented);
PK_CheckTable = __wrapSpy("PK_CheckTable", PK_CheckTable);
PK_CardGrid = __wrapSpy("PK_CardGrid", PK_CardGrid);
PK_Card = __wrapSpy("PK_Card", PK_Card);
"""

AFTER_WEB_SETTINGS = r"""
if (typeof WEB_ActiveConfigSection !== "undefined") {
  WEB_ActiveConfigSection = __wrapSpy("WEB_ActiveConfigSection", WEB_ActiveConfigSection);
}
"""


class WebUI:
    def __init__(self, role: str = "admin", mobile: bool = False, overlay_loaded: bool = True) -> None:
        self.role = role
        self.mobile = mobile
        self.overlay_loaded = overlay_loaded

        # Read foundation scripts
        api_js = (UI / "foundation" / "api.js").read_text(encoding="utf-8")
        use_res_js = (UI / "foundation" / "use-resource.js").read_text(encoding="utf-8")

        # Transpile components
        shared_jsx = transpile(UI / "components" / "shared.jsx")
        field_jsx = transpile(UI / "components" / "shared" / "form-field.jsx")
        pager_jsx = transpile(UI / "components" / "shared" / "pager.jsx")
        kit_jsx = transpile(UI / "components" / "shared" / "platform-kit.jsx")

        # web-settings.jsx (must raise FileNotFoundError if not found)
        web_settings_path = UI / "components" / "web-settings.jsx"
        web_settings_jsx = transpile(web_settings_path)

        # Wire fakes
        fakes_js = f"""
ROLE = {json.dumps(role)};
MOBILE = {json.dumps(mobile)};

window.primerApi.useResource = function (key, fetcher, opts) {{
  if (key != null) {{
    ASKED_KEYS.push(key);
    return __resource(key);
  }}
  return {{
    data: undefined,
    error: null,
    loading: false,
    degraded: false,
    refetch: function () {{}}
  }};
}};

window.primerApi.apiFetch = function (method, path, body) {{
  var entry = [method, path, body !== undefined ? body : null];
  CALLS.push(entry);
  var key = method + " " + path;
  if (__HOLD[key]) {{
    return new Promise(function () {{}});
  }}
  if (__ANSWERS[key]) {{
    var ans = __ANSWERS[key];
    if (ans.hold) return new Promise(function () {{}});
    if (ans.envelope) return Promise.reject(new window.primerApi.ApiError(ans.envelope));
    return Promise.resolve(ans.body != null ? ans.body : {{ ok: true }});
  }}
  return Promise.resolve({{ ok: true }});
}};

window.primerApi.toastPush = function (t) {{
  TOASTS.push(t);
}};

window.primerApi.useViewport = function () {{
  return {{ isMobile: !!MOBILE }};
}};

window.primerApi.useEscape = function () {{}};
window.primerApi.useFocusTrap = function () {{}};

NV_useConsole = function () {{
  return {{
    role: ROLE,
    goView: function () {{
      NAV.push([].slice.call(arguments));
    }}
  }};
}};
"""

        overlay_js = """
window.PC_RegisterOverlay = function (props) {
  __recordSpy("PC_RegisterOverlay", "_", props);
  return React.createElement("div", { "data-testid": "fake-register-overlay" });
};
""" if overlay_loaded else """
delete window.PC_RegisterOverlay;
"""

        code = "\n".join([
            api_js,
            use_res_js,
            shared_jsx,
            field_jsx,
            pager_jsx,
            kit_jsx,
            fakes_js,
            overlay_js,
            KIT_SPY,
            web_settings_jsx,
            AFTER_WEB_SETTINGS,
        ])

        self.ctx = mini_react_context(code, PRELUDE)

    def close(self) -> None:
        if hasattr(self, "ctx") and self.ctx:
            self.ctx.close()
            self.ctx = None

    def settle(self) -> None:
        if self.ctx:
            for _ in range(4):
                self.ctx.eval("void 0;")
                self.ctx.eval("MR.rerender();")

    def set_resource(
        self,
        key: str,
        *,
        data: Any = None,
        error_envelope: Any = None,
        loading: bool = False,
    ) -> None:
        self.ctx.eval(
            f"__setResource({json.dumps(key)}, {json.dumps(data)}, {json.dumps(error_envelope)}, {json.dumps(loading)});"
        )

    def answer(
        self,
        method_path: str,
        *,
        body: Any = None,
        envelope: Any = None,
        hold: bool = False,
    ) -> None:
        self.ctx.eval(
            f"__ANSWERS[{json.dumps(method_path)}] = "
            f"{{ body: {json.dumps(body)}, envelope: {json.dumps(envelope)}, hold: {json.dumps(hold)} }};"
        )
        if hold:
            self.ctx.eval(f"__HOLD[{json.dumps(method_path)}] = true;")

    def mount(self, component: str, props_js: str = "{}") -> None:
        self.ctx.eval(f"MR.mount({component}, {props_js});")
        self.settle()

    def rerender(self) -> None:
        self.ctx.eval("MR.rerender();")
        self.settle()

    def js(self, expr: str) -> Any:
        raw = self.ctx.eval(f"JSON.stringify({expr})")
        return json.loads(raw) if raw is not None else None

    def kit(self, name: str, key: str) -> dict[str, Any] | None:
        return self.js(f"__getSpyProps({json.dumps(name)}, {json.dumps(key)})")

    def call(self, name: str, key: str, handler: str, *args: Any) -> None:
        args_js = json.dumps(list(args))
        self.ctx.eval(
            f"__callSpy({json.dumps(name)}, {json.dumps(key)}, {json.dumps(handler)}, {args_js});"
        )
        self.settle()

    def click(self, testid: str) -> None:
        self.ctx.eval(f"MR.click({json.dumps(testid)});")
        self.settle()

    def change(self, testid: str, value: str) -> None:
        self.ctx.eval(f"__change({json.dumps(testid)}, {json.dumps(value)});")
        self.settle()

    def find(self, testid: str) -> dict[str, Any] | None:
        return self.js(f"__findEl({json.dumps(testid)})")

    def options(self, testid: str) -> list[dict[str, str]]:
        return self.js(f"__getOptions({json.dumps(testid)})")

    def texts(self) -> list[str]:
        return self.js("MR.texts()")

    def calls(self) -> list[list[Any]]:
        return self.js("CALLS")

    def toasts(self) -> list[dict[str, Any]]:
        return self.js("TOASTS")

    def refetched(self) -> dict[str, int]:
        return self.js("__REFETCHES") or {}

    def asked_keys(self) -> list[str]:
        return self.js("ASKED_KEYS") or []

    def nav(self) -> list[list[Any]]:
        return self.js("NAV") or []

    def overlay(self) -> dict[str, Any] | None:
        has_overlay = self.js("MR.find('fake-register-overlay') !== null")
        if not has_overlay:
            return None
        return self.kit("PC_RegisterOverlay", "_")


@pytest.fixture
def web_ui():
    created: list[WebUI] = []

    def make(role: str = "admin", mobile: bool = False, overlay_loaded: bool = True) -> WebUI:
        ui = WebUI(role=role, mobile=mobile, overlay_loaded=overlay_loaded)
        created.append(ui)
        return ui

    yield make

    for ui in created:
        ui.close()
