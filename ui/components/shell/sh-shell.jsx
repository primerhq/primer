/* global React, SH_api, SH_parseUrl, SH_buildUrl, SH_emptyDocState, SH_openDoc,
   SH_closeDoc, SH_promoteDoc, SH_pinDoc, SH_splitRight, SH_cycleMru,
   SH_createVerbRegistry, SH_createFrecency */
// Fresh shell root (S8 spec sections 1 and 3).
//
// One surface: workspace-scoped, rail + tabs + overlays + palette. There
// is no page router: SH_parseUrl reads the hash, verb navigation writes it
// with pushState, and every doc and overlay is therefore a pasteable link.

var SH_ShellContext = React.createContext(null);

function SH_useShell() {
  return React.useContext(SH_ShellContext);
}

function SH_readUrl() {
  return SH_parseUrl(window.location.hash || "");
}

function SH_Shell(props) {
  var wid = props.wid;
  var initial = SH_readUrl();
  var docsState = React.useState(SH_emptyDocState());
  var docs = docsState[0];
  var setDocs = docsState[1];
  var overlayState = React.useState(initial.overlay);
  var overlay = overlayState[0];
  var setOverlay = overlayState[1];
  var anchorState = React.useState(initial.anchor);
  var anchor = anchorState[0];
  var setAnchor = anchorState[1];

  var registry = React.useMemo(function () { return SH_createVerbRegistry(); }, []);
  var frecency = React.useMemo(function () { return SH_createFrecency(); }, []);

  var sessions = window.primerApi.useResource(
    SH_api.keys.sessions(wid),
    function (signal) { return SH_api.sessions(wid, signal); },
    { pollMs: 5000, deps: [wid] }
  );

  var status = window.primerApi.useResource(
    "auth-status",
    function (signal) {
      return window.primerApi.apiFetch("GET", "/auth/status", null,
        { signal: signal });
    },
    { pollMs: 0 }
  );

  // Voice gating (amendment M11g) and the binding chip's options. Both
  // are static-per-process, so pollMs 0.
  var caps = window.primerApi.useCapabilities();
  var agentList = window.primerApi.useResource(
    "shell-agents",
    function (signal) {
      return window.primerApi.apiFetch("GET", "/agents?limit=200", null,
        { signal: signal });
    },
    { pollMs: 0 }
  );
  // The session doc owns the scroll container, so it fills this in and
  // the jumpLatest verb reads it. A ref, not state: replacing it must not
  // re-render the whole shell.
  var jumpLatestRef = React.useRef(function () {});
  // The attention list fills this in; the triage verbs read it.
  var attentionRef = React.useRef({ items: [], resolve: function () {} });
  // Open session docs publish their status here so the trace tab can pass
  // it through to the S7 panel without opening a second read of its own.
  var sessionStatusRef = React.useRef({});
  // The voice driver publishes its stop handle here.
  var voiceRef = React.useRef({ stop: function () {} });
  // The palette owns its own open state; it publishes the opener here so
  // the palette.open verb runs the same path the chord does.
  var paletteRef = React.useRef({ open: function () {} });

  // ---- URL is the state ----------------------------------------------------
  var active = null;
  var group = docs.groups[docs.activeGroup];
  if (group && group.activeId) {
    for (var i = 0; i < group.tabs.length; i++) {
      if (group.tabs[i].id === group.activeId) active = group.tabs[i];
    }
  }
  React.useEffect(function () {
    var url = SH_buildUrl({
      wid: wid,
      doc: active ? { kind: active.kind, ref: active.ref } : null,
      overlay: overlay,
      anchor: anchor,
    });
    if ((window.location.hash || "") !== url) {
      window.history.pushState(null, "", url);
    }
  }, [wid, active && active.id, overlay && overlay.name,
      overlay && overlay.section, overlay && overlay.id, anchor]);

  React.useEffect(function () {
    function onUrl() {
      var parsed = SH_readUrl();
      setOverlay(parsed.overlay);
      setAnchor(parsed.anchor);
      if (parsed.doc) {
        setDocs(function (s) {
          return SH_openDoc(s, {
            kind: parsed.doc.kind, ref: parsed.doc.ref, preview: false,
          });
        });
      }
    }
    window.addEventListener("hashchange", onUrl);
    window.addEventListener("popstate", onUrl);
    return function () {
      window.removeEventListener("hashchange", onUrl);
      window.removeEventListener("popstate", onUrl);
    };
  }, []);

  // ---- landing: most recent session, lazily created ------------------------
  var landedRef = React.useRef(false);
  React.useEffect(function () {
    if (landedRef.current) return;
    var parsed = SH_readUrl();
    if (parsed.doc) { landedRef.current = true; return; }
    var items = (sessions.data && sessions.data.items) || null;
    if (!items) return;
    landedRef.current = true;
    if (items.length) {
      var newest = items.slice().sort(function (a, b) {
        return String(b.last_activity_at).localeCompare(String(a.last_activity_at));
      })[0];
      setDocs(function (s) {
        return SH_pinDoc(
          SH_openDoc(s, {
            kind: "session", ref: newest.session_id,
            title: newest.name || newest.session_id, preview: false,
          }),
          "session:" + newest.session_id, true
        );
      });
      return;
    }
    // Lazy creation: an empty workspace opens on a fresh session bound to
    // the system default agent (S1 spec section 8).
    SH_api.createSession(wid, {}).then(function (row) {
      var sid = row && (row.session_id || row.id);
      if (!sid) return;
      sessions.refetch();
      setDocs(function (s) {
        return SH_pinDoc(
          SH_openDoc(s, { kind: "session", ref: sid, preview: false }),
          "session:" + sid, true
        );
      });
    });
  }, [wid, sessions.data]);

  var ctx = {
    wid: wid,
    docs: docs,
    sessions: sessions,
    registry: registry,
    frecency: frecency,
    overlay: overlay,
    anchor: anchor,
    openDoc: function (req) { setDocs(function (s) { return SH_openDoc(s, req); }); },
    closeDoc: function (id) { setDocs(function (s) { return SH_closeDoc(s, id); }); },
    promoteDoc: function (id) { setDocs(function (s) { return SH_promoteDoc(s, id); }); },
    pinDoc: function (id, on) { setDocs(function (s) { return SH_pinDoc(s, id, on); }); },
    splitRight: function () { setDocs(function (s) { return SH_splitRight(s); }); },
    cycleMru: function (step) { setDocs(function (s) { return SH_cycleMru(s, step); }); },
    openOverlay: function (name, section, id) {
      setOverlay({ name: name, section: section || null, id: id || null });
    },
    closeOverlay: function () { setOverlay(null); },
    // One active workspace at a time (spec section 3). Rebuilding the URL
    // rather than mutating state keeps refresh and back/forward honest.
    switchWorkspace: function (nextWid) {
      window.location.hash = SH_buildUrl({ wid: nextWid });
    },
    setAnchor: setAnchor,
    toast: function (msg) {
      if (window.primerApi.toastPush) {
        window.primerApi.toastPush({ kind: "info", text: String(msg) });
      }
    },
    role: (status.data && status.data.role) || "user",
    speech: (caps.data && caps.data.speech) || {},
    agents: (agentList.data && agentList.data.items) || [],
    username: (status.data && status.data.username) || "anon",
    attentionRef: attentionRef,
    voiceRef: voiceRef,
    paletteRef: paletteRef,
    openPalette: function () { paletteRef.current.open(); },
    // "Never from background sessions": foreground means this session is
    // the ACTIVE tab of the active group, not merely open somewhere.
    isForeground: function (sid) {
      var group = docs.groups[docs.activeGroup];
      return !!group && group.activeId === "session:" + sid;
    },
    sessionStatus: function (sid) {
      return sessionStatusRef.current[sid] || null;
    },
    setSessionStatus: function (sid, status) {
      sessionStatusRef.current[sid] = status;
    },
    jumpLatestRef: jumpLatestRef,
    focusComposer: function () {
      var el = document.querySelector('[data-testid="shell-composer"] textarea');
      if (el) el.focus();
    },
  };

  return (
    <SH_ShellContext.Provider value={ctx}>
      <div className="sh-root" data-testid="shell-root">
        <header className="sh-topbar" data-testid="shell-topbar">
          {typeof window.SH_Topbar === "function" ? <window.SH_Topbar /> : null}
        </header>
        <aside className="sh-rail" data-testid="shell-rail">
          {typeof window.SH_Rail === "function" ? <window.SH_Rail /> : null}
        </aside>
        <main className="sh-center" data-testid="shell-center">
          {typeof window.SH_DocHost === "function" ? <window.SH_DocHost /> : null}
        </main>
        <footer className="sh-statusbar" data-testid="shell-statusbar">
          {typeof window.SH_StatusBar === "function" ? <window.SH_StatusBar /> : null}
        </footer>
        {typeof window.SH_Palette === "function" ? <window.SH_Palette /> : null}
        {typeof window.SH_OverlayHost === "function" ? <window.SH_OverlayHost /> : null}
      </div>
    </SH_ShellContext.Provider>
  );
}

