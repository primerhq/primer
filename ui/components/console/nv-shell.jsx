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

function NV_toastPayload(msg, extra) {
  var opts = typeof extra === "string" ? { kind: extra } : (extra || {});
  return {
    kind: opts.kind || "info",
    text: String(msg),
    requestId: opts.requestId || opts.request_id || null,
  };
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

  var renderToast = function (t) {
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
                  <button
                    type="button"
                    aria-label="Copy request id"
                    data-testid="toast-copy-request-id"
                    onClick={function () {
                      if (navigator.clipboard && navigator.clipboard.writeText) {
                        navigator.clipboard.writeText(rid).catch(function () {});
                      }
                    }}
                  >copy</button>
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
            <button type="button" className="close" aria-label="Dismiss"
              onClick={function () { removeToast(t.id); }}>x</button>
          </div>
        );
  };
  // Two sibling live regions, both always in the page so a toast inserted into either is announced: errors are assertive, everything else
  // polite. An alert nested inside a polite status region was announced twice by some screen readers (console review C-029).
  var isError = function (t) { return t.kind === "error"; };
  return (
    <div className="toast-stack" data-testid="nv-toasts">
      <div className="toast-region" role="status" aria-live="polite" data-testid="nv-toasts-status">
        {toasts.filter(function (t) { return !isError(t); }).map(renderToast)}
      </div>
      <div className="toast-region" role="alert" data-testid="nv-toasts-alert">
        {toasts.filter(isError).map(renderToast)}
      </div>
    </div>
  );
}

function NV_readUrl() {
  return SH_parseUrl(window.location.hash || "");
}

// Frozen module-level empty array: a fresh [] each render would defeat the ctx memo.
var EMPTY_WS_ITEMS = Object.freeze([]);

// The key the tab groups' working set is kept under: one per signed-in user, and none for anyone else. `GET /v1/auth/status` names the user only when authenticated
// (an install with auth off answers authenticated: true, username: "system", a name like any other); a signed-out status, a status without a name or a stranger's shape
// has no key, so nothing is read or written for it (a signed-out refetch used to land on ...:anon, a legal username).
function NV_tabsStoreKey(status) {
  if (!status || status.authenticated !== true) return null;
  if (typeof status.username !== "string" || !status.username) return null;
  return "primer.console.tabs.v1:" + status.username;
}

function NV_usernameOf(status) {
  if (!status || status.authenticated !== true) return null;
  if (typeof status.username !== "string" || !status.username.trim()) return null;
  return status.username;
}
// The workspace the console opens when the address names none (pivot P2 Task 14): the row GET /v1/workspaces flags with the response-only
// is_default (prime, the default workspace), else the first listed (the list is capped at one page, and an older server sends no flag), else none.
function NV_defaultWid(items) {
  var rows = items || [];
  for (var i = 0; i < rows.length; i++) {
    if (rows[i] && rows[i].is_default === true) return rows[i].id;
  }
  return rows.length && rows[0] ? rows[0].id : null;
}

