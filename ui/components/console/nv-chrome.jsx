/* global React, NV_useConsole */
// The three-view chrome (wiring plan P1 T4): activity bar, top bar,
// search field, Studio-only toggles, profile menu. Every affordance runs
// a registered verb (data-verb carries the id); the markup is the
// designer prototype's ACTIVITY BAR / TOP BAR regions, inline styles
// extracted to nv- classes.
//
// US-012b (2026-08-29 dogfood, item 3): the topbar's own workspace
// dropdown (NV_WorkspaceMenu) retired - the rail tree is the switcher
// and already tints the selected row, so the two were redundant. Its
// "New workspace…" action was already duplicated by the rail's own
// header "+"; "Workspace settings…" had no other home, so item 5a gave
// it one on the rail's new workspace-row context menu
// (onOpenWorkspaceSettings, wired in nv-studio.jsx).

function NV_Logo() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
      <polygon points="12,3 21,12 12,21 3,12" fill="currentColor" fillOpacity="0.16" />
      <polygon points="12,3 16.5,7.5 12,12 7.5,7.5" fill="currentColor" />
      <polygon points="16.5,7.5 21,12 16.5,16.5 12,12" fill="currentColor" fillOpacity="0.4" />
      <polygon points="12,12 16.5,16.5 12,21 7.5,16.5" fill="var(--brand-green)" />
      <polygon points="7.5,7.5 12,12 7.5,16.5 3,12" fill="currentColor" fillOpacity="0.4" />
    </svg>
  );
}

function NV_run(con, id, arg) {
  var verb = con.registry.get(id);
  if (verb) verb.run(arg);
}

// Theme is a per-user preference (notes 1.1); there is no prefs endpoint
// (grepped primer/api/routers, primer/model - none), so localStorage keyed
// by username is the fallback the wiring notes call acceptable. This runs
// once username resolves and corrects any theme the shared tweaks store
// (ui/foundation/tweaks.js, one browser-wide key) applied before login.
function NV_themeStorageKey(username) {
  return "primer.theme." + (username || "anon");
}

function NV_ActivityBar() {
  var con = NV_useConsole();
  var initials = String(con.username || "?").slice(0, 2).toLowerCase();
  React.useEffect(function () {
    if (!con.username) return;
    try {
      var saved = localStorage.getItem(NV_themeStorageKey(con.username));
      var current = document.documentElement.getAttribute("data-theme");
      if (saved && saved !== current) {
        window.primerApi.setTweak("theme", saved);
        document.documentElement.setAttribute("data-theme", saved);
        con.bump();
      }
    } catch (_e) { /* private mode, quota, etc. - non-fatal */ }
  }, [con.username]);
  return (
    <div className="nv-actbar" data-testid="nv-actbar">
      <div className="nv-actbar-logo" title="primer"><NV_Logo /></div>
      <button type="button" className="nv-actbar-btn" title="Studio"
        data-verb="view.studio" data-testid="nv-go-studio"
        data-active={con.view.name === "studio" ? "true" : "false"}
        onClick={function () { NV_run(con, "view.studio"); }}>
        <svg width="17" height="17" viewBox="0 0 16 16" fill="none"
          stroke="currentColor" strokeWidth="1.3">
          <rect x="1.5" y="2" width="13" height="12" rx="1.5" />
          <path d="M5.5 2v12M10.5 8.5V14M10.5 2v3.5h4" />
        </svg>
      </button>
      <button type="button" className="nv-actbar-btn" title="Platform"
        data-verb="view.platform" data-testid="nv-go-platform"
        data-active={con.view.name === "platform" ? "true" : "false"}
        onClick={function () { NV_run(con, "view.platform"); }}>
        <svg width="17" height="17" viewBox="0 0 16 16" fill="none"
          stroke="currentColor" strokeWidth="1.3">
          <rect x="1.5" y="1.5" width="5.4" height="5.4" rx="1" />
          <rect x="9.1" y="1.5" width="5.4" height="5.4" rx="1" />
          <rect x="1.5" y="9.1" width="5.4" height="5.4" rx="1" />
          <rect x="9.1" y="9.1" width="5.4" height="5.4" rx="1" />
        </svg>
      </button>
      <div className="nv-actbar-spacer" />
      <button type="button" className="nv-avatar" title="Profile & system"
        data-testid="nv-profile-btn"
        onClick={function (ev) { ev.stopPropagation(); con.toggleMenu("profile"); }}>
        {initials}
      </button>
    </div>
  );
}

