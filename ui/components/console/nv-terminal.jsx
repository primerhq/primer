/* global React, NV_useConsole */
// The terminal panel (wiring plan P2 T8): per-workspace PTY over the
// terminal websocket, xterm.js render, VS Code-style bottom panel.
// Admin by default; the per-workspace user-access toggle governs the
// rest - a refused socket renders the denied state, never a spinner.

// F8(b): top-edge drag-resize, clamped 100px-70vh (design). window.innerHeight
// is read at drag time (not cached) so a viewport resize between opens still
// clamps correctly.
var NV_TERMINAL_MIN_HEIGHT = 100;
var NV_TERMINAL_DEFAULT_HEIGHT = 260;

function NV_terminalMaxHeight() {
  return Math.round(window.innerHeight * 0.7);
}

function NV_Terminal() {
  var con = NV_useConsole();
  var hostRef = React.useRef(null);
  var stateRef = React.useRef({ term: null, ws: null, fit: null });
  // F8(a): the WS close CODE is the refusal signal (already distinct
  // server-side - terminal.py closes 4403 for the role/access gate, every
  // other close - auth/missing-workspace/internal-error/a bare network
  // blip - is NOT a denial). The old code inferred "denied" from ANY
  // close-before-open, which is a lie on a blip: it rendered the
  // permanent "disabled for this workspace" copy for a transient drop.
  // Two distinct, mutually exclusive states now; a retry only makes sense
  // for the transient one.
  var deniedState = React.useState(false);
  var denied = deniedState[0];
  var setDenied = deniedState[1];
  var connLostState = React.useState(false);
  var connLost = connLostState[0];
  var setConnLost = connLostState[1];
  var exitState = React.useState(null);
  var exitCode = exitState[0];
  var setExit = exitState[1];
  var retryState = React.useState(0);
  var retryToken = retryState[0];
  var setRetryToken = retryState[1];
  var heightState = React.useState(NV_TERMINAL_DEFAULT_HEIGHT);
  var height = heightState[0];
  var setHeight = heightState[1];

  var ws = (con.workspaces || []).find(function (w) { return w.id === con.wid; });
  // uiv2 Wave 1: the mockup's header names the workspace ID short form
  // ("ws-3f8a9bc"), not the display name other rail/files headers
  // prefer - implementer-notes 2.6's own literal example, kept as-is
  // rather than switched to name-preferred for cross-file consistency.
  var wsLabel = (ws && ws.id) || con.wid;

  React.useEffect(function () {
    if (!hostRef.current || !window.Terminal) return undefined;
    setDenied(false);
    setConnLost(false);
    setExit(null);
    // Guards against the close event our OWN cleanup's sock.close() fires
    // below: without it, tearing down for a retry or an unmount would
    // read as "connection lost" on the very socket we intentionally
    // closed, and a stale effect's close (a retry starts a brand new
    // effect instance before the old socket's close event even fires)
    // could stomp the NEW instance's state right after it just cleared it.
    var cancelled = false;
    // A clean shell exit sends {"exit": code} then closes normally - that
    // is expected termination, not a lost connection, and must keep
    // showing the transcript + exit badge rather than the retry empty
    // state. React state read inside onclose's closure would still see
    // the pre-connect value (the closure was created once, above), so
    // this is a plain local the two handlers share within one effect run.
    var gotExit = false;
    // uiv2 Wave 1: the mockup's terminal content sits flush on the
    // console theme, not xterm's own default black viewport (xterm.min.
    // css hardcodes .xterm-viewport background-color: #000, overridden
    // per-panel below via .nv-terminal-host .xterm-viewport - CSS
    // custom properties resolve to real color strings at construction
    // time here since xterm's theme option needs actual values, not
    // var() references, and switching theme requires a fresh Terminal
    // instance anyway (this effect already reruns per con.wid/retry,
    // not per theme change - a live theme toggle mid-session keeps the
    // colors it opened with, same as fontFamily/fontSize above).
    var rootStyle = getComputedStyle(document.documentElement);
    var termAccent = rootStyle.getPropertyValue("--accent").trim() || "#61d46a";
    var termText = rootStyle.getPropertyValue("--text").trim() || "#e7e7e7";
    var term = new window.Terminal({
      fontFamily: "IBM Plex Mono, monospace",
      fontSize: 12,
      theme: {
        background: "transparent",
        foreground: termAccent,
        cursor: termAccent,
        cursorAccent: termText,
        selectionBackground: rootStyle.getPropertyValue("--accent-dim").trim() || undefined,
      },
    });
    var fit = window.FitAddon ? new window.FitAddon.FitAddon() : null;
    if (fit) term.loadAddon(fit);
    term.open(hostRef.current);
    if (fit) fit.fit();

    var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    var sock = new WebSocket(
      proto + "//" + window.location.host
      + "/v1/workspaces/" + encodeURIComponent(con.wid) + "/terminal"
      + "?cols=" + term.cols + "&rows=" + term.rows
    );
    sock.binaryType = "arraybuffer";
    sock.onmessage = function (ev) {
      if (typeof ev.data === "string") {
        try {
          var msg = JSON.parse(ev.data);
          if (msg && typeof msg.exit === "number") {
            gotExit = true;
            setExit(msg.exit);
          }
        } catch (_e) { /* control noise */ }
        return;
      }
      term.write(new Uint8Array(ev.data));
    };
    sock.onclose = function (ev) {
      if (cancelled || gotExit) return;
      if (ev && ev.code === 4403) {
        setDenied(true);
      } else {
        // Every OTHER close - auth/missing-workspace/internal-error close
        // codes and a bare network blip (browsers report an abnormal drop
        // as code 1006) alike - reads as "connection lost", never as a
        // permanent per-workspace refusal.
        setConnLost(true);
      }
    };
    term.onData(function (data) {
      if (sock.readyState === 1) sock.send(new TextEncoder().encode(data));
    });
    term.onResize(function (size) {
      if (sock.readyState === 1) {
        sock.send(JSON.stringify({ resize: { cols: size.cols, rows: size.rows } }));
      }
    });
    var obs = null;
    if (window.ResizeObserver && fit) {
      obs = new ResizeObserver(function () { fit.fit(); });
      obs.observe(hostRef.current);
    }
    stateRef.current = { term: term, ws: sock, fit: fit };
    return function () {
      cancelled = true;
      if (obs) obs.disconnect();
      try { sock.close(); } catch (_e) { /* closing */ }
      term.dispose();
    };
  }, [con.wid, retryToken]);

  // F8(b): top-edge drag. Dragging the handle UP grows the panel (the
  // panel's own top edge moves up), matching a VS Code-style bottom panel.
  var dragRef = React.useRef(null);
  // Cross-review MEDIUM: the drag's own window listeners used to be
  // removed only by their own mouseup. Closing the panel (or any other
  // unmount) mid-drag left them attached to window forever - the next
  // mousemove anywhere would call the by-then-disposed xterm fit
  // addon's .fit(), which throws. Track the live onMove/onUp pair here
  // so the unmount effect below can tear down a still-active drag too.
  var dragCleanupRef = React.useRef(null);
  var startResize = function (downEvent) {
    downEvent.preventDefault();
    var startY = downEvent.clientY;
    var startHeight = height;
    var onMove = function (moveEvent) {
      var delta = startY - moveEvent.clientY;
      var next = startHeight + delta;
      var max = NV_terminalMaxHeight();
      if (next < NV_TERMINAL_MIN_HEIGHT) next = NV_TERMINAL_MIN_HEIGHT;
      if (next > max) next = max;
      setHeight(next);
      if (stateRef.current.fit) stateRef.current.fit.fit();
    };
    var onUp = function () {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      dragCleanupRef.current = null;
    };
    dragCleanupRef.current = onUp;
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };
  React.useEffect(function () {
    return function () {
      if (dragCleanupRef.current) dragCleanupRef.current();
    };
  }, []);

  var retry = function () {
    setDenied(false);
    setConnLost(false);
    setRetryToken(function (n) { return n + 1; });
  };

  return (
    <div className="nv-terminal" data-testid="nv-terminal" style={{ height: height + "px" }}>
      <div
        ref={dragRef}
        className="nv-terminal-resize-handle"
        data-testid="nv-terminal-resize-handle"
        onMouseDown={startResize}
      />
      <div className="nv-trace-head">
        <span>Terminal</span>
        <span className="nv-rail-section-ws mono">{wsLabel} · pty</span>
        {exitCode != null ? (
          <span className="nv-term-exit">exit {exitCode}</span>
        ) : null}
        <span style={{ flex: 1 }} />
        <button type="button" className="nv-rail-iconbtn"
          data-testid="nv-terminal-close"
          onClick={function () {
            var verb = con.registry.get("terminal.toggle");
            if (verb) verb.run();
          }}>×</button>
      </div>
      {denied ? (
        <div className="nv-rail-empty" data-testid="nv-terminal-denied">
          <div>
            The terminal is disabled for this workspace. An admin can
            enable per-workspace user access on the workspace's settings.
          </div>
        </div>
      ) : connLost ? (
        <div className="nv-rail-empty" data-testid="nv-terminal-conn-lost">
          <div>Connection lost. This is usually transient.</div>
          <button type="button" className="nv-rail-iconbtn"
            data-testid="nv-terminal-retry"
            onClick={retry}>Retry</button>
        </div>
      ) : (
        <div className="nv-terminal-host" ref={hostRef} />
      )}
    </div>
  );
}

window.NV_Terminal = NV_Terminal;
