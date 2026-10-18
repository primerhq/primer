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
// by username is the fallback the wiring notes call acceptable.
//
// Three choices (platform revamp D3): Light, Dark and System. The choice is
// stored per user under primer.theme.<username> as light | dark | system, and
// mirrored into the browser-wide tweak (ui/foundation/tweaks.js) as light |
// dark | null, where null is "follow the OS": the inline bootstrap of
// index.html and tweaks.js's matchMedia listener already read null that way,
// on a reload and live, so System needs no code in either of them. What the
// style sheet keys on, <html data-theme>, is only ever light or dark: no
// token block defines "system", so painting the word would drop every colour.
var NV_THEME_CHOICES = [
  { value: "light", label: "Light", icon: "☀" },
  { value: "dark", label: "Dark", icon: "☾" },
  { value: "system", label: "System", icon: "⌘" },
];

function NV_themeStorageKey(username) {
  return "primer.theme." + (username || "anon");
}

function NV_isThemeChoice(value) {
  return NV_THEME_CHOICES.some(function (c) { return c.value === value; });
}

// The scheme the OS prefers now. Dark without matchMedia, as the bootstrap
// script of index.html resolves it.
function NV_osTheme() {
  try {
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) return "light";
  } catch (_e) { /* an engine without the query: dark, as the bootstrap */ }
  return "dark";
}

// The value data-theme gets for a choice: light or dark, always.
function NV_resolveTheme(choice) {
  return choice === "light" || choice === "dark" ? choice : NV_osTheme();
}

// The user's choice as stored: their own key, else the browser-wide light or
// dark an older version of this menu left in the tweak, else System (nothing
// stored means the page follows the OS, which is what System says). Never
// throws: a blocked localStorage or a corrupt value reads as System.
function NV_themeChoice(username) {
  try {
    var own = localStorage.getItem(NV_themeStorageKey(username));
    if (NV_isThemeChoice(own)) return own;
    var tweaks = JSON.parse(localStorage.getItem("primer.tweaks") || "{}");
    if (tweaks && (tweaks.theme === "light" || tweaks.theme === "dark")) return tweaks.theme;
  } catch (_e) { /* private mode, a value that is not JSON, etc. */ }
  return "system";
}

// The one place a choice takes effect: the tweak (null for System), the
// attribute the style sheet reads, the user's own key, and a re-render so the
// menu shows the pressed choice. env is { username, bump() }: the console
// context has both, and so does the shell's themeEnvRef for the palette verbs.
// A value that is not a choice is System, never painted as it is.
function NV_applyThemeChoice(env, choice) {
  var picked = NV_isThemeChoice(choice) ? choice : "system";
  window.primerApi.setTweak("theme", picked === "system" ? null : picked);
  document.documentElement.setAttribute("data-theme", NV_resolveTheme(picked));
  try {
    localStorage.setItem(NV_themeStorageKey(env.username), picked);
  } catch (_e) { /* private mode, quota, etc. - non-fatal */ }
  env.bump();
}

// Applies the user's stored choice once the username resolves (and again for
// the next user): the tweaks store is one browser-wide key, so it may still
// hold whoever used this browser last. A user who never chose keeps what the
// page painted. The desktop (NV_ActivityBar) and the phone (NV_MobileShell)
// both call it; before D3 only the desktop restored, so a phone kept the last
// account's colour.
function NV_useThemeRestore(con) {
  React.useEffect(function () {
    if (!con.username) return;
    try {
      var saved = localStorage.getItem(NV_themeStorageKey(con.username));
      if (NV_isThemeChoice(saved)) NV_applyThemeChoice(con, saved);
    } catch (_e) { /* private mode, quota, etc. - non-fatal */ }
  }, [con.username]);
}

// "<glyph> Name" for one choice. The glyph is decoration, so a button's
// accessible name is its label alone.
function NV_ThemeChoiceLabel(props) {
  var choice = NV_THEME_CHOICES.filter(function (c) { return c.value === props.value; })[0];
  if (!choice) return null;
  return (
    <React.Fragment>
      <span aria-hidden="true">{choice.icon}</span>{" "}{choice.label}
    </React.Fragment>
  );
}