// Boot: auth gate (auth.jsx) wraps the setup gate (S5) wraps the shell.
// The shell never renders on an incomplete install.
function SH_RootGate() {
  var status = window.primerApi.useResource(
    "auth-status",
    function (signal) {
      return window.primerApi.apiFetch("GET", "/auth/status", null, { signal: signal });
    },
    { pollMs: 0 }
  );
  var parsed = SH_readUrl();
  var wsList = window.primerApi.useResource(
    "shell-workspaces",
    function (signal) { return SH_api.workspaces(signal); },
    { pollMs: 0, pauseWhile: function () { return !!parsed.wid; } }
  );

  if (status.loading) return <div className="sh-boot" data-testid="shell-boot" />;
  if (status.data && status.data.setup_complete === false) {
    return <window.SetupWizardGate onDone={function () { window.location.reload(); }} />;
  }
  var wid = parsed.wid;
  if (!wid) {
    var items = (wsList.data && wsList.data.items) || [];
    if (!items.length) return <div className="sh-boot" data-testid="shell-boot" />;
    wid = items[0].id;
  }
  return <SH_Shell wid={wid} />;
}

window.SH_ShellContext = SH_ShellContext;
window.SH_useShell = SH_useShell;
window.SH_Shell = SH_Shell;
window.SH_RootGate = SH_RootGate;
