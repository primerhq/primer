/* global React, SH_api, SH_parseUrl, SH_buildUrl, SH_createVerbRegistry,
   SH_createFrecency */
// The three-view console root (wiring plan P1 T4). Owns: parsed URL
// state (wid, view, doc, overlay, anchor), the workspace + auth
// resources, the verb registry, panel toggles, and the view host.
// The Studio/Platform/System bodies land in P2/P4/P5; this file's
// contract to them is NV_useConsole().
//
// URL discipline (inherited from sh-shell's hard lessons): the hash
// is the state; this component re-reads it on hashchange/popstate and
// writes it back only when its own state differs, so an arriving deep
// link is never clobbered by a half-changed render.

var NV_ConsoleContext = React.createContext(null);

function NV_useConsole() {
  return React.useContext(NV_ConsoleContext);
}

// The ONE rendered toast stack (ported from the deleted sh shell on
// flag day). primerApi.toastPush enqueues into module state that only
// a rendered subscriber displays; without this host every toast is a
// silent no-op. The host takes over the global entry points while
// mounted so kind/timing rules live in one place.
function NV_ToastHost() {
  var toastState = React.useState([]);
  var toasts = toastState[0];
  var setToasts = toastState[1];
  var toastSeq = React.useRef(1);

  var pushToast = function (t) {
    var id = String(toastSeq.current++);
    setToasts(function (arr) {
      return arr.concat([Object.assign({}, t, { id: id })]);
    });
    setTimeout(function () {
      setToasts(function (arr) {
        return arr.filter(function (x) { return x.id !== id; });
      });
    }, (t && t.kind === "error" ? 12 : 5) * 1000);
  };
  var removeToast = function (id) {
    setToasts(function (arr) {
      return arr.filter(function (x) { return x.id !== id; });
    });
  };

  var pushToastRef = React.useRef(pushToast);
  pushToastRef.current = pushToast;
  var removeToastRef = React.useRef(removeToast);
  removeToastRef.current = removeToast;

  React.useEffect(function () {
    var api = (window.primerApi = window.primerApi || {});
    var prevPush = api.toastPush;
    var prevDismiss = api.toastDismiss;
    api.toastPush = function (t) { return pushToastRef.current(t); };
    api.toastDismiss = function (id) { return removeToastRef.current(id); };
    return function () {
      api.toastPush = prevPush;
      api.toastDismiss = prevDismiss;
    };
  }, []);

  return (
    <div className="toast-stack" data-testid="nv-toasts">
      {toasts.map(function (t) {
        var rid = t.requestId || t.reqId;
        return (
          <div key={t.id} className={"toast toast-" + (t.kind || "info")}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="title">{t.title || t.text}</div>
              {t.detail ? <div className="detail">{t.detail}</div> : null}
              {rid ? (
                <div className="req-id">
                  {"request-id "}
                  <span style={{ color: "var(--text)" }}>{rid}</span>
                  {" · "}
                  {/* The request id is what an operator pastes into a
                      bug report; copy beats transcription. */}
                  <a
                    data-testid="toast-copy-request-id"
                    onClick={function () {
                      if (navigator.clipboard && navigator.clipboard.writeText) {
                        navigator.clipboard.writeText(rid).catch(function () {});
                      }
                    }}
                  >copy</a>
                </div>
              ) : null}
              {Array.isArray(t.actions) && t.actions.length ? (
                <div className="toast-actions">
                  {t.actions.map(function (action, actionIdx) {
                    return (
                      <button type="button" key={actionIdx}
                        className="toast-action" data-testid="toast-action"
                        onClick={function () {
                          // Notes 1.5: an action button must route, not
                          // just dismiss - href drives real navigation
                          // through the same hash the shell reads.
                          if (action && action.href) {
                            window.location.hash = action.href;
                          }
                          if (action && typeof action.run === "function") {
                            action.run();
                          }
                          removeToast(t.id);
                        }}>{action.label}</button>
                    );
                  })}
                </div>
              ) : null}
            </div>
            <button type="button" className="close"
              onClick={function () { removeToast(t.id); }}>x</button>
          </div>
        );
      })}
    </div>
  );
}