// The tab groups' working set, kept per user in this browser (console review C-023). Identity and layout only (TG_serialize); storage can be
// unavailable or hold anything (private mode, quota, another version), so every access is guarded and a bad value is the same as no value.
function NV_loadTabs(key) {
  try {
    var raw = window.localStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch (_e) {
    return null;
  }
}
function NV_saveTabs(key, saved) {
  try {
    window.localStorage.setItem(key, JSON.stringify(saved));
  } catch (_e) {
    // nothing to do: the working set is a convenience
  }
}
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
    "nv-shell:auth-status",
    function (signal) {
      return window.primerApi.apiFetch("GET", "/auth/status", null,
        { signal: signal });
    },
    { retryMs: 2000 }
  );
  // The theme verbs (registered once, in the core-verb effect below) run long after the render that registered them, and act on the shell as it is
  // then: the signed-in username (the key of the user's stored choice) and the shell's re-render, which redraws the profile menu's pressed button.
  // Refreshed on every render, like pushToastRef above.
  var themeEnvRef = React.useRef(null);
  themeEnvRef.current = {
    username: NV_usernameOf(status.data),
    bump: function () { setTick(function (v) { return v + 1; }); },
  };
  // Capability probe: speech config gates the voice affordances.
  var caps = window.primerApi.useCapabilities();
  var voiceRef = React.useRef(null);
  var wsItems = (workspaces.data && workspaces.data.items) || EMPTY_WS_ITEMS;

  // resolveSessionMeta for NV_TabGroups' session tabs: reuses the rail's
  // OWN cache key (nv-rail.jsx) - use-resource.js keys its cache by
  // string across components, so this costs one fetch, not two.
  var railSessions = window.NV_useSessionList();
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

  // Default workspace: the URL's, else the flagged one (NV_defaultWid: prime, else the first listed). FUNCTIONAL on purpose: this effect runs after the render in
  // which the list arrived and its closure holds that render's wid (null). A link followed in between has already queued setWid(<its workspace>) through the
  // hashchange listener, and a plain setWid(first) from the stale closure queued after it won: the link's workspace was lost, its doc= kept, and the URL write
  // effect then wrote the first workspace into the address bar (the unknown-workspace CI flake, ticket 01a11d41). The updater sees the latest value and only
  // fills an empty one.
  React.useEffect(function () {
    if (wsItems.length) setWid(function (current) { return current || NV_defaultWid(wsItems); });
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
  // The listener effect below hands its reader to the write effect, which
  // reads an address-bar change it has not seen before it writes over it.
  var readUrlRef = React.useRef(null);
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
      // The address bar shows what was APPLIED (console review C-025). The write effect below only runs when a state value moved, so an
      // unknown part that parsed to the state the console was already in (?overlay=bogus, ?view=bogus, ?foo=bar) changed nothing and
      // stayed in the bar, and a shared link carried a parameter that meant nothing. A valid link is its own canonical form and is
      // left alone; one with an unknown part is replaced (never pushed: it is the same navigation).
      var canonical = SH_buildUrl(parsed);
      if (canonical !== current) {
        ownHashRef.current = canonical;
        window.history.replaceState(null, "", canonical);
      }
    }
    readUrlRef.current = onNav;
    window.addEventListener("hashchange", onNav);
    window.addEventListener("popstate", onNav);
    return function () {
      readUrlRef.current = null;
      window.removeEventListener("hashchange", onNav);
      window.removeEventListener("popstate", onNav);
    };
  }, []);

  // --- URL sync: write when our state moved ------------------------------
  React.useEffect(function () {
    var url = SH_buildUrl({
      wid: wid, view: view, doc: doc, overlay: overlay, anchor: anchor,
    });
    var current = window.location.hash || "";
    if (current !== url) {
      // The address bar moved since this shell last read or wrote it (a link
      // opened, or the hash set, between the first render and now) and the
      // event for it has not been delivered yet: this state is the stale
      // side. Read the address bar instead of overwriting it with this
      // state's URL, which would lose the navigation for good.
      if (current !== ownHashRef.current) {
        pendingPushRef.current = false;
        if (readUrlRef.current) readUrlRef.current();
        return;
      }
      ownHashRef.current = url;
      if (pendingPushRef.current) {
        window.history.pushState(null, "", url);
      } else {
        window.history.replaceState(null, "", url);
      }
      pendingPushRef.current = false;
    }
  }, [wid, view && view.name, view && view.nav, view && view.sub,
    doc && doc.kind, doc && doc.ref,
    overlay && overlay.name, overlay && overlay.section,
    overlay && overlay.id, anchor]);

  // The working set survives a reload (console review C-023). Desktop only: the phone's shell shows any open document full screen and its Back closes the tab, so a
  // restored desktop set would take the phone over and a phone's Back would delete what the desktop stored. The key names the signed-in user (NV_tabsStoreKey: none
  // until the auth status says who), and the restore runs ONCE, under the key it read; the write only ever uses that same key (tabsKeyRef), so a later refetch that
  // turns out to be another user (a window shown again after someone else signed in) can neither store the tabs on screen under that user nor read theirs. The
  // restore merges into whatever the load already holds (the URL's document, opened as a tab, stays the active one); the write effect waits for it (tabsRestored),
  // or the first render's one-tab model would overwrite the stored working set before it was read. useViewport re-reads the width when its listener attaches,
  // before the auth-status resource can provide a persistence key, so the isMobile guard sees the settled band without delaying desktop restore.
  var tabsStoreKey = NV_tabsStoreKey(status.data);
  var tabsKeyRef = React.useRef(null);
  var tabsRestoredState = React.useState(false);
  var tabsRestored = tabsRestoredState[0];
  var setTabsRestored = tabsRestoredState[1];
  React.useEffect(function () {
    if (isMobile || !tabsStoreKey || tabsRestored) return;
    tabsKeyRef.current = tabsStoreKey;
    var saved = NV_loadTabs(tabsStoreKey);
    if (saved) {
      setTgModel(function (live) { return window.TG_restoreInto(live, saved, window.SH_DOC_KINDS); });
    }
    setTabsRestored(true);
  }, [isMobile, tabsStoreKey, tabsRestored]);
  React.useEffect(function () {
    if (isMobile || !tabsRestored || !tabsStoreKey || tabsStoreKey !== tabsKeyRef.current) return;
    NV_saveTabs(tabsKeyRef.current, window.TG_serialize(tgModel));
  }, [isMobile, tgModel, tabsStoreKey, tabsRestored]);

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

  // Drops the view without a history entry (no markPush, so the URL write replaces the current entry). A surface that has acted on a
  // view (the phone shell opening a Platform section) calls it, so the URL stops naming a view nothing will show: a later hashchange or
  // popstate re-parses the URL into a fresh view object, and a link still in it would be replayed.
  var clearView = React.useCallback(function () {
    setView(null);
  }, [setView]);

  // sub: the third segment of the view, the provider type of Platform > Providers (seam I-1); it means nothing for a nav that has no types, and the URL builder drops it there.
  var goView = React.useCallback(function (name, nav, sub) {
    markPush();
    setView({ name: name, nav: nav || null, sub: sub || null });
    setOpenMenu(null);
  }, [setView, setOpenMenu, markPush]);

  // Canonicalise a view without creating a history entry. A surface uses this
  // when it has already fallen back from an unavailable page to the page it
  // actually rendered.
  var replaceView = React.useCallback(function (name, nav, sub) {
    setView({ name: name, nav: nav || null, sub: sub || null });
    setOpenMenu(null);
  }, [setView, setOpenMenu]);

  // Closes the open overlay and lands on a view as ONE navigation: one markPush, so one history entry (revamp design 10, D1). A detail overlay's
  // Delete and Back mean "leave this record", and where that lands is the entity's Platform page (target: { name, nav?, sub? }, the shim's
  // SH_SHIM_LIST_TARGETS), not the page the overlay was opened over (a pasted link opens it over the studio). `sub` is the provider type of the
  // Providers page; a console whose view has no such slot ignores it.
  var closeOverlayTo = React.useCallback(function (target) {
    markPush();
    setOverlay(null);
    setView({ name: target.name, nav: target.nav || null, sub: target.sub || null });
  }, [markPush, setOverlay, setView]);

  // Opens a session in the studio from wherever it was asked for: a session row on an agent's, a toolset's or a workspace's detail. The session
  // is a studio document, so the overlay closes, the studio is selected and the session opens as a promoted tab, in ONE history entry (the palette does the
  // same in three calls). The files sidebar, the terminal and the rail follow the session's workspace: the one the caller names (a session row carries
  // its own workspace_id), else the one the rail's cache knows, else the one already selected. A session created a moment ago is not in the cache yet;
  // tabs are global, so it shows all the same.
  var openSessionInStudio = React.useCallback(function (sid, owid) {
    if (!sid) return;
    var meta = resolveSessionMeta(sid);
    var target = owid || (meta && meta.wid) || null;
    markPush();
    if (target) setWid(target);
    setOverlay(null);
    setView({ name: "studio", nav: null, sub: null });
    setTgModel(function (m) {
      return window.TG_openTab(m, { kind: "session", ref: sid }, { promote: true });
    });
  }, [resolveSessionMeta, markPush, setWid, setOverlay, setView, setTgModel]);

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
    // D3 (platform revamp): your own profile page, for every role, and the three theme choices. The pointer affordances are the profile menu's
    // "Your profile" row and its Theme buttons (nv-chrome.jsx), and the phone's More tab carries the same three buttons (nv-mobile-shell.jsx).
    reg({
      id: "view.profile", label: "Open Profile", surfaces: ["topbar", "palette"],
      run: function () { goView("system", "profile"); },
    });
    reg({
      id: "theme.light", label: "Switch To Light Theme", surfaces: ["topbar", "palette"],
      run: function () { window.NV_applyThemeChoice(themeEnvRef.current, "light"); },
    });
    reg({
      id: "theme.dark", label: "Switch To Dark Theme", surfaces: ["topbar", "palette"],
      run: function () { window.NV_applyThemeChoice(themeEnvRef.current, "dark"); },
    });
    reg({
      id: "theme.system", label: "Switch To System Theme", surfaces: ["topbar", "palette"],
      run: function () { window.NV_applyThemeChoice(themeEnvRef.current, "system"); },
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
      id: "provider.register", label: "Register Provider", surfaces: ["palette", "platform-nav"],
      run: function () {
        goView("platform", "providers");
        // The navigation commit may remount the providers page. A synchronous
        // request would reach the unmounting instance's taker and be lost; the
        // macrotask runs after the commit so the request reaches the live taker.
        // The pending mailbox stays as the backstop for slower mounts.
        if (window.PC_requestRegister) setTimeout(function () { window.PC_requestRegister(null); }, 0);
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
    // Pivot P2 Task 15: an admin's reset of prime's agents.md to the shipped version (NV_resetPrimeInstructions, nv-system.jsx: it asks first). Its
    // pointer affordance is the row of the Dashboard Setup card's menu, which carries this id as its data-verb and is drawn for an admin only; the
    // palette offers it to an admin only too (available reads the role the palette ranks with, nv-palette.jsx), since the route answers 403 to anyone else.
    reg({
      id: "setup.reset_prime_instructions", label: "Reset Prime Instructions",
      surfaces: ["overlay-button", "palette"], destructive: true,
      available: function (c) { return c.role === "admin"; },
      run: function () {
        if (typeof window.NV_resetPrimeInstructions === "function") window.NV_resetPrimeInstructions();
      },
    });
    // Every Platform page and every provider type is a palette verb too ("Open Providers", "Open Providers: TTS", "Open LLM Profiles" ...) on the platform-nav
    // surface: the rows of the Platform sidebar are their pointer affordance (the data-verb of NV_PlatNav, nv-platform.jsx). NV_platVerbSpecs lists them in the
    // sidebar's order and leaves the custom pages out: a custom page registers its own verb with one reg() call right after this loop (platform.open.web,
    // "Open Web Settings", is the first; platform.open.templates is "Open Workspace Templates"), in the plan that ships the page. Registered last on purpose:
    // the palette lists the first eight verbs of an empty query in registration order.
    window.NV_platVerbSpecs().forEach(function (s) {
      // The label is built from a page title or a provider type, which is free text. The registry throws on a label its lint refuses (a word that starts with a digit
      // or a symbol), and a throw here takes the console down, so that one verb is skipped with a warning: its page keeps its row and every other verb registers.
      var refused = window.SH_lintVerbLabel(s.label);
      if (refused) {
        console.warn("platform verb " + s.id + " not registered: " + refused);
        return;
      }
      reg({
        id: s.id, label: s.label, surfaces: ["palette", "platform-nav"],
        run: function () { goView("platform", s.nav, s.sub); },
      });
    });
    // Platform › Web (platform revamp 04): the custom page where an admin says which providers answer web searches and page fetches. Its sidebar row
    // carries this verb's id as its data-verb (NV_PlatNav draws one per nav), so the row is the pointer affordance. Like the row, the verb exists only
    // while the page's component is loaded (NV_platNavAvailable): without it a link to the page opens Providers, and a verb named for it would land there too.
    if (window.NV_platNavAvailable("web")) {
      reg({
        id: "platform.open.web", label: "Open Web Settings", aliases: ["Web Search", "Web Fetch"],
        surfaces: ["palette", "platform-nav"],
        run: function () { goView("platform", "web"); },
      });
    }
    // Platform › Workspace templates (platform revamp 08): the custom page of the workspace templates. Like Web's, its two verbs exist only while the page's
    // component is loaded (NV_platNavAvailable), so no verb lands on Providers. Open Workspace Templates has the sidebar row as its pointer affordance. Create
    // Workspace Template opens the page and asks it to open its create overlay; a page that is not mounted yet takes the request when it mounts
    // (WT_requestCreate, workspaces/templates.jsx). Its pointer affordances are the new-workspace dialog's "Create a template" (nv-overlays.jsx) and the
    // page's own empty state.
    if (window.NV_platNavAvailable("templates")) {
      reg({
        id: "platform.open.templates", label: "Open Workspace Templates", surfaces: ["palette", "platform-nav"],
        run: function () { goView("platform", "templates"); },
      });
      reg({
        id: "template.create", label: "Create Workspace Template", surfaces: ["palette", "overlay-button"],
        run: function () {
          goView("platform", "templates");
          if (window.WT_requestCreate) window.WT_requestCreate();
        },
      });
    }
  }, [registry]);

  var ctx = React.useMemo(function () {
    return {
      wid: wid,
      view: view || { name: "studio", nav: null, sub: null },
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
      username: NV_usernameOf(status.data),
      role: (status.data && status.data.role) || "user",
      viewer: status.data && status.data.principal,
      speech: (caps.data && caps.data.speech) || {},
      voiceRef: voiceRef,
      paletteRef: paletteRef,
      goView: goView,
      replaceView: replaceView,
      clearView: clearView,
      closeOverlayTo: closeOverlayTo,
      openSessionInStudio: openSessionInStudio,
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
          window.primerApi.toastPush(NV_toastPayload(msg, extra));
        }
      },
    };
  }, [wid, view, doc, overlay, anchor, panels, openMenu, registry,
    frecency, wsItems, status, caps, voiceRef, paletteRef, goView, replaceView, clearView,
    closeOverlayTo, openSessionInStudio,
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
              <div className="nv-view" data-testid={"nv-view:" + viewName} role="main">
                {/* The page's heading for assistive tech: the shell draws none of its own (the overlays and pages carry their own). */}
                <h1 className="nv-sr-only" data-testid="nv-view-heading">
                  {viewName === "platform" ? "Platform" : viewName === "system" ? "System settings" : "Studio"}
                </h1>
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
window.NV_defaultWid = NV_defaultWid;
