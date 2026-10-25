/* global React, NV_useConsole, SH_api */
// The desktop canvas panel (pivot P5, Task 20). The agent's canvas is a live,
// shared part of the session: an agent draws it with canvas_apply, a person
// clicks and chooses on it, and the agent reads what they did. The panel is the
// desktop home for that surface - a split above the chat, with the frame (the
// sandboxed iframe the renderer draws into) and the bridge (the MessageChannel
// the console and the renderer talk over).
//
// Security (the lead's S8 rulings, binding):
//   * the frame is sandboxed to an OPAQUE origin - never same-origin (ruling 4).
//     Its only script control is the /canvas/frame route's CSP, so the raw copy
//     of the frame's source under the console origin must not be reachable
//     (/console/canvas/* answers 404, primer/api/_app_middleware.py).
//   * run_html is the console's word that THIS viewer may run the canvas's Html
//     at once: true ONLY for the session's creator with strict_mode off (ruling
//     1). Every other viewer gets every Html element inert, behind a Run button.
//   * the console accepts NOTHING from the canvas frame tree on window after the
//     port is up (ruling 2). The renderer frame and its Html sub-frames can all
//     postMessage to this (top) window; none of it is the data channel (that is
//     the MessagePort). Before the handshake, only the renderer's own `ready`
//     reply, and only from the renderer frame's own window, is accepted.

// --- pure seams (run in MiniRacer, tests/ui/test_canvas_panel.py) ---------------

// Which way the panel sits: an explicit state wins (the URL's when the doc is
// the focused one, else the doc's local state); otherwise it is automatic -
// shown when the canvas has elements, hidden on a phone.
function NV_canvasVisibility(p) {
  p = p || {};
  var explicit = (p.urlState != null) ? p.urlState : p.localState;
  if (explicit === "open") return "shown";
  if (explicit === "closed") return "hidden";
  if (p.mobile) return "hidden";
  return (p.elementCount > 0) ? "shown" : "hidden";
}

// The panel/transcript split, as a percentage of the height the panel takes.
// Clamped to 20..80 (a sliver and a takeover are both useless), persisted in
// localStorage, default 50.
var NV_CANVAS_SPLIT_KEY = "primer.canvas.split";
function NV_canvasClampSplit(pct) {
  var n = (typeof pct === "number" && isFinite(pct)) ? pct : 50;
  if (n < 20) return 20;
  if (n > 80) return 80;
  return n;
}
function NV_canvasSplitGet() {
  var raw = null;
  try { raw = window.localStorage.getItem(NV_CANVAS_SPLIT_KEY); } catch (e) { raw = null; }
  var n = (raw === null || raw === undefined) ? 50 : parseFloat(raw);
  return NV_canvasClampSplit(n);
}
function NV_canvasSplitSet(pct) {
  var c = NV_canvasClampSplit(pct);
  try { window.localStorage.setItem(NV_CANVAS_SPLIT_KEY, String(c)); } catch (e) { /* private mode */ }
  return c;
}

