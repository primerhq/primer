/* global React, Icon */
// TerminalPanel — collapsible bottom panel for the Studio IDE shell (PR-B /
// P7). Spans the center column under StudioCenter's active panel (studio.jsx
// mounts this below StudioCenter inside .st-col-center, only while
// studio.state.terminalOpen is true — Ctrl-` / the header's terminal-toggle
// button flip that flag).
//
// Server contract (LOCKED — see docs/superpowers/specs/2026-07-01-studio-
// design.md §6.5): one cookie-authenticated WS per terminal tab —
//   WS /v1/workspaces/{wid}/terminal?cols=<n>&rows=<n>
// BINARY frames carry raw bytes (client→server: stdin; server→client: PTY
// output). TEXT frames carry JSON control: client → {"resize":{cols,rows}};
// server → {"exit":<code>} then closes. Unauthenticated → close code 4401.
// No reconnect in v1 (ephemeral terminals) — on close/error we just show the
// state and let the operator open a fresh tab via "+".
//
// Component split:
//   TerminalPanel        — header strip (tabs, +, connection dot, collapse)
//                           + the body that hosts one ST_TerminalInstance
//                           per open tab.
//   ST_TerminalInstance   — owns ONE tab's xterm.js Terminal + FitAddon + WS
//                           for its whole lifetime. Kept mounted (hidden via
//                           CSS, not unmounted) while its tab isn't active so
//                           switching tabs doesn't lose scrollback. Keyed by
//                           `wid + ":" + tab.id` so a workspace switch forces
//                           a fresh mount (tearing down the old WS/PTY even
//                           though tab ids like "bash" repeat across
//                           workspaces).
//
// No-build scope rules (see studio.jsx): top-level declarations use `var`;
// helpers are prefixed ST_; every exported symbol is assigned to window.X at
// the bottom. This file also avoids arrow functions / const / destructured
// params inside function bodies to match the surrounding studio-*.jsx style.

// ---------------------------------------------------------------------------
// ST_termWsUrl — ws(s)://<host>/v1/workspaces/<wid>/terminal?cols=&rows=
// Mirrors the scheme-selection pattern in chats.jsx's chat WS (~L748).
// ---------------------------------------------------------------------------

function ST_termWsUrl(wid, cols, rows) {
  var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return proto + "//" + window.location.host + "/v1/workspaces/" + encodeURIComponent(wid) +
    "/terminal?cols=" + encodeURIComponent(cols) + "&rows=" + encodeURIComponent(rows);
}

// ---------------------------------------------------------------------------
// ST_xtermTheme — resolve the Studio's oklch design tokens (styles.css
// :root[data-theme]) into an xterm.js theme object. Read live (not cached)
// so a dark/light toggle can be re-applied to already-open terminals.
// ---------------------------------------------------------------------------

function ST_xtermTheme() {
  var css = window.getComputedStyle(document.documentElement);
  function v(name, fallback) {
    var val = css.getPropertyValue(name);
    return val && val.trim() ? val.trim() : fallback;
  }
  return {
    background: v("--bg", "#15161a"),
    foreground: v("--text", "#e8e8ea"),
    cursor: v("--accent", "#5fd97a"),
    cursorAccent: v("--bg", "#15161a"),
    selectionBackground: v("--accent-border", "rgba(95,217,122,0.3)"),
    black: v("--bg-2", "#232428"),
    red: v("--red", "#e0605a"),
    green: v("--green", "#5fd97a"),
    yellow: v("--amber", "#d9a95f"),
    blue: v("--blue", "#5f9ed9"),
    magenta: v("--violet", "#a25fd9"),
    cyan: v("--teal", "#5fd9c8"),
    white: v("--text-2", "#b6b6ba"),
    brightBlack: v("--text-4", "#6a6a70"),
    brightRed: v("--red", "#e0605a"),
    brightGreen: v("--green", "#5fd97a"),
    brightYellow: v("--amber", "#d9a95f"),
    brightBlue: v("--blue", "#5f9ed9"),
    brightMagenta: v("--violet", "#a25fd9"),
    brightCyan: v("--teal", "#5fd9c8"),
    brightWhite: v("--text", "#e8e8ea"),
  };
}

// ---------------------------------------------------------------------------
// ST_nextTermId — "term-<n>" with n one past the highest existing suffix, so
// closing + reopening tabs never collides with a still-open tab's id.
// ---------------------------------------------------------------------------

function ST_nextTermId(tabs) {
  var max = 0;
  (tabs || []).forEach(function (t) {
    var m = /^term-(\d+)$/.exec(t && t.id);
    if (m) max = Math.max(max, parseInt(m[1], 10));
  });
  return "term-" + (max + 1);
}

// ---------------------------------------------------------------------------
// ST_TermConnDot — the runtime-connection indicator. data-testid encodes the
// live state (terminal-conn-connecting|live|closed|error) per state.
// ---------------------------------------------------------------------------