function NV_ActivityBar() {
  var con = NV_useConsole();
  var initials = String(con.username || "?").slice(0, 2).toLowerCase();
  NV_useThemeRestore(con);
  return (
    <div className="nv-actbar" data-testid="nv-actbar" role="navigation" aria-label="Views">
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

// C-027: an open menu closes on Escape and gives focus back to the button that opened it, and closes when focus moves to something outside it and
// its button (a keyboard user who tabs out of a menu used to leave it hanging open over the page). It is on the Escape stack (foundation/escape-stack.js)
// only while open, so an Escape closes the menu and nothing under it, and with no menu open the palette and the overlays hear it.
function NV_useMenuDismiss(con, name, triggerTestId) {
  var open = con.openMenu === name;
  window.primerApi.useEscape(function (ev) {
    ev.preventDefault();
    con.toggleMenu(null);
    var trigger = document.querySelector('[data-testid="' + triggerTestId + '"]');
    if (trigger) trigger.focus();
  }, open);
}

// Focus moved to another element (relatedTarget is that element; null is a click on a non-focusable part of the menu, which stays) that is neither
// inside the menu's container nor the button that opened it: close.
function NV_menuFocusLeft(ev, triggerTestId) {
  var next = ev.relatedTarget;
  if (!next || ev.currentTarget.contains(next)) return false;
  return !(next.getAttribute && next.getAttribute("data-testid") === triggerTestId);
}

function NV_ProfileMenu() {
  var con = NV_useConsole();
  var choice = NV_themeChoice(con.username);
  return (
    <div className="nv-profile-menu" data-testid="nv-profile-menu"
      onClick={function (ev) { ev.stopPropagation(); }}
      onBlur={function (ev) { if (NV_menuFocusLeft(ev, "nv-profile-btn")) con.toggleMenu(null); }}>
      <div className="nv-profile-head">
        <div className="nv-avatar">{String(con.username || "?").slice(0, 2).toLowerCase()}</div>
        <div style={{ minWidth: 0 }}>
          <div className="nv-profile-name">{con.username}</div>
          <div className="nv-profile-role">{con.role}</div>
        </div>
      </div>
      {/* The three buttons are written out, not mapped from NV_THEME_CHOICES, so each carries a literal data-verb the dual-render guard can read
          (tests/ui/test_shell_dual_render_guard.py): the pointer affordances of the theme.* palette verbs. */}
      <div className="nv-theme-row" data-testid="nv-theme-row">
        <div className="nv-theme-label" id="nv-theme-label">Theme</div>
        <div className="nv-seg" data-testid="nv-theme-seg" role="group" aria-labelledby="nv-theme-label">
          <button type="button" data-testid="nv-theme:light" data-verb="theme.light"
            data-active={choice === "light" ? "true" : "false"}
            aria-pressed={choice === "light" ? "true" : "false"}
            onClick={function () { NV_applyThemeChoice(con, "light"); }}>
            <NV_ThemeChoiceLabel value="light" />
          </button>
          <button type="button" data-testid="nv-theme:dark" data-verb="theme.dark"
            data-active={choice === "dark" ? "true" : "false"}
            aria-pressed={choice === "dark" ? "true" : "false"}
            onClick={function () { NV_applyThemeChoice(con, "dark"); }}>
            <NV_ThemeChoiceLabel value="dark" />
          </button>
          <button type="button" data-testid="nv-theme:system" data-verb="theme.system"
            data-active={choice === "system" ? "true" : "false"}
            aria-pressed={choice === "system" ? "true" : "false"}
            onClick={function () { NV_applyThemeChoice(con, "system"); }}>
            <NV_ThemeChoiceLabel value="system" />
          </button>
        </div>
      </div>
      <div className="nv-menu-sep" />
      <button type="button" className="nv-menu-row"
        data-verb="view.profile" data-testid="nv-go-profile"
        onClick={function () {
          con.toggleMenu(null);
          NV_run(con, "view.profile");
        }}>Your profile</button>
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
      <button type="button" className="nv-menu-row" data-testid="nv-logout-all"
        title="End every session of this account, on every device"
        onClick={async function () {
          con.toggleMenu(null);
          // Revokes every cookie of the account (SEC-05), not only this one, so it confirms first.
          const ok = await window.confirmDialog({
            title: "Sign out everywhere",
            message: "This ends every session of your account, on every browser and device, including this one. API tokens are not affected.",
            confirmLabel: "Sign out everywhere", danger: true,
          });
          if (!ok) return;
          fetch("/v1/auth/logout-all", { method: "POST" }).then(
            function () { window.location.reload(); },
            function () { window.location.reload(); }
          );
        }}>Sign out everywhere</button>
    </div>
  );
}

// uiv2 Wave 1 (01a06431): the workspace-selector chip un-retires the
// topbar switcher US-012b (2026-08-29) folded into the rail tree alone
// ("the two were redundant"). The mockup shows both coexisting - the
// chip is an always-visible, rail-scroll-independent entry point; the
// rail stays the detailed browse/session view. No new switching logic:
// both the row click and the footer "+ New workspace…" run the SAME
// workspace.switch/workspace.create verbs the rail already uses, and
// this also fixes a live dead path - workspace.switch's own run(),
// called with no wid (palette row, Ctrl+Shift+o), already did
// setOpenMenu("ws") expecting a listener; nothing rendered against
// "ws" until now, so the chord/palette verb was silently a no-op.
function NV_WorkspaceChip() {
  var con = NV_useConsole();
  NV_useMenuDismiss(con, "ws", "nv-ws-chip");
  var workspaces = con.workspaces || [];
  var active = workspaces.filter(function (w) { return w.id === con.wid; })[0];
  return (
    <div className="nv-ws-wrap"
      onBlur={function (ev) { if (con.openMenu === "ws" && NV_menuFocusLeft(ev, "nv-ws-chip")) con.toggleMenu(null); }}>
      <button type="button" className="nv-ws-chip" title="Switch workspace"
        data-verb="workspace.switch" data-testid="nv-ws-chip"
        onClick={function (ev) { ev.stopPropagation(); con.toggleMenu("ws"); }}>
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none"
          stroke="currentColor" strokeWidth="1.3" style={{ flexShrink: 0 }}>
          <path d="M1.5 3.5 6 1l4.5 2.5v5L6 11 1.5 8.5Z M6 6v5M1.5 3.5 6 6l4.5-2.5" />
        </svg>
        <span className="nv-ws-chip-name">{active ? (active.name || active.id) : "Workspace"}</span>
        <svg width="9" height="9" viewBox="0 0 10 10" fill="none" stroke="currentColor"
          strokeWidth="1.5" style={{ flexShrink: 0 }}>
          <path d="M2.5 3.5 5 6.5 7.5 3.5" />
        </svg>
      </button>
      {con.openMenu === "ws" ? (
        <div className="nv-ws-menu" data-testid="nv-ws-menu"
          onClick={function (ev) { ev.stopPropagation(); }}>
          {workspaces.map(function (w) {
            return (
              <button type="button" key={w.id} className="nv-ws-menu-row"
                data-testid={"nv-ws-menu-row:" + w.id}
                data-current={w.id === con.wid ? "true" : "false"}
                onClick={function () {
                  con.toggleMenu(null);
                  var verb = con.registry.get("workspace.switch");
                  if (verb) verb.run({ wid: w.id });
                }}>
                <span className="nv-ws-menu-name">{w.name || w.id}</span>
                {w.name ? <span className="nv-ws-menu-id mono">{w.id}</span> : null}
              </button>
            );
          })}
          <div className="nv-menu-sep" />
          <button type="button" className="nv-ws-menu-new"
            data-verb="workspace.create" data-testid="nv-ws-menu-new"
            onClick={function () {
              con.toggleMenu(null);
              var verb = con.registry.get("workspace.create");
              if (verb) verb.run();
            }}>+ New workspace…</button>
        </div>
      ) : null}
    </div>
  );
}

function NV_Topbar() {
  var con = NV_useConsole();
  NV_useMenuDismiss(con, "profile", "nv-profile-btn");
  return (
    <div className="nv-topbar" data-testid="nv-topbar" role="banner">
      {con.view.name === "studio" ? <NV_WorkspaceChip /> : null}
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
// The theme helpers: the palette verbs (nv-shell.jsx) call NV_applyThemeChoice, and the phone's More tab (nv-mobile-shell.jsx) uses the rest.
window.NV_THEME_CHOICES = NV_THEME_CHOICES;
window.NV_osTheme = NV_osTheme;
window.NV_resolveTheme = NV_resolveTheme;
window.NV_themeChoice = NV_themeChoice;
window.NV_applyThemeChoice = NV_applyThemeChoice;
window.NV_useThemeRestore = NV_useThemeRestore;
window.NV_ThemeChoiceLabel = NV_ThemeChoiceLabel;