// The frame->console message shapes (ui/canvas/renderer.js's header): `ready`,
// `event`, `open_link`, `copy`, `error`. Every inbound message is validated and
// dropped when it does not fit - the size cap is the 16 KiB the renderer itself
// enforces on the way out.
var CV_FRAME_MAX_BYTES = 16 * 1024;
var CV_ID_RE = /^[a-z][a-z0-9_]{0,31}$/;
var CV_EVENT_KINDS = ["click", "change", "emit", "set_value", "play", "pause", "ended", "open"];
function CV_isFrameId(v) { return typeof v === "string" && CV_ID_RE.test(v); }
function NV_canvasValidateFrameMessage(msg) {
  if (!msg || typeof msg !== "object" || typeof msg.type !== "string") return null;
  if (JSON.stringify(msg).length > CV_FRAME_MAX_BYTES) return null;
  var t = msg.type;
  if (t === "ready") {
    return (msg.protocol === 1) ? { type: "ready", protocol: 1 } : null;
  }
  if (t === "event") {
    if (!CV_isFrameId(msg.element)) return null;
    if (CV_EVENT_KINDS.indexOf(msg.kind) < 0) return null;
    var out = { type: "event", element: msg.element, kind: msg.kind };
    if (msg.value !== undefined) out.value = msg.value;
    if (typeof msg.name === "string") out.name = msg.name;
    if (msg.data !== undefined) out.data = msg.data;
    if (typeof msg.position === "number") out.position = msg.position;
    return out;
  }
  if (t === "open_link") {
    if (typeof msg.url !== "string" || !msg.url) return null;
    var o = { type: "open_link", url: msg.url };
    if (msg.element !== undefined) {
      if (!CV_isFrameId(msg.element)) return null;
      o.element = msg.element;
    }
    return o;
  }
  if (t === "copy") {
    if (!CV_isFrameId(msg.element)) return null;
    if (typeof msg.text !== "string" || msg.text.length > 10000) return null;
    return { type: "copy", element: msg.element, text: msg.text };
  }
  if (t === "error") {
    if (typeof msg.message !== "string" || msg.message.length > 500) return null;
    return { type: "error", message: msg.message };
  }
  return null;
}

// The server's words for a refused canvas event, the way a person reads them.
// 409 and 429 say it plainly (the server's message); a 422 unknown_element is
// the canvas having changed under the click; everything else is a generic
// "couldn't send that". Both read the refusal through the foundation's one
// reader (window.primerApi.readRefusal, ui/foundation/api.js): the server's
// words and the code come from there, never out of the envelope directly.
function NV_canvasProblemText(err, fallback) {
  return window.primerApi.readRefusal(err, fallback || "the request failed").message;
}
function NV_canvasEventErrorText(err) {
  var refusal = window.primerApi.readRefusal(err, "the request failed");
  var status = err && err.status;
  if (status === 409 || status === 429) return refusal.message;
  if (status === 422 && refusal.code === "unknown_element") return "That control is gone: the canvas changed.";
  return "Couldn't send that to the agent: " + refusal.message;
}

// Ruling 1 (inert HTML): run_html is true ONLY when the viewer created the
// session AND strict_mode allows it (is off). The identity rule is the same
// type-and-id match the server's rank floor uses (shell-attention.js). Every
// other viewer - an admin viewing a user's session, a shared-prime reader - gets
// every Html element inert, behind a Run button.
function NV_canvasIsCreator(viewer, session) {
  if (!viewer || !session) return false;
  var c = session.initiated_by;
  if (!c) return false;
  return viewer.type === c.type && viewer.id === c.id;
}
function NV_canvasRunHtml(viewer, session, settings) {
  if (!NV_canvasIsCreator(viewer, session)) return false;
  if (settings && settings.strict_mode) return false;
  return true;
}

// The theme the frame draws with, read from the console's own design tokens so
// the canvas matches the surrounding surface. `readVar` is a getter for one CSS
// custom property (the component hands it getComputedStyle on :root).
function NV_canvasThemeVars(readVar) {
  function g(name) { try { return (readVar && readVar(name)) || ""; } catch (e) { return ""; } }
  return {
    "--cv-bg": g("--bg-1"), "--cv-surface": g("--bg-2"), "--cv-fg": g("--text"),
    "--cv-muted": g("--text-3"), "--cv-accent": g("--accent"), "--cv-accent-fg": g("--accent-fg"),
    "--cv-danger": g("--danger"), "--cv-border": g("--border"), "--cv-radius": g("--r-8"),
    "--cv-font": g("--font-ui"), "--cv-mono": g("--font-mono"),
  };
}

// Ruling 2 (window messages): the decision for the console's window `message`
// listener. `ev.source` must be the renderer frame's own window (an Html
// sub-frame, or any other frame, is dropped at once); after the handshake
// (handshakeDone) the port owns everything and window is dead; before it, only
// the renderer's `ready` reply is accepted. Returns "ready" or null.
function NV_canvasWindowMessage(ev, frameWindow, handshakeDone) {
  if (!ev || !frameWindow) return null;
  if (ev.source !== frameWindow) return null;
  if (handshakeDone) return null;
  var m = NV_canvasValidateFrameMessage(ev.data);
  return (m && m.type === "ready") ? "ready" : null;
}

