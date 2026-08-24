/* global React, NV_useConsole */
// The three-view chrome (wiring plan P1 T4): activity bar, top bar,
// workspace selector, search field, Studio-only toggles, profile menu.
// Every affordance runs a registered verb (data-verb carries the id);
// the markup is the designer prototype's ACTIVITY BAR / TOP BAR
// regions, inline styles extracted to nv- classes.

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

function NV_ActivityBar() {
  var con = NV_useConsole();
  var initials = String(con.username || "?").slice(0, 2).toLowerCase();
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

function NV_WorkspaceMenu() {
  var con = NV_useConsole();
  var rows = con.workspaces || [];
  return (
    <div className="nv-menu" data-testid="nv-ws-menu"
      onClick={function (ev) { ev.stopPropagation(); }}>
      {/* The workspace list scrolls; settings/new stay pinned below.
          Without the scroll region a long install pushed both action
          rows off-viewport, and the shell never scrolls, so they were
          unreachable (BDD pass 2026-08-24). */}
      <div className="nv-menu-scroll">
        {rows.map(function (w) {
          return (
            <button type="button" key={w.id} className="nv-menu-row"
              data-current={w.id === con.wid ? "true" : "false"}
              data-testid={"nv-ws-row:" + w.id}
              onClick={function () {
                con.toggleMenu(null);
                NV_run(con, "workspace.switch", { wid: w.id });
              }}>
              <span style={{ flex: 1, fontWeight: w.id === con.wid ? 600 : 400 }}>
                {w.name || w.label || w.id}
              </span>
              <span className="nv-menu-id">{String(w.id).slice(0, 10)}</span>
            </button>
          );
        })}
      </div>
      <div className="nv-menu-sep" />
      <button type="button" className="nv-menu-row"
        data-testid="nv-ws-settings"
        onClick={function () {
          con.toggleMenu(null);
          // The workspace's own tabs (config / channels / log / destroy)
          // open IN PLACE as the shared overlay: the open session tab
          // survives underneath.
          con.openOverlay("workspaces", "detail", con.wid);
        }}>
        <span>Workspace settings…</span>
      </button>
      <button type="button" className="nv-menu-row nv-menu-new"
        data-verb="workspace.create" data-testid="nv-ws-new"
        onClick={function () {
          con.toggleMenu(null);
          NV_run(con, "workspace.create");
        }}>
        <span style={{ fontSize: 14, lineHeight: 1 }}>+</span>
        <span>New workspace…</span>
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
  var ws = (con.workspaces || []).find(function (w) { return w.id === con.wid; });
  return (
    <div className="nv-topbar" data-testid="nv-topbar">
      <div className="nv-ws-wrap">
        <button type="button" className="nv-ws-btn" data-testid="nv-ws-btn"
          onClick={function (ev) { ev.stopPropagation(); con.toggleMenu("ws"); }}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none"
            stroke="var(--text-3)" strokeWidth="1.3">
            <path d="M1.5 3.5 6 1l4.5 2.5v5L6 11 1.5 8.5Z M6 6v5M1.5 3.5 6 6l4.5-2.5" />
          </svg>
          <span>{(ws && (ws.name || ws.label || ws.id)) || con.wid || "workspace"}</span>
          <svg width="9" height="9" viewBox="0 0 10 10" fill="none"
            stroke="var(--text-3)" strokeWidth="1.5">
            <path d="M2 3.5 5 6.5 8 3.5" />
          </svg>
        </button>
        {con.openMenu === "ws" ? <NV_WorkspaceMenu /> : null}
      </div>
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
          <button type="button" className="nv-topbar-toggle"
            title="Toggle workspace events" data-verb="events.toggle"
            data-testid="nv-toggle-events"
            data-active={con.panels.events ? "true" : "false"}
            onClick={function () { NV_run(con, "events.toggle"); }}>
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none"
              stroke="currentColor" strokeWidth="1.3">
              <path d="M1.5 7h2.6l1.6-4 2.6 8 1.6-4h2.6" />
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