var ST_TERM_CONN_LABEL = { connecting: "connecting", live: "live", closed: "closed", error: "error" };
var ST_TERM_CONN_COLOR = {
  connecting: "var(--amber)",
  live: "var(--green)",
  closed: "var(--text-4)",
  error: "var(--red)",
};

function ST_TermConnDot({ state }) {
  var st = ST_TERM_CONN_LABEL[state] ? state : "connecting";
  return (
    <span className="st-term-conn" data-testid={"terminal-conn-" + st} title={"Terminal " + ST_TERM_CONN_LABEL[st]}>
      <span className="st-term-conn-dot" style={{ background: ST_TERM_CONN_COLOR[st] }} />
      {ST_TERM_CONN_LABEL[st]}
    </span>
  );
}

// ---------------------------------------------------------------------------
// ST_TerminalInstance — one tab's xterm.js Terminal + FitAddon + WebSocket.
// Mounted once per (wid, tab.id) via the parent's `key`; stays mounted
// (hidden via CSS) while inactive so scrollback survives tab switches.
// ---------------------------------------------------------------------------

function ST_TerminalInstance({ wid, tab, active, theme, onState }) {
  var containerRef = React.useRef(null);
  var termRef = React.useRef(null);
  var fitRef = React.useRef(null);
  var wsRef = React.useRef(null);

  // Mount/teardown: create the Terminal + FitAddon, open the WS, wire the
  // binary-stdin / JSON-control frame protocol, and clean everything up on
  // unmount (tab close, panel collapse, or a wid change via the parent key).
  React.useEffect(function () {
    var container = containerRef.current;
    if (!container || !window.Terminal || !(window.FitAddon && window.FitAddon.FitAddon)) {
      return undefined;
    }

    var term = new window.Terminal({
      convertEol: false,
      cursorBlink: true,
      fontFamily: '"IBM Plex Mono", monospace',
      fontSize: 12,
      theme: ST_xtermTheme(),
    });
    var fitAddon = new window.FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(container);
    termRef.current = term;
    fitRef.current = fitAddon;

    try { fitAddon.fit(); } catch (_e) { /* container not laid out yet — keep xterm's 80x24 default */ }

    var encoder = new TextEncoder();
    var ws = null;
    onState(tab.id, "connecting");
    try {
      ws = new WebSocket(ST_termWsUrl(wid, term.cols, term.rows));
    } catch (_e) {
      onState(tab.id, "error");
    }

    if (ws) {
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = function () { onState(tab.id, "live"); };
      ws.onclose = function () { onState(tab.id, "closed"); };
      ws.onerror = function () { onState(tab.id, "error"); };
      ws.onmessage = function (ev) {
        if (typeof ev.data === "string") {
          var msg;
          try { msg = JSON.parse(ev.data); } catch (_e) { return; }
          if (msg && typeof msg.exit !== "undefined") {
            term.write("\r\n\x1b[2m[process exited: " + msg.exit + "]\x1b[0m\r\n");
            onState(tab.id, "closed");
          }
          return;
        }
        var bytes = ev.data instanceof ArrayBuffer ? new Uint8Array(ev.data) : ev.data;
        term.write(bytes);
      };
    }

    var dataSub = term.onData(function (d) {
      var sock = wsRef.current;
      if (sock && sock.readyState === WebSocket.OPEN) {
        sock.send(encoder.encode(d));
      }
    });

    var resizeSub = term.onResize(function (size) {
      var sock = wsRef.current;
      if (sock && sock.readyState === WebSocket.OPEN) {
        sock.send(JSON.stringify({ resize: { cols: size.cols, rows: size.rows } }));
      }
    });

    // Re-fit on container size changes: sidebar drag-resize, the panel
    // itself resizing, or a window resize all change this container's box.
    // Debounced so a drag doesn't spam fit()/resize frames.
    var fitTimer = null;
    function scheduleFit() {
      if (fitTimer) clearTimeout(fitTimer);
      fitTimer = setTimeout(function () {
        fitTimer = null;
        try { fitAddon.fit(); } catch (_e) { /* hidden (display:none) — zero size, ignore */ }
      }, 80);
    }
    var ro = null;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(scheduleFit);
      ro.observe(container);
    }
    window.addEventListener("resize", scheduleFit);

    return function () {
      if (fitTimer) clearTimeout(fitTimer);
      window.removeEventListener("resize", scheduleFit);
      if (ro) ro.disconnect();
      dataSub.dispose();
      resizeSub.dispose();
      if (wsRef.current) {
        try { wsRef.current.close(); } catch (_e) { /* already closed */ }
      }
      wsRef.current = null;
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
    };
  }, [wid, tab.id]);

  // Re-fit + refocus when this tab becomes the active one. While hidden
  // (display:none) the container has zero size, so we only fit when visible
  // — fitting a zero-size container would collapse it to FitAddon's 2x1 floor.
  React.useEffect(function () {
    if (!active) return undefined;
    var term = termRef.current;
    var fitAddon = fitRef.current;
    if (!term || !fitAddon) return undefined;
    var raf = requestAnimationFrame(function () {
      try { fitAddon.fit(); } catch (_e) { /* ignore */ }
      term.focus();
    });
    return function () { cancelAnimationFrame(raf); };
  }, [active]);

  // Live theme sync — a dark/light toggle should repaint an already-open
  // terminal, not just terminals opened after the toggle.
  React.useEffect(function () {
    if (!termRef.current) return;
    termRef.current.options.theme = ST_xtermTheme();
  }, [theme]);

  return (
    <div
      ref={containerRef}
      className="st-term-instance"
      data-testid="terminal-body"
      data-term-id={tab.id}
      style={{ display: active ? "block" : "none", height: "100%", width: "100%" }}
    />
  );
}