// The best-effort host of a URL, for the "Open link?" dialog (no DOM in the
// harness, so a plain match, not a URL parse).
function NV_canvasHostOf(url) {
  var m = /^https?:\/\/([^\/]+)/i.exec(String(url || ""));
  return m ? m[1] : String(url || "");
}

// Each panel writes its view here (a plain module global, like NV_SESSION_INSTANCES)
// so the verbs' `available` predicates can read whether the focused session's
// canvas is shown and how many elements it holds.
var NV_CANVAS_VIEWS = {};
function NV_canvasViewFor(sid) {
  return NV_CANVAS_VIEWS[sid] || { shown: false, count: 0 };
}

// The doc's local open/closed state, held when the doc is NOT the focused one
// (the focused doc's state lives in the URL).
var NV_CANVAS_LOCAL = {};

// --- the verbs (the palette entry + the tab-menu pointer affordance) -----------

function NV_registerCanvasVerbs(registry, focused) {
  function reg(v) { if (!registry.get(v.id)) registry.register(v); }
  reg({
    id: "canvas.show", label: "Show Canvas",
    contexts: ["session"], surfaces: ["palette", "tab-menu"],
    available: function (c) { return !!(c && c.session) && !NV_canvasViewFor(c.session.id).shown; },
    run: function () {
      var f = focused(); if (!f) return;
      NV_CANVAS_LOCAL[f.sid] = "open";
      if (f.con && f.con.setCanvas) f.con.setCanvas("open");
    },
  });
  reg({
    id: "canvas.hide", label: "Hide Canvas",
    contexts: ["session"], surfaces: ["palette", "tab-menu"],
    available: function (c) { return !!(c && c.session) && NV_canvasViewFor(c.session.id).shown; },
    run: function () {
      var f = focused(); if (!f) return;
      NV_CANVAS_LOCAL[f.sid] = "closed";
      if (f.con && f.con.setCanvas) f.con.setCanvas("closed");
    },
  });
  reg({
    id: "canvas.clear", label: "Clear Canvas",
    contexts: ["session"], surfaces: ["palette", "tab-menu"], destructive: true,
    available: function (c) { return !!(c && c.session) && NV_canvasViewFor(c.session.id).count > 0; },
    run: function () {
      var f = focused(); if (!f) return;
      window.confirmDialog({
        title: "Clear the canvas?",
        message: "The agent's elements and the user's values are removed. The agent sees an empty canvas on its next read.",
        confirmLabel: "Clear", danger: true,
      }).then(function (ok) {
        if (!ok) return;
        SH_api.canvasClear(f.wid, f.sid).then(function () {
          f.con.toast("Canvas cleared");
          if (f.refetchAll) f.refetchAll();
        }, function (err) {
          f.con.toast("Couldn't clear the canvas: " + NV_canvasEventErrorText(err));
        });
      });
    },
  });
}

// --- the frame (the sandboxed iframe + the bridge) ------------------------------