function NV_readUrl() {
  return SH_parseUrl(window.location.hash || "");
}

// Frozen module-level empty array: a fresh [] each render would defeat the ctx memo.
var EMPTY_WS_ITEMS = Object.freeze([]);

function NV_Shell() {
  // US-014 M1: a distinct mobile UX (not squeezed desktop), same state
  // and resources - the swap below is purely which chrome renders.
  // isTablet stays on the desktop branch (design: tablet keeps desktop
  // layout); ?force-desktop=1 is handled inside useViewport itself, so
  // isMobile is already false whenever that escape is active.
  var isMobile = window.primerApi.useViewport().isMobile;
  var initial = React.useMemo(NV_readUrl, []);
  var widState = React.useState(initial.wid);
  var wid = widState[0];
  var setWid = widState[1];
  var viewState = React.useState(initial.view);
  var view = viewState[0];
  var setView = viewState[1];
  // TG model as shell state (US-007 R2 phase 2 design): doc is DERIVED
  // from it (TG_activeDoc), not a second source of truth kept in sync.
  // Seed from the initial URL's doc= as a preview tab, matching
  // nv-doc-host.jsx's old preview-by-default load behavior.
  var tgModelState = React.useState(function () {
    var base = window.TG_init();
    return initial.doc ? window.TG_openTab(base, initial.doc, {}) : base;
  });
  var tgModel = tgModelState[0];
  var setTgModel = tgModelState[1];
  var doc = React.useMemo(function () {
    return window.TG_activeDoc(tgModel);
  }, [tgModel]);
  var overlayState = React.useState(initial.overlay);
  var overlay = overlayState[0];
  var setOverlay = overlayState[1];
  var anchorState = React.useState(initial.anchor);
  var anchor = anchorState[0];
  var setAnchor = anchorState[1];
  var menuState = React.useState(null);
  var openMenu = menuState[0];
  var setOpenMenu = menuState[1];
  var panelsState = React.useState({ terminal: false });
  var panels = panelsState[0];
  var setPanels = panelsState[1];
  var tickState = React.useState(0);
  var setTick = tickState[1];
  var paletteRef = React.useRef({ open: function () {} });

  var registry = React.useMemo(function () {
    return SH_createVerbRegistry();
  }, []);
  var frecency = React.useMemo(function () {
    return window.SH_createFrecency ? window.SH_createFrecency() : null;
  }, []);

  var workspaces = window.primerApi.useResource(
    "nv-workspaces",
    function (signal) { return SH_api.workspaces(signal); },
    { pollMs: 15000 }
  );
  var status = window.primerApi.useResource(
    "auth-status",
    function (signal) {
      return window.primerApi.apiFetch("GET", "/auth/status", null,
        { signal: signal });
    },
    { pollMs: 0 }
  );
  // Capability probe: speech config gates the voice affordances.
  var caps = window.primerApi.useCapabilities();
  var voiceRef = React.useRef(null);
  var wsItems = (workspaces.data && workspaces.data.items) || EMPTY_WS_ITEMS;

  // resolveSessionMeta for NV_TabGroups' session tabs: reuses the rail's
  // OWN cache key (nv-rail.jsx) - use-resource.js keys its cache by
  // string across components, so this costs one fetch, not two.
  var railSessions = window.primerApi.useResource(
    "nv-rail-all-sessions",
    function (signal) { return SH_api.allSessions(signal); },
    { pollMs: 5000, deps: [] }
  );
  // F1 (2026-08-29 UI review): generalized from the wid-only seam this
  // used to be - session tabs also want the name (label, instead of the
  // bare id) and binding (NV_identity's glyph/color), and both already
  // ride the same cached row the wid came from.
  var sessionMetaById = React.useMemo(function () {
    var map = {};
    ((railSessions.data && railSessions.data.items) || []).forEach(function (s) {
      map[s.session_id] = {
        wid: s.workspace_id, name: s.name, binding: s.binding,
      };
    });
    return map;
  }, [railSessions.data]);
  // F10 (2026-08-29 UI review): sessionMetaById is derived purely from
  // the 5s poll above, so a session opened the same second it's created
  // (or clicked) shows no pulse, no name and no glyph until the poll
  // catches up. stampedMetaById is an immediate, caller-supplied overlay
  // for exactly those two known-at-open-time opens (rail click, create-
  // session overlay - see stampSessionMeta on the ctx object below);
  // the poll's own data always wins once it arrives, and a session's
  // wid/name/binding don't change themselves just by being opened, so
  // the stamp never goes stale in the meantime.
  var stampedMetaState = React.useState({});
  var stampedMetaById = stampedMetaState[0];
  var setStampedMetaById = stampedMetaState[1];
  var stampSessionMeta = React.useCallback(function (sid, meta) {
    if (!sid || !meta || !meta.wid) return;
    setStampedMetaById(function (prev) {
      var prevMeta = prev[sid];
      if (prevMeta && prevMeta.wid === meta.wid && prevMeta.name === meta.name
        && prevMeta.binding === meta.binding) {
        return prev;
      }
      var next = Object.assign({}, prev);
      next[sid] = meta;
      return next;
    });
  }, []);
  // Returns undefined (not a partial object) for a totally unresolved
  // sid - tabs read this as "no pulse, fall back to the raw id label,"
  // never an error.
  var resolveSessionMeta = React.useCallback(function (sid) {
    return sessionMetaById[sid] || stampedMetaById[sid];
  }, [sessionMetaById, stampedMetaById]);

  // Default workspace: the URL's, else the first listed.
  React.useEffect(function () {
    if (!wid && wsItems.length) setWid(wsItems[0].id);
  }, [wid, wsItems.length]);

  // --- URL sync: read on navigation events -------------------------------
  var ownHashRef = React.useRef(window.location.hash || "");
  // Notes 1.4: verb-driven navigation pushes a real history entry; passive
  // state sync (the default-workspace pick below, or a hash arriving from
  // hashchange/popstate) keeps replaceState. Verb-driven call sites mark
  // this ref right before changing state; the write effect below consumes
  // and resets it, so an unmarked change (the default fallback) replaces.
  var pendingPushRef = React.useRef(false);
  var markPush = React.useCallback(function () {
    pendingPushRef.current = true;
  }, []);
  React.useEffect(function () {
    function onNav() {
      var current = window.location.hash || "";
      if (current === ownHashRef.current) return;
      ownHashRef.current = current;
      var parsed = SH_parseUrl(current);
      setWid(parsed.wid);
      setView(parsed.view);
      // Tabs are global (notes 2.3) and persist across navigation; a URL
      // naming no doc means "names nothing," not "close everything," so
      // a null parsed.doc leaves tgModel untouched. A named doc opens as
      // preview, matching the load-time seed above.
      if (parsed.doc) {
        setTgModel(function (m) { return window.TG_openTab(m, parsed.doc, {}); });
      }
      setOverlay(parsed.overlay);
      setAnchor(parsed.anchor);
    }
    window.addEventListener("hashchange", onNav);
    window.addEventListener("popstate", onNav);
    return function () {
      window.removeEventListener("hashchange", onNav);
      window.removeEventListener("popstate", onNav);
    };
  }, []);

  // --- URL sync: write when our state moved ------------------------------
  React.useEffect(function () {
    var url = SH_buildUrl({
      wid: wid, view: view, doc: doc, overlay: overlay, anchor: anchor,
    });
    if ((window.location.hash || "") !== url) {
      ownHashRef.current = url;
      if (pendingPushRef.current) {
        window.history.pushState(null, "", url);
      } else {
        window.history.replaceState(null, "", url);
      }
      pendingPushRef.current = false;
    }
  }, [wid, view && view.name, view && view.nav,
    doc && doc.kind, doc && doc.ref,
    overlay && overlay.name, overlay && overlay.section,
    overlay && overlay.id, anchor]);

  // Menus close on any outside click.
  React.useEffect(function () {
    function onDocClick() { setOpenMenu(null); }
    document.addEventListener("click", onDocClick);
    return function () { document.removeEventListener("click", onDocClick); };
  }, []);

  // A registered chord is a live binding, not a palette label. One
  // dispatcher walks the registry so a verb's chord can never be
  // declared and then silently dead. The palette owns Ctrl+K itself
  // (it also handles Esc); everything else resolves here.
  React.useEffect(function () {
    function chordMatches(chord, ev) {
      var bits = String(chord).split("+");
      var key = bits[bits.length - 1].toLowerCase();
      var wantCtrl = bits.indexOf("Ctrl") >= 0;
      var wantShift = bits.indexOf("Shift") >= 0;
      // F6 (2026-08-29 UI review): session.create moved to Alt+n, which
      // this matcher never checked before - without gating on ev.altKey,
      // "Alt+n" would have matched a bare "n" keypress in any text field.
      var wantAlt = bits.indexOf("Alt") >= 0;
      if ((ev.ctrlKey || ev.metaKey) !== wantCtrl) return false;
      if (!!ev.shiftKey !== wantShift) return false;
      if (!!ev.altKey !== wantAlt) return false;
      return String(ev.key).toLowerCase() === key;
    }
    function onKey(ev) {
      if (ev.defaultPrevented) return;
      var verbs = registry.all();
      for (var i = 0; i < verbs.length; i++) {
        var v = verbs[i];
        if (!v.chord || v.id === "palette.open") continue;
        if (chordMatches(v.chord, ev)) {
          ev.preventDefault();
          v.run();
          return;
        }
      }
    }
    window.addEventListener("keydown", onKey);
    return function () { window.removeEventListener("keydown", onKey); };
  }, [registry]);

  var goView = React.useCallback(function (name, nav) {
    markPush();
    setView({ name: name, nav: nav || null });
    setOpenMenu(null);
  }, [setView, setOpenMenu, markPush]);

  // --- Core verbs (chrome-level; view bodies add their own) --------------
  React.useEffect(function () {
    function reg(v) { if (!registry.get(v.id)) registry.register(v); }
    reg({
      id: "view.studio", label: "Open Studio", surfaces: ["topbar", "palette"],
      run: function () { goView("studio"); },
    });
    reg({
      id: "view.platform", label: "Open Platform", surfaces: ["topbar", "palette"],
      run: function () { goView("platform"); },
    });
    reg({
      id: "view.system", label: "Open System Settings",
      surfaces: ["topbar", "palette"],
      run: function () { goView("system"); },
    });
    reg({
      id: "workspace.switch", label: "Switch Workspace",
      // F6 (2026-08-29 UI review): Ctrl+Shift+P is Firefox's private-
      // window shortcut - the page never saw it. Ctrl+Shift+O is not
      // reserved in Chrome/Edge/Firefox. Surface is "rail" now, not
      // "topbar" - US-012b item 3 retired the topbar dropdown, the rail
      // tree row is the only pointer affordance left.
      chord: "Ctrl+Shift+o", surfaces: ["rail", "palette"],
      run: function (arg) {
        if (arg && arg.wid) {
          markPush();
          setWid(arg.wid);
          // US-007 R2: tabs are global across workspaces (notes 2.3) -
          // switching workspace no longer drops the open doc/anchor, it
          // only changes which workspace drives the Files sidebar/
          // terminal/rail selection. (Was setDoc(null)/setAnchor(null)
          // under the old single-doc-per-workspace model.)
        } else {
          setOpenMenu("ws");
        }
      },
    });
    reg({
      // F6 (2026-08-29 UI review): Ctrl+N is browser-reserved (new
      // window) in Chrome/Edge - the page never even sees the keydown,
      // so the chord was silently dead. Alt+N is not reserved.
      id: "session.create", label: "Create Session", chord: "Alt+n",
      surfaces: ["rail", "palette"],
      // The shared entry point (rail "+", empty state, palette) opens
      // the SAME overlay a pasted overlay=new-session link opens.
      run: function () {
        markPush();
        setOverlay({ name: "new-session", section: null, id: null });
      },
    });
    reg({
      id: "workspace.create", label: "Create Workspace",
      surfaces: ["topbar", "palette"],
      run: function () {
        markPush();
        setOverlay({ name: "new-workspace", section: null, id: null });
      },
    });
    reg({
      id: "palette.open", label: "Open Palette", chord: "Ctrl+k",
      surfaces: ["topbar", "palette"],
      run: function () { paletteRef.current.open(); },
    });
    reg({
      id: "terminal.toggle", label: "Toggle Terminal", chord: "Ctrl+j",
      surfaces: ["topbar", "palette"],
      run: function () {
        setPanels(function (p) {
          return { terminal: !p.terminal };
        });
      },
    });
  }, [registry]);

  var ctx = React.useMemo(function () {
    return {
      wid: wid,
      view: view || { name: "studio", nav: null },
      doc: doc,
      overlay: overlay,
      anchor: anchor,
      panels: panels,
      openMenu: openMenu,
      toggleMenu: function (name) {
        setOpenMenu(function (cur) { return cur === name ? null : name; });
      },
      registry: registry,
      frecency: frecency,
      workspaces: wsItems,
      username: (status.data && status.data.username) || "anon",
      role: (status.data && status.data.role) || "user",
      speech: (caps.data && caps.data.speech) || {},
      voiceRef: voiceRef,
      paletteRef: paletteRef,
      goView: goView,
      tgModel: tgModel,
      resolveSessionMeta: resolveSessionMeta,
      stampSessionMeta: stampSessionMeta,
      // F2 (2026-08-29 UI review): a cross-workspace session open used to
      // run workspace.switch (its own markPush) then setDoc (a SECOND
      // markPush) - two history entries for one navigation, so Back
      // landed on a half-state (new workspace, old doc). One markPush,
      // one combined state change; callers that also want the opened doc
      // promoted (not just previewed) still call con.promoteDoc right
      // after, same two-step every other caller here already uses.
      openInWorkspace: function (owid, docSpec) {
        markPush();
        if (owid) setWid(owid);
        setTgModel(function (m) {
          return docSpec ? window.TG_openTab(m, docSpec, {}) : m;
        });
      },
      // Same F2 shape, for the rail's workspace-menu "New session" (which
      // right-clicking a NON-selected workspace row also switches-then-
      // acts): workspace.switch's own run and session.create's own run
      // each carry their own markPush, so calling both back to back is
      // the same two-history-entries risk as onOpenSession was. One
      // markPush here instead of routing through either verb.
      createSessionInWorkspace: function (owid) {
        markPush();
        if (owid) setWid(owid);
        setOverlay({ name: "new-session", section: null, id: null });
      },
      // Opening a document/overlay is a real navigation (palette entity
      // rows, sidebar clicks, verb runs all funnel through this one
      // setter) so it earns a history entry, not a silent replace.
      // Preview/reactivate (never promote:true here) - callers that want
      // promoted-on-open call con.promoteDoc right after, same two-step
      // every existing caller (nv-overlays.jsx, nv-files-sidebar.jsx,
      // nv-file-docs.jsx) already uses.
      setDoc: function (d) {
        markPush();
        setTgModel(function (m) {
          // con.setDoc(null) is a real, live call shape - nv-doc-host.jsx
          // closing its last tab, nv-session-doc.jsx's onDeleted - both
          // meaning "nothing is open here anymore." Map it onto closing
          // whichever tab is currently active rather than crashing
          // TG_openTab on a null doc.
          if (!d) {
            var active = window.TG_activeDoc(m);
            return active ? window.TG_closeTab(m, active.id) : m;
          }
          return window.TG_openTab(m, d, {});
        });
      },
      // Replaces nv-doc-host.jsx's old hack of stuffing this onto `con`
      // imperatively during render. Never touches the URL - preview vs.
      // promoted isn't URL-visible.
      promoteDoc: function (id) {
        setTgModel(function (m) { return window.TG_promoteTab(m, id); });
      },
      // Wired straight to NV_TabGroups' onModelChange (op is "open" for
      // select/promote, "manage" for close/move/split/focus - see that
      // file's comment). Only "open" pushes; "manage" changes can move
      // the active doc as a side effect the user didn't navigate to.
      onTgModelChange: function (next, op) {
        if (op === "open") markPush();
        setTgModel(next);
      },
      openOverlay: function (name, section, id) {
        markPush();
        setOverlay({ name: name, section: section || null, id: id || null });
      },
      closeOverlay: function () { markPush(); setOverlay(null); },
      bump: function () { setTick(function (v) { return v + 1; }); },
      // extra (optional): { kind, requestId } - an error toast that came
      // from an ApiError should pass the error so the request id renders
      // with its copy affordance. A plain string toast never carried one,
      // which left that affordance dead on every error (BDD round 2).
      toast: function (msg, extra) {
        if (window.primerApi.toastPush) {
          window.primerApi.toastPush({
            kind: (extra && extra.kind) || "info",
            text: String(msg),
            requestId: (extra && (extra.requestId || extra.request_id))
              || null,
          });
        }
      },
    };
  }, [wid, view, doc, overlay, anchor, panels, openMenu, registry,
    frecency, wsItems, status, caps, voiceRef, paletteRef, goView,
    tgModel, setTgModel, resolveSessionMeta, stampSessionMeta, setOpenMenu,
    setOverlay, setTick, markPush, setWid]);

  var viewName = ctx.view.name;
  return (
    <NV_ConsoleContext.Provider value={ctx}>
      <div className="nv-root" data-testid="nv-root" data-view={viewName}
        data-mobile={isMobile ? "true" : "false"}>
        {isMobile && typeof window.NV_MobileShell === "function" ? (
          <window.NV_MobileShell />
        ) : (
          <React.Fragment>
            <window.NV_ActivityBar />
            <div className="nv-main">
              <window.NV_Topbar />
              <div className="nv-view" data-testid={"nv-view:" + viewName}>
                {viewName === "studio" && typeof window.NV_Studio === "function"
                  ? <window.NV_Studio />
                  : null}
                {viewName === "platform" && typeof window.NV_Platform === "function"
                  ? <window.NV_Platform />
                  : null}
                {viewName === "system" && typeof window.NV_System === "function"
                  ? <window.NV_System />
                  : null}
                {(viewName === "studio" && typeof window.NV_Studio !== "function")
                  || (viewName === "platform" && typeof window.NV_Platform !== "function")
                  || (viewName === "system" && typeof window.NV_System !== "function")
                  ? (
                    <div className="nv-view-pending" data-testid="nv-view-pending">
                      <span>The {viewName} view lands in a later wiring phase.</span>
                    </div>
                  ) : null}
              </div>
            </div>
          </React.Fragment>
        )}
        {typeof window.NV_OverlayHost === "function"
          ? <window.NV_OverlayHost />
          : null}
        {typeof window.NV_Palette === "function" ? <window.NV_Palette /> : null}
        {/* One host renders the active confirm/prompt dialog for
            everything below it (FB9); window.confirmDialog resolves
            through it, so without the mount every confirm hangs. */}
        {typeof window.ConfirmHost === "function"
          ? <window.ConfirmHost />
          : null}
        <NV_ToastHost />
      </div>
    </NV_ConsoleContext.Provider>
  );
}

window.NV_useConsole = NV_useConsole;
window.NV_Shell = NV_Shell;