function NV_ProfileMenu() {
  var con = NV_useConsole();
  var theme = document.documentElement.getAttribute("data-theme") || "dark";
  function setTheme(next) {
    window.primerApi.setTweak("theme", next);
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem(NV_themeStorageKey(con.username), next);
    } catch (_e) { /* private mode, quota, etc. - non-fatal */ }
    con.bump();
  }
  return (
    <div className="nv-profile-menu" data-testid="nv-profile-menu"
      onClick={function (ev) { ev.stopPropagation(); }}>
      <div className="nv-profile-head">
        <div className="nv-avatar">{String(con.username || "?").slice(0, 2).toLowerCase()}</div>
        <div style={{ minWidth: 0 }}>
          <div className="nv-profile-name">{con.username}</div>
          <div className="nv-profile-role">{con.role}</div>
        </div>
      </div>
      <div className="nv-seg" data-testid="nv-theme-seg">
        <button type="button" data-active={theme === "dark" ? "true" : "false"}
          onClick={function () { setTheme("dark"); }}>Dark</button>
        <button type="button" data-active={theme === "light" ? "true" : "false"}
          onClick={function () { setTheme("light"); }}>Light</button>
      </div>
      <div className="nv-menu-sep" />
      {con.role !== "restricted" ? (
        <button type="button" className="nv-menu-row"
          data-verb="view.system" data-testid="nv-go-system"
          onClick={function () {
            con.toggleMenu(null);
            NV_run(con, "view.system");
          }}>System settings</button>
      ) : null}
      <button type="button" className="nv-menu-row" data-testid="nv-logout"
        onClick={function () {
          fetch("/v1/auth/logout", { method: "POST" }).then(
            function () { window.location.reload(); },
            function () { window.location.reload(); }
          );
        }}>Log out</button>
    </div>
  );
}

function NV_Topbar() {
  var con = NV_useConsole();
  return (
    <div className="nv-topbar" data-testid="nv-topbar">
      <div className="nv-search-wrap">
        <button type="button" className="nv-search-btn" data-verb="palette.open"
          data-testid="nv-search-btn"
          onClick={function () { NV_run(con, "palette.open"); }}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none"
            stroke="currentColor" strokeWidth="1.4">
            <circle cx="5.2" cy="5.2" r="3.6" />
            <path d="M8 8 11 11" />
          </svg>
          <span className="nv-search-hint">Search, or run a command</span>
          <kbd className="nv-kbd">Ctrl+K</kbd>
        </button>
      </div>
      {con.view.name === "studio" ? (
        <div className="nv-topbar-toggles">
          <button type="button" className="nv-topbar-toggle"
            title="Toggle terminal" data-verb="terminal.toggle"
            data-testid="nv-toggle-terminal"
            data-active={con.panels.terminal ? "true" : "false"}
            onClick={function () { NV_run(con, "terminal.toggle"); }}>
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none"
              stroke="currentColor" strokeWidth="1.3">
              <rect x="1" y="2" width="12" height="10" rx="1.5" />
              <path d="M3.5 5.5 6 7.5 3.5 9.5M7.5 9.5h3" />
            </svg>
          </button>
        </div>
      ) : null}
      {con.openMenu === "profile" ? <NV_ProfileMenu /> : null}
    </div>
  );
}

window.NV_ActivityBar = NV_ActivityBar;
window.NV_Topbar = NV_Topbar;