function NV_CanvasFrame(props) {
  var con = NV_useConsole();
  var iframeRef = React.useRef(null);
  var portRef = React.useRef(null);
  var readyRef = React.useRef(false);
  var dataRef = React.useRef(props.data);
  dataRef.current = props.data;
  var sessionRef = React.useRef(props.session);
  sessionRef.current = props.session;

  function uuid() {
    return (typeof crypto !== "undefined" && crypto.randomUUID)
      ? crypto.randomUUID()
      : "c-" + Date.now() + "-" + Math.floor(Math.random() * 1e9);
  }

  // The console->frame `state`: what the canvas is (rev, elements, values), the
  // settings, the minted file URLs, the theme, and run_html (ruling 1).
  function pushState() {
    var port = portRef.current;
    var d = dataRef.current;
    if (!port || !d) return;
    port.postMessage({
      type: "state",
      rev: d.rev,
      elements: d.elements,
      values: d.values,
      settings: d.settings,
      file_urls: d.file_urls,
      own_origin: (window.location && window.location.origin) || "",
      theme: window.NV_canvasThemeVars(function (name) {
        try {
          return (typeof getComputedStyle !== "undefined" && document.documentElement)
            ? getComputedStyle(document.documentElement).getPropertyValue(name) : "";
        } catch (e) { return ""; }
      }),
      run_html: window.NV_canvasRunHtml(con.viewer, sessionRef.current, d.settings),
    });
  }

  function postEvent(body) {
    SH_api.canvasEvent(props.wid, props.sid, body).then(
      function () { /* a 202: recorded (or a deduped repeat) */ },
      function (err) { con.toast(window.NV_canvasEventErrorText(err)); }
    );
  }

  // A validated frame->console message, off the port.
  function onPortMessage(ev) {
    var m = window.NV_canvasValidateFrameMessage(ev && ev.data);
    if (!m) return;
    if (m.type === "ready") { readyRef.current = true; pushState(); return; }
    if (m.type === "event") {
      postEvent({
        element: m.element, kind: m.kind,
        value: m.value, name: m.name, data: m.data, position: m.position,
        client_event_id: uuid(),
      });
      // Tell the panel a person just acted: it refetches the snapshot now rather
      // than waiting for the tap's canvas frame to move the store's channel.
      if (typeof props.onEvent === "function") props.onEvent(m);
      return;
    }
    if (m.type === "open_link") {
      window.confirmDialog({
        title: "Open link?", message: window.NV_canvasHostOf(m.url) + "\n" + m.url, confirmLabel: "Open",
      }).then(function (ok) {
        if (!ok) return;
        window.open(m.url, "_blank", "noopener,noreferrer");
        // A native Link element's open is worth the agent's knowing; an Html
        // element's open_link carries no element (the server requires one), so it
        // is just opened, not recorded.
        if (m.element) postEvent({ element: m.element, kind: "open", client_event_id: uuid() });
      });
      return;
    }
    if (m.type === "copy") {
      var text = m.text;
      // The copy is a toast with an action: the click on the action is the user
      // activation that the clipboard write needs.
      window.primerApi.toastPush({
        kind: "info", text: "Copy text from the canvas",
        actions: [{
          label: "Copy",
          run: function () {
            if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
              navigator.clipboard.writeText(text).then(
                function () { con.toast("Copied"); },
                function () { /* the clipboard is unavailable; the toast is the record */ }
              );
            }
          },
        }],
      });
      return;
    }
    if (m.type === "error") { con.toast(m.message); return; }
  }

  // The console's window `message` listener (ruling 2). It exists only to be
  // dropped-by-default: everything the canvas tree can post to this (top) window
  // is refused unless it is the renderer's own `ready` reply, from the renderer
  // frame's window, before the handshake.
  function onWindowMessage(ev) {
    var frame = iframeRef.current;
    if (!frame || !frame.contentWindow) return;
    if (window.NV_canvasWindowMessage(ev, frame.contentWindow, readyRef.current) === "ready") {
      readyRef.current = true;
    }
  }

  // The bridge: a fresh MessageChannel per load, the handshake posted to the
  // renderer frame's window (port1 transferred), then everything over the port.
  React.useEffect(function () {
    var frame = iframeRef.current;
    if (!frame || !frame.contentWindow || typeof window.MessageChannel !== "function") return undefined;
    var channel = new window.MessageChannel();
    var port = channel.port2;
    portRef.current = port;
    port.onmessage = onPortMessage;
    window.addEventListener("message", onWindowMessage);
    try {
      frame.contentWindow.postMessage({ type: "primer.canvas.connect", protocol: 1 }, "*", [channel.port1]);
    } catch (e) { /* the frame may not be ready to receive yet */ }
    return function () {
      window.removeEventListener("message", onWindowMessage);
      try { port.close(); } catch (e) { /* already closed */ }
    };
  }, [props.data && props.data.frame_url]);

  // Re-post state whenever the snapshot the frame draws changes.
  React.useEffect(function () {
    if (readyRef.current) pushState();
  }, [
    props.data && props.data.rev,
    props.data && props.data.event_seq,
    props.data && JSON.stringify(props.data.settings),
    props.data && JSON.stringify(props.data.file_urls),
  ]);

  return (
    <iframe
      ref={iframeRef}
      className="nv-canvas-frame"
      data-testid="nv-canvas-frame"
      title="Agent canvas"
      src={props.data ? props.data.frame_url : ""}
      sandbox="allow-scripts allow-forms"
      allow="autoplay"
      referrerPolicy="no-referrer" />
  );
}

