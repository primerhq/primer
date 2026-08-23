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

function NV_readUrl() {
  return SH_parseUrl(window.location.hash || "");
}

function NV_Shell() {
  var initial = React.useMemo(NV_readUrl, []);
  var widState = React.useState(initial.wid);
  var wid = widState[0];
  var setWid = widState[1];
  var viewState = React.useState(initial.view);
  var view = viewState[0];
  var setView = viewState[1];
  var docState = React.useState(initial.doc);
  var doc = docState[0];
  var setDoc = docState[1];
  var overlayState = React.useState(initial.overlay);
  var overlay = overlayState[0];
  var setOverlay = overlayState[1];
  var anchorState = React.useState(initial.anchor);
  var anchor = anchorState[0];
  var setAnchor = anchorState[1];
  var menuState = React.useState(null);
  var openMenu = menuState[0];
  var setOpenMenu = menuState[1];
  var panelsState = React.useState({ terminal: false, events: false });
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
  var wsItems = (workspaces.data && workspaces.data.items) || [];

  // Default workspace: the URL's, else the first listed.
  React.useEffect(function () {
    if (!wid && wsItems.length) setWid(wsItems[0].id);
  }, [wid, wsItems.length]);

  // --- URL sync: read on navigation events -------------------------------
  var ownHashRef = React.useRef(window.location.hash || "");
  React.useEffect(function () {
    function onNav() {
      var current = window.location.hash || "";
      if (current === ownHashRef.current) return;
      ownHashRef.current = current;
      var parsed = SH_parseUrl(current);
      setWid(parsed.wid);
      setView(parsed.view);
      setDoc(parsed.doc);
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
      window.history.pushState(null, "", url);
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
      if ((ev.ctrlKey || ev.metaKey) !== wantCtrl) return false;
      if (!!ev.shiftKey !== wantShift) return false;
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

  function goView(name, nav) {
    setView({ name: name, nav: nav || null });
    setOpenMenu(null);
  }

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
      chord: "Ctrl+Shift+p", surfaces: ["topbar", "palette"],
      run: function (arg) {
        if (arg && arg.wid) {
          setWid(arg.wid);
          setDoc(null);
          setAnchor(null);
        } else {
          setOpenMenu("ws");
        }
      },
    });
    reg({
      id: "session.create", label: "Create Session", chord: "Ctrl+n",
      surfaces: ["rail", "palette"],
      // The shared entry point (rail "+", empty state, palette) opens
      // the SAME overlay a pasted overlay=new-session link opens.
      run: function () {
        setOverlay({ name: "new-session", section: null, id: null });
      },
    });
    reg({
      id: "workspace.create", label: "Create Workspace",
      surfaces: ["topbar", "palette"],
      run: function () {
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
          return { terminal: !p.terminal, events: p.events };
        });
      },
    });
    reg({
      id: "events.toggle", label: "Toggle Workspace Events",
      surfaces: ["topbar", "palette"],
      run: function () {
        setPanels(function (p) {
          return { terminal: p.terminal, events: !p.events };
        });
      },
    });
  }, [registry]);

  var ctx = {
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
    paletteRef: paletteRef,
    goView: goView,
    setDoc: setDoc,
    openOverlay: function (name, section, id) {
      setOverlay({ name: name, section: section || null, id: id || null });
    },
    closeOverlay: function () { setOverlay(null); },
    bump: function () { setTick(function (v) { return v + 1; }); },
    toast: function (msg) {
      if (window.primerApi.toastPush) {
        window.primerApi.toastPush({ kind: "info", text: String(msg) });
      }
    },
  };

  var viewName = ctx.view.name;
  return (
    <NV_ConsoleContext.Provider value={ctx}>
      <div className="nv-root" data-testid="nv-root" data-view={viewName}>
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
        {typeof window.NV_OverlayHost === "function"
          ? <window.NV_OverlayHost />
          : null}
        {typeof window.NV_Palette === "function" ? <window.NV_Palette /> : null}
      </div>
    </NV_ConsoleContext.Provider>
  );
}

window.NV_useConsole = NV_useConsole;
window.NV_Shell = NV_Shell;
