// primer UI - useRouter, re-implemented on top of overlay state.
//
// Pinned decision 14. Eight of the thirteen re-hosted pages read
// window.primerApi.useRouter() for params.id or navigate:
//
//   providers.jsx:268-270   agents.jsx:48-50      graphs.jsx:239-241
//   triggers.jsx:151-154    toolsets.jsx:50-52    harnesses.jsx:40-43
//   workspaces.jsx:55-57    knowledge.jsx:33-35
//
// P5 deletes foundation/router.js, but the NAME has to survive or every
// one of those pages breaks the moment it is opened. So the shell
// publishes the same {path, params, query, navigate} contract, sourced
// from the active overlay's <name>:<section>:<id> segments:
//
//   overlay=providers:tts:pv-1  ->  path "/providers/tts/pv-1",
//                                   params {section: "tts", id: "pv-1"}
//
// navigate(path) is translated back into openOverlay, so a page that
// "navigates" to a detail view deep-links the overlay instead of
// rewriting the shell's hash out from under it.

function SH_shimPath(overlay) {
  if (!overlay || !overlay.name) return "/";
  var path = "/" + overlay.name;
  if (overlay.section) path += "/" + overlay.section;
  if (overlay.id) path += "/" + overlay.id;
  return path;
}

function SH_shimParams(overlay) {
  var params = {};
  if (!overlay) return params;
  if (overlay.section) params.section = overlay.section;
  if (overlay.id) params.id = overlay.id;
  return params;
}

// The re-hosted detail pages carry TABS, and each states its tab in the
// query rather than the path: agents.jsx navigates to
// "/agents/<id>?tab=tools", toolsets.jsx passes {tab} as a second
// argument. Both forms mean the same thing, so both are read here.
function SH_shimTab(path, opts) {
  if (opts && opts.tab) return String(opts.tab);
  var q = String(path || "").split("?")[1];
  if (!q) return null;
  var pairs = q.split("&");
  for (var i = 0; i < pairs.length; i++) {
    var kv = pairs[i].split("=");
    if (kv[0] === "tab" && kv[1]) return decodeURIComponent(kv[1]);
  }
  return null;
}

// A page calling navigate("/providers/llm/pv-9") means "show me this
// deeper thing", not "leave the shell". Segment 0 is the overlay name
// when it is one we host, otherwise the current overlay keeps its name
// and the remaining segments become section/id.
//
// A tab takes the section slot, because a tabbed page has only one
// record open and so has no second use for it. That keeps ONE grammar
// for the URL rather than growing a fourth segment nothing else needs.
function SH_shimQuery(overlay) {
  var query = {};
  if (overlay && overlay.section) query.tab = overlay.section;
  return query;
}

// The path's non-empty segments ("/agents/a1?tab=tools" -> ["agents", "a1"]).
function SH_shimSegments(path) {
  var parts = String(path || "").split("?")[0].split("/");
  var clean = [];
  for (var i = 0; i < parts.length; i++) if (parts[i]) clean.push(parts[i]);
  return clean;
}

// Where a BARE list path goes (revamp design 10, D1). A detail page's Delete and Back call navigate("/<plural>"); read as "open that overlay with
// no record" that rendered the entity's LIST page inside the overlay, a second copy of the Platform page underneath it, with its own Create
// button. A list never renders in an overlay: the path means "close the overlay and select the entity's Platform page", and the Workspaces page,
// which the Platform view no longer has, means the studio.
//
// The key is the cleaned path joined by "/". Two rows are the workspace-shaped sections, whose lists are two segments: a template's Delete lands on
// the templates page, a workspace provider's on the Providers page. The provider row also names the provider type in the view's `sub` slot
// ("workspace"); the address carries a type only once the provider-type grammar lands (Plan 01 Task 6), and until then the user lands on the Providers
// page itself. The rows are exact paths: a path with a record after them ("workspaces/templates/<id>") is a record, as before.
//
// The cost: navigate("/workspaces/templates") and ("/workspaces/providers") can no longer open a workspace whose id is exactly "templates" or
// "providers". templates.jsx navigates to /workspaces/<id> for every workspace it lists, so such a workspace would land on the templates page from
// there. Ids the console creates are allocated (ws-<hex>); the API accepts a chosen DNS-1123 id, so it takes a hand-chosen one. The address form
// overlay=workspaces::templates still opens it.
//
// A surface that has its own Platform page adds its row here: the Providers catalogue (Plan 02) is not in the table yet, so navigate("/providers")
// still opens the catalogue overlay.
var SH_SHIM_LIST_TARGETS = {
  agents: { name: "platform", nav: "agents" },
  graphs: { name: "platform", nav: "graphs" },
  toolsets: { name: "platform", nav: "toolsets" },
  triggers: { name: "platform", nav: "triggers" },
  harnesses: { name: "platform", nav: "harnesses" },
  services: { name: "platform", nav: "services" },
  approvals: { name: "platform", nav: "approvals" },
  workspaces: { name: "studio", nav: null },
  "workspaces/templates": { name: "platform", nav: "templates" },
  "workspaces/providers": { name: "platform", nav: "providers", sub: "workspace" },
};