// --- the panel -----------------------------------------------------------------

function NV_CanvasPanel(props) {
  var con = NV_useConsole();
  var sid = props.sid;
  var wid = props.wid;
  var focused = !!props.focused;
  var mobile = window.primerApi.useViewport().isMobile;
  // The session store's canvas channel is a REFRESH signal (the tap's CanvasFrame
  // {rev, event_seq}), not data: the panel refetches the snapshot when it moves.
  var canvasChannel = window.useSessionStore(wid, sid, "canvas");
  var res = window.primerApi.useResource(
    SH_api.keys.canvas(sid),
    function (signal) { return SH_api.canvas(wid, sid, signal); },
    { deps: [sid, wid, canvasChannel && canvasChannel.rev, canvasChannel && canvasChannel.eventSeq], ignoreIdle: true }
  );
  var tickState = React.useState(0);
  var setTick = tickState[1];
  // The panel/transcript split (a % of the height the panel takes), persisted
  // (NV_canvasSplitGet/Set); the resize handle below moves it.
  var splitState = React.useState(window.NV_canvasSplitGet());
  var split = splitState[0];
  var setSplit = splitState[1];
  function changeSplit(next) {
    var c = window.NV_canvasClampSplit(next);
    window.NV_canvasSplitSet(c);
    setSplit(c);
  }
  function onResizeKey(ev) {
    // A horizontal separator moves vertically: up grows the panel, down shrinks it.
    if (ev.key === "ArrowUp" || ev.key === "ArrowRight") { ev.preventDefault(); changeSplit(split + 5); }
    else if (ev.key === "ArrowDown" || ev.key === "ArrowLeft") { ev.preventDefault(); changeSplit(split - 5); }
  }
  var data = res.data;
  var count = (data && data.element_count) || 0;
  // An explicit state wins: the URL's when this doc is the focused one, else the
  // doc's local state (the URL belongs to the focused doc only).
  var urlState = focused ? (con.canvas || null) : null;
  var localState = NV_CANVAS_LOCAL[sid] || null;
  var shown = window.NV_canvasVisibility({
    urlState: urlState, localState: localState, elementCount: count, mobile: mobile,
  }) === "shown";
  // Written for the verbs' `available` predicates (a plain module global).
  NV_CANVAS_VIEWS[sid] = { shown: shown, count: count };

  var loading = !!res.loading && !data;
  var errored = !!res.error && !data;

  function toggle() {
    var next = shown ? "closed" : "open";
    NV_CANVAS_LOCAL[sid] = next;
    if (focused && con.setCanvas) con.setCanvas(next);
    setTick(function (t) { return t + 1; });
  }

  // The sheet's close: back to null (automatic), not "closed" - the phone falls
  // back to the chip-driven flow rather than pinning itself shut.
  function closeSheet() {
    NV_CANVAS_LOCAL[sid] = null;
    if (focused && con.setCanvas) con.setCanvas(null);
    setTick(function (t) { return t + 1; });
  }

  // The body the split and the sheet both hold: loading, error, empty, or the
  // frame (with the bridge).
  function body() {
    if (loading) {
      return (
        <div className="nv-canvas-state" data-testid="nv-canvas-loading">Loading canvas…</div>
      );
    }
    if (errored) {
      return (
        <div className="nv-canvas-state nv-canvas-state-error" data-testid="nv-canvas-error">
          <span>The canvas could not load: {NV_canvasProblemText(res.error)}</span>
          <button type="button" className="nv-btn-secondary"
            data-testid="nv-canvas-error-retry"
            onClick={function () { if (res.refetch) res.refetch(); }}>Try again</button>
        </div>
      );
    }
    if (count === 0) {
      return (
        <div className="nv-canvas-state" data-testid="nv-canvas-empty">
          Nothing on the canvas yet. An agent draws here with canvas_apply.
        </div>
      );
    }
    return (
      <NV_CanvasFrame sid={sid} wid={wid} session={props.session} data={data}
        onEvent={function () { if (res.refetch) res.refetch(); }} />
    );
  }

  if (!shown) return null;
  if (mobile) {
    // A phone gets a top sheet (70vh, pinned at the top) instead of the split:
    // the chip in the header opens it, its close button returns to automatic.
    return (
      <div className="nv-canvas-sheet" data-testid="nv-canvas-sheet">
        <div className="nv-canvas-sheet-head">
          <span className="nv-canvas-title">Canvas</span>
          {data ? <span className="nv-canvas-rev">rev {data.rev}</span> : null}
          <span style={{ flex: 1 }} />
          <button type="button" className="nv-canvas-sheet-close"
            data-testid="nv-canvas-sheet-close" data-verb="canvas.hide"
            aria-label="Close canvas" onClick={closeSheet}>×</button>
        </div>
        <div className="nv-canvas-sheet-body">{body()}</div>
      </div>
    );
  }
  return (
    <div className="nv-canvas" data-testid="nv-canvas" style={{ height: split + "%" }}>
      <div className="nv-canvas-head">
        <span className="nv-canvas-title">Canvas</span>
        {data ? <span className="nv-canvas-rev">rev {data.rev}</span> : null}
        <span style={{ flex: 1 }} />
        <button type="button" className="nv-canvas-hide"
          data-testid="nv-canvas-hide" data-verb="canvas.hide"
          aria-label="Hide canvas" onClick={toggle}>×</button>
      </div>
      <div className="nv-canvas-body">{body()}</div>
      <div className="nv-canvas-resize" data-testid="nv-canvas-resize"
        role="separator" aria-orientation="horizontal"
        aria-valuemin={20} aria-valuemax={80} aria-valuenow={split}
        tabIndex={0}
        onKeyDown={onResizeKey}
        onDoubleClick={function () { changeSplit(50); }}
        title="Resize the canvas (arrows, or double-click to reset)">
        <span aria-hidden="true">⋯</span>
      </div>
    </div>
  );
}