// ---------------------------------------------------------------------------
// TerminalPanel — header strip (tabs + new-tab + connection dot + collapse)
// and the body hosting every open tab's ST_TerminalInstance.
// ---------------------------------------------------------------------------

function TerminalPanel({ wid, studio }) {
  var s = studio.state;
  var termTabs = s.termTabs && s.termTabs.length ? s.termTabs : [];
  var activeTermId = s.activeTermId;
  var [connStates, setConnStates] = React.useState({});

  // A workspace switch invalidates every prior connection-state entry (the
  // ST_TerminalInstance children remount fresh under new keys too — see the
  // `wid + ":" + tab.id` key below).
  React.useEffect(function () { setConnStates({}); }, [wid]);

  var handleState = React.useCallback(function (id, state) {
    setConnStates(function (prev) {
      if (prev[id] === state) return prev;
      var next = Object.assign({}, prev);
      next[id] = state;
      return next;
    });
  }, []);

  function focusTab(id) {
    studio.patch({ activeTermId: id });
  }

  function addTab() {
    var tabs = s.termTabs || [];
    var newTab = { id: ST_nextTermId(tabs), title: "bash" };
    studio.patch({ termTabs: tabs.concat([newTab]), activeTermId: newTab.id });
  }

  function closeTab(e, id) {
    if (e) e.stopPropagation();
    var tabs = s.termTabs || [];
    var idx = -1;
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].id === id) { idx = i; break; }
    }
    var nextTabs = tabs.filter(function (t) { return t.id !== id; });
    var nextActive = s.activeTermId;
    if (nextActive === id) {
      if (!nextTabs.length) {
        nextActive = null;
      } else {
        var nextIdx = Math.min(Math.max(0, idx - 1), nextTabs.length - 1);
        nextActive = nextTabs[nextIdx].id;
      }
    }
    studio.patch({ termTabs: nextTabs, activeTermId: nextActive });
    setConnStates(function (prev) {
      if (!(id in prev)) return prev;
      var next = Object.assign({}, prev);
      delete next[id];
      return next;
    });
  }

  var activeState = connStates[activeTermId] || "connecting";

  return (
    <div className="st-term-panel" data-testid="terminal-panel">
      <div className="st-term-head">
        <span className="st-term-label">Terminal</span>
        {termTabs.map(function (tab) {
          var isActive = tab.id === activeTermId;
          var tabState = connStates[tab.id] || "connecting";
          return (
            <div
              key={tab.id}
              className={"st-term-tab" + (isActive ? " is-active" : "")}
              data-testid="terminal-tab"
              data-term-id={tab.id}
              data-active={isActive ? "true" : "false"}
              title={tab.title}
              onClick={function () { focusTab(tab.id); }}
            >
              <span className="st-term-tab-dot" style={{ background: ST_TERM_CONN_COLOR[tabState] || ST_TERM_CONN_COLOR.connecting }} />
              <span>{tab.title}</span>
              <span
                className="st-term-tab-close"
                data-testid="terminal-tab-close"
                title="Close terminal"
                onClick={function (e) { closeTab(e, tab.id); }}
              >
                <Icon name="x" size={10} />
              </span>
            </div>
          );
        })}
        <span
          className="st-term-add"
          data-testid="terminal-new-tab"
          title="New terminal"
          onClick={addTab}
        >
          <Icon name="plus" size={13} />
        </span>
        <span className="st-term-sp" />
        {activeTermId && <ST_TermConnDot state={activeState} />}
        <span
          className="st-term-collapse"
          data-testid="terminal-collapse"
          title="Collapse terminal (Ctrl-`)"
          onClick={studio.toggleTerminal}
        >
          <Icon name="chevron-down" size={13} />
        </span>
      </div>

      <div className="st-term-body">
        {termTabs.length === 0 && (
          <div className="st-term-empty" data-testid="terminal-empty">
            No terminals — click + to open one.
          </div>
        )}
        {termTabs.map(function (tab) {
          return (
            <ST_TerminalInstance
              key={wid + ":" + tab.id}
              wid={wid}
              tab={tab}
              active={tab.id === activeTermId}
              theme={s.theme}
              onState={handleState}
            />
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// No-build exports
// ---------------------------------------------------------------------------
window.TerminalPanel = TerminalPanel;
