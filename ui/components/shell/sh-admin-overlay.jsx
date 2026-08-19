/* global React, SH_useShell */
// The admin/config overlay: the ONLY overlay designed from scratch.
//
// Section 8 is specific: one search-first surface, common settings at
// level one, "Advanced" collapsed, admin sections role-gated, every
// setting palette-addressable. Four shipped pages become sections of it
// rather than four separate overlays, and the S5 wizard is re-hostable
// here for re-runs (amendment C5).

var SH_ADMIN_SECTIONS = [
  {
    id: "api-tokens",
    title: "API Tokens",
    level: 1,
    roles: ["admin", "user"],
    keywords: "token tokens api key personal access",
    render: function () { return <window.AT_ApiTokensPage />; },
  },
  {
    id: "mcp",
    title: "MCP Server",
    level: 1,
    roles: ["admin"],
    keywords: "mcp server exposure tools endpoint",
    render: function () { return <window.MC_McpPage />; },
  },
  {
    id: "users",
    title: "Users",
    level: 1,
    roles: ["admin"],
    keywords: "user users account accounts role roles password",
    render: function () { return <window.ADM_AdminUsersPage />; },
  },
  {
    id: "sso",
    title: "SSO Providers",
    level: 2,
    roles: ["admin"],
    keywords: "sso oidc saml identity provider login",
    render: function () { return <window.SSO_ProvidersPage />; },
  },
  {
    id: "setup",
    title: "Setup Wizard",
    level: 2,
    roles: ["admin"],
    keywords: "setup wizard bootstrap operator first run rerun",
    render: function (shell) {
      return (
        <window.SetupWizardSteps
          onComplete={function () { shell.toast("Setup re-run complete"); }}
        />
      );
    },
  },
];

// Pure: a section matches when the query is empty or hits its title or
// keywords, and the caller's role is one the section allows.
function SH_searchAdmin(sections, query, role) {
  var q = String(query || "").toLowerCase().trim();
  var out = [];
  for (var i = 0; i < sections.length; i++) {
    var section = sections[i];
    if (section.roles.indexOf(role) < 0) continue;
    if (q) {
      var hay = (section.title + " " + section.keywords).toLowerCase();
      if (hay.indexOf(q) < 0) continue;
    }
    out.push(section);
  }
  return out;
}

// One verb per section, so every setting is reachable from the palette
// and each verb also renders as an overlay button (dual-render rule).
function SH_registerAdminVerbs(shell) {
  for (var i = 0; i < SH_ADMIN_SECTIONS.length; i++) {
    (function (section) {
      var id = "overlay.open.admin." + section.id;
      if (shell.registry.get(id)) return;
      shell.registry.register({
        id: id,
        label: "Open " + section.title,
        surfaces: ["overlay-button", "palette"],
        run: function () { shell.openOverlay("admin", section.id); },
      });
    })(SH_ADMIN_SECTIONS[i]);
  }
}

function SH_AdminOverlay(props) {
  var shell = SH_useShell();
  var queryState = React.useState("");
  var query = queryState[0];
  var setQuery = queryState[1];

  React.useEffect(function () { SH_registerAdminVerbs(shell); }, []);

  var visible = SH_searchAdmin(SH_ADMIN_SECTIONS, query, shell.role);
  var focused = props.section
    ? visible.filter(function (s) { return s.id === props.section; })
    : visible;
  var common = focused.filter(function (s) { return s.level === 1; });
  var advanced = focused.filter(function (s) { return s.level !== 1; });

  function panel(section) {
    return (
      <section key={section.id} className="sh-admin-section"
        data-testid={"shell-admin-section:" + section.id}>
        <h4>{section.title}</h4>
        {section.render(shell)}
      </section>
    );
  }

  return (
    <div className="sh-admin" data-testid="shell-admin">
      <input
        type="search"
        className="sh-admin-search"
        data-testid="shell-admin-search"
        placeholder="Search settings"
        value={query}
        onChange={function (ev) { setQuery(ev.target.value); }}
      />
      {common.map(panel)}
      {advanced.length ? (
        <details className="sh-admin-advanced"
          data-testid="shell-admin-advanced" open={!!query}>
          <summary>Advanced</summary>
          {advanced.map(panel)}
        </details>
      ) : null}
      {!focused.length ? (
        <p className="sh-empty">
          No setting matches that. Press Ctrl+K to search verbs instead.
        </p>
      ) : null}
    </div>
  );
}

window.SH_ADMIN_SECTIONS = SH_ADMIN_SECTIONS;
window.SH_searchAdmin = SH_searchAdmin;
window.SH_registerAdminVerbs = SH_registerAdminVerbs;
window.SH_AdminOverlay = SH_AdminOverlay;