// --- the phone chip -------------------------------------------------------------

// The affordance a phone gets in the session header (the desktop gets the split
// and the overflow menu): a button that names the canvas and its element count,
// and opens the top sheet. Shown by the header only while the canvas has
// elements or is open.
function NV_CanvasChip(props) {
  return (
    <button type="button" className="nv-canvas-chip"
      data-testid="nv-canvas-chip" data-verb="canvas.show"
      onClick={props.onOpen}>
      {"Canvas · " + props.count}
    </button>
  );
}

// --- the admin's settings dialog (pivot P5, Task 23) ------------------------------------------

// The five limits (Task 5) and their bounds, from the CanvasSettings model.
function NV_canvasSettingsBounds() {
  return [
    { key: "max_elements", label: "Most elements", min: 1, max: 1000 },
    { key: "max_html_bytes", label: "Most bytes of one Html body", min: 1024, max: 262144 },
    { key: "max_canvas_bytes", label: "Most bytes of a whole canvas", min: 16384, max: 4194304 },
    { key: "max_events", label: "Events kept for the agent", min: 10, max: 1000 },
    { key: "max_waking_events_per_minute", label: "Waking events per minute", min: 1, max: 120 },
  ];
}

// The field errors of a 422, keyed by field name: the thrown ApiError's fieldErrors (the
// RFC 7807 envelope's extensions.errors), read off the error - never off err.envelope (the
// one-reader rule). Each entry's loc's last named segment (after the body/query/path
// prefix) is the field.
function NV_canvasSettingsFieldErrors(err) {
  var errors = err && Array.isArray(err.fieldErrors) ? err.fieldErrors : [];
  var out = {};
  for (var i = 0; i < errors.length; i++) {
    var e = errors[i];
    var loc = e && Array.isArray(e.loc) ? e.loc : [];
    var field = null;
    for (var j = loc.length - 1; j >= 0; j--) {
      var seg = loc[j];
      if (typeof seg === "string" && seg !== "body" && seg !== "query" && seg !== "path") { field = seg; break; }
    }
    if (field && e && typeof e.msg === "string") out[field] = e.msg;
  }
  return out;
}