// Until the Platform Workspace templates page ships, its legacy list overlay
// remains the only real templates list. Once the component exists, the same
// bare path closes to Platform instead. A tab request is never a list request.
function SH_shimUsesTemplateListOverlay(path, opts) {
  var clean = SH_shimSegments(path);
  return clean.join("/") === "workspaces/templates"
    && !SH_shimTab(path, opts)
    && typeof window.WT_TemplatesPage !== "function";
}

// The view a bare list path closes to, or null. A copy, so a caller cannot edit the table. A tab is the page asking for more than the list.
function SH_shimListTarget(path, opts) {
  var clean = SH_shimSegments(path);
  if (!clean.length || SH_shimTab(path, opts)) return null;
  var key = clean.join("/");
  if (key === "workspaces/templates" && SH_shimUsesTemplateListOverlay(path, opts)) return null;
  if (!Object.prototype.hasOwnProperty.call(SH_SHIM_LIST_TARGETS, key)) return null;
  var target = SH_SHIM_LIST_TARGETS[key];
  return { name: target.name, nav: target.nav || null, sub: target.sub || null };
}

// "/sessions/<id>" is a studio document, not an overlay: a session row on an agent's, a toolset's or a workspace's detail. Read as an overlay path it
// reopened the CURRENT overlay with the session id in its tab slot. Returns the id, or null.
function SH_shimSessionId(path) {
  var clean = SH_shimSegments(path);
  return clean.length === 2 && clean[0] === "sessions" ? clean[1] : null;
}

// The session's own workspace, when the caller has it: navigate("/sessions/<id>", { workspace_id: row.workspace_id }). A path cannot carry it, and a
// session row does; without it the shell falls back to the workspace its rail cache knows, then to the one already selected.
function SH_shimSessionWorkspace(opts) {
  return opts && opts.workspace_id ? String(opts.workspace_id) : null;
}

// closeTo(view) and openSession(sid, workspaceId) are the shell's two navigations that leave the overlay (con.closeOverlayTo,
// con.openSessionInStudio). A host that passes neither keeps the old behaviour for those paths.
function SH_shimNavigate(overlay, openOverlay, path, opts, closeTo, openSession) {
  var clean = SH_shimSegments(path);
  if (!clean.length) return;
  if (SH_shimUsesTemplateListOverlay(path, opts)) {
    openOverlay("workspaces", "templates", null);
    return;
  }
  if (typeof closeTo === "function") {
    var target = SH_shimListTarget(path, opts);
    if (target) {
      closeTo(target);
      return;
    }
  }
  if (typeof openSession === "function") {
    var sid = SH_shimSessionId(path);
    if (sid) {
      openSession(sid, SH_shimSessionWorkspace(opts));
      return;
    }
  }
  var known = window.SH_OVERLAYS || [];
  var name = overlay && overlay.name;
  var rest = clean;
  for (var k = 0; k < known.length; k++) {
    if (known[k] === clean[0]) { name = clean[0]; rest = clean.slice(1); break; }
  }
  if (!name) return;
  var tab = SH_shimTab(path, opts);
  if (tab) {
    openOverlay(name, tab, rest[0] || (overlay && overlay.id) || null);
    return;
  }
  // Two trailing segments are section + record, as the grammar reads.
  // ONE is the record, not the section: every page that reads this back
  // reads params.id (agents, triggers, toolsets, harnesses, workspaces),
  // and nothing outside the shell reads params.section at all, since the
  // section slot is what carries the tab. Filling section first meant a
  // page navigating to its own detail view handed the record to a slot
  // its own reader never looks at, so the detail view opened with no
  // record and the shell had nothing to title the page with.
  if (rest.length > 1) {
    openOverlay(name, rest[0], rest[1]);
    return;
  }
  openOverlay(name, null, rest[0] || null);
}

function SH_installRouterShim(getOverlay, openOverlay, closeTo, openSession) {
  var ns = (window.primerApi = window.primerApi || {});
  ns.useRouter = function () {
    var overlay = getOverlay();
    return {
      path: SH_shimPath(overlay),
      params: SH_shimParams(overlay),
      // The section slot doubles as the tab for a tabbed detail page.
      // A page that reads params.section (providers reads a class) is
      // untouched; a page that reads query.tab now gets an answer
      // instead of always falling back to its first tab.
      query: SH_shimQuery(overlay),
      navigate: function (path, opts) {
        SH_shimNavigate(getOverlay(), openOverlay, path, opts, closeTo, openSession);
      },
    };
  };
  // routes/matchRoute existed on the old namespace; a re-hosted page that
  // reads them must get an empty answer rather than a TypeError.
  ns.routes = ns.routes || [];
  ns.matchRoute = ns.matchRoute || function () { return null; };
}

window.SH_shimPath = SH_shimPath;
window.SH_shimParams = SH_shimParams;
window.SH_shimQuery = SH_shimQuery;
window.SH_SHIM_LIST_TARGETS = SH_SHIM_LIST_TARGETS;
window.SH_shimListTarget = SH_shimListTarget;
window.SH_shimSessionId = SH_shimSessionId;
window.SH_shimSessionWorkspace = SH_shimSessionWorkspace;
window.SH_installRouterShim = SH_installRouterShim;
