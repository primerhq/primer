/* global React, NV_useConsole */
// The terminal panel (wiring plan P2 T8): per-workspace PTY over the
// terminal websocket, xterm.js render, VS Code-style bottom panel.
// Admin by default; the per-workspace user-access toggle governs the
// rest - a refused socket renders the denied state, never a spinner.

function NV_Terminal() {
  var con = NV_useConsole();
  var hostRef = React.useRef(null);
  var stateRef = React.useRef({ term: null, ws: null, fit: null });
  var deniedState = React.useState(false);
  var denied = deniedState[0];
  var setDenied = deniedState[1];
  var exitState = React.useState(null);
  var exitCode = exitState[0];
  var setExit = exitState[1];

  React.useEffect(function () {
    if (!hostRef.current || !window.Terminal) return undefined;
    var term = new window.Terminal({
      fontFamily: "IBM Plex Mono, monospace",
      fontSize: 12,
      theme: { background: "transparent" },
    });
    var fit = window.FitAddon ? new window.FitAddon.FitAddon() : null;
    if (fit) term.loadAddon(fit);
    term.open(hostRef.current);
    if (fit) fit.fit();

    var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    var ws = new WebSocket(
      proto + "//" + window.location.host
      + "/v1/workspaces/" + encodeURIComponent(con.wid) + "/terminal"
      + "?cols=" + term.cols + "&rows=" + term.rows
    );
    ws.binaryType = "arraybuffer";
    var opened = false;
    ws.onopen = function () { opened = true; };
    ws.onmessage = function (ev) {
      if (typeof ev.data === "string") {
        try {
          var msg = JSON.parse(ev.data);
          if (msg && typeof msg.exit === "number") setExit(msg.exit);
        } catch (_e) { /* control noise */ }
        return;
      }
      term.write(new Uint8Array(ev.data));
    };
    ws.onclose = function () {
      if (!opened) setDenied(true);
    };
    term.onData(function (data) {
      if (ws.readyState === 1) ws.send(new TextEncoder().encode(data));
    });
    term.onResize(function (size) {
      if (ws.readyState === 1) {
        ws.send(JSON.stringify({ resize: { cols: size.cols, rows: size.rows } }));
      }
    });
    var obs = null;
    if (window.ResizeObserver && fit) {
      obs = new ResizeObserver(function () { fit.fit(); });
      obs.observe(hostRef.current);
    }
    stateRef.current = { term: term, ws: ws, fit: fit };
    return function () {
      if (obs) obs.disconnect();
      try { ws.close(); } catch (_e) { /* closing */ }
      term.dispose();
    };
  }, [con.wid]);

  return (
    <div className="nv-terminal" data-testid="nv-terminal">
      <div className="nv-trace-head">
        <span>terminal · {con.wid}</span>
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
      ) : (
        <div className="nv-terminal-host" ref={hostRef} />
      )}
    </div>
  );
}

window.NV_Terminal = NV_Terminal;