// The admin's canvas settings dialog (pivot P5, Task 23): the media policy, the media
// allowlist, strict mode and the limits, on the platform kit. Save sends the WHOLE row
// (every field round-trips - the master's Review Focus 1); a 422 shows each field's error
// under its own control.
function NV_CanvasSettingsDialog(props) {
  var con = NV_useConsole();
  var res = window.primerApi.useResource(
    "nv-canvas-settings",
    function (signal) { return SH_api.canvasSettings(signal); },
    { deps: [] }
  );
  var loaded = res && res.data;
  var formState = React.useState(null);
  var form = formState[0], setForm = formState[1];
  var feState = React.useState(null);
  var fieldErrors = feState[0], setFieldErrors = feState[1];
  var svState = React.useState(false);
  var saving = svState[0], setSaving = svState[1];

  // Initialise the form when the settings arrive: the allowlist becomes the textarea's
  // one-host-per-line text (split back into hosts on save).
  React.useEffect(function () {
    if (loaded) setForm(Object.assign({}, loaded, { media_allowlist: (loaded.media_allowlist || []).join("\n") }));
  }, [loaded]);

  function patch(part) {
    setForm(function (f) { return f ? Object.assign({}, f, part) : f; });
  }

  function save() {
    if (!form) return;
    var allowlist = String(form.media_allowlist || "").split("\n")
      .map(function (s) { return s.trim(); }).filter(Boolean);
    var body = {
      agent_tools_enabled: !!form.agent_tools_enabled,
      media_policy: form.media_policy,
      media_allowlist: allowlist,
      strict_mode: !!form.strict_mode,
      max_elements: Math.round(Number(form.max_elements)),
      max_html_bytes: Math.round(Number(form.max_html_bytes)),
      max_canvas_bytes: Math.round(Number(form.max_canvas_bytes)),
      max_events: Math.round(Number(form.max_events)),
      max_waking_events_per_minute: Math.round(Number(form.max_waking_events_per_minute)),
    };
    setSaving(true);
    SH_api.putCanvasSettings(body).then(
      function () { con.toast("Canvas settings saved"); props.onClose(); },
      function (err) { setSaving(false); setFieldErrors(NV_canvasSettingsFieldErrors(err)); }
    );
  }

  // loading and error states inside the overlay: the form is null until the settings
  // arrive, so neither a loaded form nor (with an error) a retry is drawn until then.
  if (!form) {
    return (
      <window.PK_FormOverlay title="Canvas settings" onClose={props.onClose} testid="canvas-settings">
        {res && res.error ? (
          <div className="nv-canvas-settings-state" data-testid="canvas-settings-error">
            <span>{window.NV_canvasProblemText(res.error, "The canvas settings could not load.")}</span>
            <button type="button" className="nv-btn-secondary" data-testid="canvas-settings-retry"
              onClick={function () { if (res.refetch) res.refetch(); }}>Try again</button>
          </div>
        ) : (
          <div className="nv-canvas-settings-state" data-testid="canvas-settings-loading">Loading the canvas settings…</div>
        )}
      </window.PK_FormOverlay>
    );
  }

  var mediaOptions = [
    { value: "any-https", label: "Any https" },
    { value: "allowlist", label: "Listed hosts" },
    { value: "none", label: "No external media" },
  ];
  var bounds = NV_canvasSettingsBounds();

  return (
    <window.PK_FormOverlay title="Canvas settings" onClose={props.onClose} testid="canvas-settings"
      footer={{ right: (
        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" className="nv-btn-secondary" data-testid="canvas-settings-cancel"
            onClick={props.onClose}>Cancel</button>
          <Btn kind="primary" data-testid="canvas-settings-save" busy={saving} onClick={save}>
            {saving ? "Saving…" : "Save"}
          </Btn>
        </div>
      ) }}>
      <window.PK_Field label="Media policy" help="Where a canvas may load external images and audio from."
        error={fieldErrors && fieldErrors.media_policy ? fieldErrors.media_policy : null}>
        <window.PK_Segmented testid="canvas-settings-media" ariaLabel="Media policy"
          options={mediaOptions} value={form.media_policy}
          onChange={function (v) { patch({ media_policy: v }); }} />
      </window.PK_Field>
      {form.media_policy === "allowlist" ? (
        <window.PK_Field label="Allowed hosts" span="full" help="One host per line (a host name or *.domain)."
          error={fieldErrors && fieldErrors.media_allowlist ? fieldErrors.media_allowlist : null}>
          <textarea className="nv-canvas-settings-allowlist" data-testid="canvas-settings-allowlist"
            value={form.media_allowlist} rows={3}
            onChange={function (ev) { patch({ media_allowlist: ev.target.value }); }} />
        </window.PK_Field>
      ) : null}
      <window.PK_Field label="Strict mode"
        error={fieldErrors && fieldErrors.strict_mode ? fieldErrors.strict_mode : null}>
        <label className="nv-canvas-settings-strict">
          <input type="checkbox" data-testid="canvas-settings-strict"
            checked={!!form.strict_mode}
            onChange={function (ev) { patch({ strict_mode: ev.target.checked }); }} />
          <span>Run Html only after a click</span>
        </label>
      </window.PK_Field>
      <window.PK_Section title="LIMITS" testid="canvas-settings-limits" cols={2}>
        {bounds.map(function (b) {
          return (
            <window.PK_Field key={b.key} label={b.label}
              error={fieldErrors && fieldErrors[b.key] ? fieldErrors[b.key] : null}>
              <input type="number" className={"nv-canvas-settings-limit nv-canvas-settings-limit-" + b.key}
                data-testid={"canvas-settings-limit:" + b.key}
                min={b.min} max={b.max} value={form[b.key]}
                onChange={function (ev) { patch({ [b.key]: ev.target.value }); }} />
            </window.PK_Field>
          );
        })}
      </window.PK_Section>
    </window.PK_FormOverlay>
  );
}

// --- window exports --------------------------------------------------------------
window.NV_canvasVisibility = NV_canvasVisibility;
window.NV_canvasClampSplit = NV_canvasClampSplit;
window.NV_canvasSplitGet = NV_canvasSplitGet;
window.NV_canvasSplitSet = NV_canvasSplitSet;
window.NV_canvasValidateFrameMessage = NV_canvasValidateFrameMessage;
window.NV_canvasProblemText = NV_canvasProblemText;
window.NV_canvasEventErrorText = NV_canvasEventErrorText;
window.NV_canvasIsCreator = NV_canvasIsCreator;
window.NV_canvasRunHtml = NV_canvasRunHtml;
window.NV_canvasThemeVars = NV_canvasThemeVars;
window.NV_canvasWindowMessage = NV_canvasWindowMessage;
window.NV_canvasHostOf = NV_canvasHostOf;
window.NV_CANVAS_VIEWS = NV_CANVAS_VIEWS;
window.NV_canvasViewFor = NV_canvasViewFor;
window.NV_registerCanvasVerbs = NV_registerCanvasVerbs;
window.NV_CanvasPanel = NV_CanvasPanel;
window.NV_CanvasFrame = NV_CanvasFrame;
window.NV_CanvasChip = NV_CanvasChip;
window.NV_CanvasSettingsDialog = NV_CanvasSettingsDialog;
// The docs' local open/closed state (exported so the session header's phone chip
// can set it when the doc is not the focused one; the focused doc's state lives
// in the URL).
window.NV_CANVAS_LOCAL = NV_CANVAS_LOCAL;
