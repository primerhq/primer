/* global React, SH_api, NV_useConsole, NV_identity, NV_GRAPH_GLYPH */
// The Platform view (wiring plan P4 T10): grouped left nav + one
// config-driven card page per entity, ported from the prototype's
// PLATFORM region. Cards open the SHARED overlays (the same component
// whether reached from a card, the palette, or a pasted link), create
// buttons open the entity's own form, hosted on this page
// (tests/ui/test_platform_create_opens_the_form.py tabulates which), delete is
// inline with confirm - a referenced entity's 409/422 surfaces as the refusal
// toast. Two nav ids are custom pages (seam I-2): "web" and "templates" mount
// a component another file exports on window, and their sidebar row exists
// only while that component is loaded.
//
// Providers get the family pills over the REAL per-class plurals (the
// S4 catalog's registry) and open the catalog overlay at that class;
// approvals get the decisions audit table under the policy cards.

var NV_PLAT_ICONS = {
  providers: "M2 4.5 7 2l5 2.5-5 2.5Z M2 7l5 2.5L12 7 M2 9.5 7 12l5-2.5",
  profiles: "M2 4.5h6.5 M10.5 4.5H12 M8.5 2.8v3.4 M2 9.5h1.5 M5.5 9.5H12 "
    + "M5.5 7.8v3.4",
  agents: "M7 1.5 12.5 7 7 12.5 1.5 7Z",
  graphs: "M1.5 2.5h4v3.4h-4Z M8.5 8.1h4v3.4h-4Z M5.5 4.2h3v5.8",
  toolsets: "M9.8 1.8a3 3 0 0 0-3.4 4L2 10.2 3.8 12l4.4-4.4a3 3 0 0 0 "
    + "4-3.4L10 6.4 7.6 4Z",
  collections: "M3.5 1.5h7v11h-5.5a1.5 1.5 0 0 0-1.5 1.5Z M3.5 1.5V14",
  tools: "M5.5 2.5h3v9h-3Z M2 5.5h3 M9 5.5h3 M2 8.5h3 M9 8.5h3",
  templates: "M2 2h10v3H2Z M2 6.5h4.5V12H2Z M8 6.5h4V12H8Z",
  triggers: "M8 1 3 8h3.5L6 13l5-7H7.5Z",
  channels: "M2 2.5h10v7H6L3 12V9.5H2Z",
  harnesses: "M4 4.2a1.4 1.4 0 1 0 0-.01 M4 11.2a1.4 1.4 0 1 0 0-.01 "
    + "M10.5 4.2a1.4 1.4 0 1 0 0-.01 M4 5.5v4.3 M10.5 5.5c0 2.8-6.5 "
    + "2-6.5 4.3",
  services: "M7 1.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11Z M1.5 7h11 "
    + "M7 1.5c2.4 2.6 2.4 8.4 0 11-2.4-2.6-2.4-8.4 0-11",
  // Web (a custom page): a wireframe globe, a circle cut by a meridian and two parallels, so it reads apart from services' globe (an equator and a lens).
  web: "M7 1.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11Z M7 1.5v11 "
    + "M2 4.8h10 M2 9.2h10",
  approvals: "M7 1.5 12 3v4c0 3-2.2 5-5 6-2.8-1-5-3-5-6V3Z "
    + "M4.8 7l1.6 1.6L9.6 5.4",
};

// The sidebar's groups (design 4.1), in the order of the grammar (ui/foundation/shell-url.js SH_VIEWS.platform, held equal by
// tests/ui/test_console_platform.py). "providers" mounts the catalogue and "web" and "templates" are custom pages (NV_PLAT_CUSTOM_PAGES);
// every other id is an NV_PLAT_PAGES entry. Workspaces is not here: the Workspaces page left the Platform view (design 14.1).
var NV_PLAT_GROUPS = [
  { label: "Intelligence", ids: ["providers", "profiles", "agents", "graphs"] },
  { label: "Workbench",
    ids: ["templates", "toolsets", "tools", "collections", "web"] },
  { label: "Automation", ids: ["triggers", "channels", "harnesses", "services"] },
  { label: "Governance", ids: ["approvals"] },
];

// IA restructure 01a04d6a: the old family-pill registry that used to
// live here (a deliberate module-local duplicate of provider-catalog.
// jsx's PROVIDER_CLASSES, per this file's own prior comment) is gone -
// the platform page no longer renders its own providers UI at all, it
// mounts window.ProviderCatalog inline (see NV_ProvidersPlatPage below),
// which owns that registry as the one real copy.

function NV_fact(k, v) {
  return v == null || v === "" ? null : [k, String(v)];
}

function NV_countOf(v) {
  if (Array.isArray(v)) return v.length;
  return typeof v === "number" ? v : null;
}

// The agents setup counts on: evaluate_setup_state (primer/bootstrap/
// setup_state.py) reports the install as not set up when either row is
// missing. tests/ui/test_platform_delete_confirm.py pins this list to
// that predicate, so a renamed reserved id cannot drift unnoticed.
var NV_SETUP_AGENT_IDS = ["operator", "builder"];

// What one card's delete prompt says. Deleting a setup agent locks every
// user out of the console, so that prompt names the consequence and the
// way back. The entities that HOLD data say what the delete takes with them
// (admin review ADM-31), each read from the code or doc that states it:
// docs/agents/workspaces.md (destroy, workspace_lost), docs/agents/knowledge.md
// (the delete cascade), docs/agents/triggers-and-subscriptions.md
// (trigger::delete cascade-deletes subscriptions) and the services router
// (every version and its artifacts go first). They have no "referenced"
// refusal, so the plain sentence would be untrue for them. Every other
// entity keeps the plain prompt.
function NV_deleteConfirm(nav, row) {
  var id = row.id || row.name;
  var message = "Permanently delete " + id
    + "? Referenced entities refuse deletion.";
  if (nav === "workspaces") {
    // Every backend deletes the files with the environment (docker: the
    // container AND its volume; local: rmtree of the root; kubernetes: the
    // PVC), and every session's history lives inside the workspace
    // (.state/sessions/<sid>/), so it goes too. A workspace whose local
    // provider is refused on this deployment loses only its row
    // (WorkspaceRegistry.destroy); the card cannot tell the two apart.
    message = "Permanently delete " + id + "? This deletes the workspace's "
      + "files and every session's history (the transcripts live inside "
      + "it), tears down its environment (the container and its volume, or "
      + "on Kubernetes the pod and its storage), and ends every open "
      + "session on it as workspace_lost; those sessions cannot be "
      + "resumed. If the workspace's provider is refused on this "
      + "deployment, only its row is dropped and its files are left on the "
      + "provider's disk.";
  } else if (nav === "collections") {
    message = "Permanently delete " + id + "? Every document in it, their "
      + "content and its search index are deleted with it, and this cannot "
      + "be undone. A new collection with the same id starts empty.";
  } else if (nav === "triggers") {
    message = "Permanently delete " + id + "? Its subscriptions are deleted "
      + "with it, so nothing that trigger was routing will run again, and a "
      + "session parked waiting on it will never be woken by it.";
  } else if (nav === "services") {
    // The public URL is built from the name, and the card shows the
    // generated id: the prompt gives the name the operator knows.
    var svc = row.name || id;
    // The resolver caches name -> service per process for a few seconds
    // (RESOLVE_TTL_SECONDS, primer/service/serve.py), so the URL can answer
    // briefly after the delete.
    message = "Permanently delete " + svc + "? Every version and its files "
      + "are deleted, and /svc/" + svc + "/ stops answering (a server "
      + "process may keep answering from its cache for a few seconds).";
  }
  if (nav === "agents" && NV_SETUP_AGENT_IDS.indexOf(row.id) !== -1) {
    message = id + " is a built-in agent that setup checks for. "
      + "Deleting it marks this install as not set up: admins are sent to "
      + "the setup checklist and everyone else waits on a setup screen "
      + "until the agent is back. A server restart, or Re-run seed on the "
      + "setup checklist, re-creates it with its default definition, not "
      + "your edits.";
  }
  return { title: "Delete " + (row.name || row.id), message: message };
}

// Confirm, then DELETE one card's entity. env carries the console's
// dialog, fetch, toast and list refetch so the whole flow runs outside
// React (tests/ui/test_platform_delete_confirm.py). Resolves true when
// the row was deleted, false when the prompt was declined or refused.
function NV_deleteRow(env, nav, row, path) {
  var c = NV_deleteConfirm(nav, row);
  return env.confirmDialog({
    title: c.title, message: c.message, danger: true,
  }).then(function (ok) {
    if (!ok) return false;
    return env.apiFetch("DELETE", path).then(function () {
      env.toast("Deleted " + (row.id || row.name));
      env.refetch();
      return true;
    }, function (e) {
      env.toast("Delete refused: " + window.primerApi.readRefusal(e).message,
        { kind: "error", requestId: e.requestId });
      return false;
    });
  });
}

// True exactly when an overlay that was open has just closed. A card opens its
// entity in an overlay over the grid and a write made there never reached the
// grid behind it (its list is polled every 15 s), so the page refetches on this
// transition. tests/ui/test_platform_overlay_close_refetch.py runs it.
function NV_overlayClosed(wasOpen, isOpen) {
  return !!wasOpen && !isOpen;
}

// A row created from a form the Platform page hosts: refresh the cards
// behind and open the new row's own detail overlay (where a toolset's source
// or a trigger's subscriptions are edited), which is where the legacy list
// sent the operator after a create. A row with no id has no detail to open.
// tests/ui/test_platform_create_opens_the_form.py runs this in MiniRacer.
function NV_createdRow(con, refetch, nav, row) {
  refetch();
  if (row && row.id) con.openOverlay(nav, null, row.id);
}

// What a list says when it has nothing to show. "Empty" and "a filter hides every row" are different: only the second is a matter of
// matching. `noun` is a page's [singular, plural]; `total` is how many rows exist before the filter.
function NV_emptyText(noun, query, total) {
  var n = NV_nounOf(noun);
  var q = String(query || "").trim();
  if (q && total > 0) return "No " + n[1] + ' match "' + q + '".';
  return "No " + n[1] + " yet.";
}

// "12 agents", "1 agent", or "3 of 12 agents" while a filter hides some.
function NV_countText(noun, shown, total) {
  var n = NV_nounOf(noun);
  var word = (total === 1 ? n[0] : n[1]);
  if (shown !== total) return shown + " of " + total + " " + word;
  return shown + " " + (shown === 1 ? n[0] : n[1]);
}

// A page that forgets its noun (or declares a malformed one) says "item" / "items": the two helpers above run on every
// render of every page and nothing catches a throw there, so a missing noun must not be able to blank the console.
function NV_nounOf(noun) {
  var ok = Array.isArray(noun) && typeof noun[0] === "string" && noun[0]
    && typeof noun[1] === "string" && noun[1];
  return ok ? noun : ["item", "items"];
}

// What the channel create host shows, from its fetch of the channel providers
// (the dialog needs the list). Data wins over everything: a background refetch
// or a failed refetch keeps the dialog it already has. No data and an error is
// an error; no data yet is loading; data with no provider is the dead end of
// ADM-07, which the host turns into an action instead of prose.
function NV_channelCreateState(res) {
  if (res && res.data) return (res.data.items || []).length ? "ready" : "none";
  if (res && res.error) return "error";
  return "loading";
}

// The way out of "New channel" with no channel provider: the Platform page's
// own Providers catalogue (it is mounted inline, not as an overlay, since the
// IA restructure), opened on the Channels type: view=platform:providers:channel.
function NV_addChannelProvider(con) {
  con.goView("platform", "providers", "channel");
}

// Provider types (seam I-1). Platform > Providers has a sub-entry per type in the sidebar, and the type is the third segment of the view
// (view=platform:providers:<type>, ui/foundation/shell-url.js). NV_platProviderTypes() is that grammar's own list, in the sidebar's order, and
// NV_PROVIDER_TYPE_LABELS names each one; a type the grammar adds without a label here is drawn under its key. Plain JS on purpose (see the
// note at NV_fact): tests/ui/test_platform_nav.py runs these.
var NV_PROVIDER_TYPE_LABELS = {
  llm: "LLM", embedding: "Embedding", ssp: "Semantic Search Storage",
  cross_encoder: "Cross Encoding", workspace: "Workspace",
  web_search: "Web Search", channel: "Channels",
  artifact_storage: "Artifact Store", tts: "TTS", stt: "ASR",
};

function NV_platProviderTypes() {
  var subs = window.SH_VIEW_SUBS && window.SH_VIEW_SUBS["platform:providers"];
  return Array.isArray(subs) ? subs.slice() : [];
}

// Custom pages (seam I-2). Two nav ids are not NV_PLAT_PAGES entries: their page is a component another file exports on window
// (WEB_SettingsPage, Platform revamp Plan 04; WT_TemplatesPage, Plan 08), read with no props because it reads the console itself.
// A custom nav exists only while its component is loaded: no row until then, and a link to it opens Providers (every type), so no placeholder
// page ever ships and a pasted link is never a blank page.
var NV_PLAT_CUSTOM_PAGES = { web: "WEB_SettingsPage", templates: "WT_TemplatesPage" };
var NV_PLAT_CUSTOM_LABELS = { web: "Web", templates: "Workspace templates" };

function NV_platHas(table, id) {
  return !!table && Object.prototype.hasOwnProperty.call(table, id);
}

// Is there a page behind this nav id right now? Providers always; a custom page when its component is a function on window; any other id when it
// has an NV_PLAT_PAGES entry (a retired id, such as "workspaces", has none).
function NV_platNavAvailable(id) {
  if (id === "providers") return true;
  if (NV_platHas(NV_PLAT_CUSTOM_PAGES, id)) {
    return typeof window[NV_PLAT_CUSTOM_PAGES[id]] === "function";
  }
  return NV_platHas(window.NV_PLAT_PAGES, id);
}

// The nav to show for a requested one: itself when it has a page, else Providers.
function NV_platResolveNav(nav) {
  return NV_platNavAvailable(nav) ? nav : "providers";
}

// A nav's name in the sidebar: "Providers", a custom page's label, or its entry's title (the sidebar and the phone's section list read the
// same title). An id with none is shown as itself rather than as nothing.
function NV_platNavLabel(id) {
  if (id === "providers") return "Providers";
  if (NV_platHas(NV_PLAT_CUSTOM_LABELS, id)) return NV_PLAT_CUSTOM_LABELS[id];
  var page = NV_platHas(window.NV_PLAT_PAGES, id) ? window.NV_PLAT_PAGES[id] : null;
  return page && page.title ? page.title : id;
}

// Which sidebar entry the open view marks as current: a nav id, or "providers:<type>" for an open provider type. The Providers row itself is
// current only on All (no type), so the row and the type entries never both claim the page. A nav with no page behind it is Providers.
function NV_platNavCurrent(view) {
  var nav = NV_platResolveNav((view && view.nav) || "providers");
  if (nav === "providers" && view && view.sub
      && NV_platProviderTypes().indexOf(view.sub) >= 0) {
    return "providers:" + view.sub;
  }
  return nav;
}

// What the catalogue's own navigation does on the Platform page. The catalogue reports a chip click (or a register that lands on a class) as a
// {kind: "provider-class", classKey} ref; a class that is a sidebar type moves the address to that type, unless the address is already on it (no
// history entry for a move that goes nowhere). Any other class (web_fetch and model_profile have their own pages) and every provider-instance ref
// stay in the catalogue's own state and do not touch the address. Answers whether it moved the address.
function NV_providersNavigate(con, ref) {
  if (!ref || ref.kind !== "provider-class") return false;
  if (NV_platProviderTypes().indexOf(ref.classKey) < 0) return false;
  var view = con.view;
  if (view && view.nav === "providers" && view.sub === ref.classKey) return false;
  con.goView("platform", "providers", ref.classKey);
  return true;
}

// Per-entity page config. list() returns a promise of {items}; card(row, con)
// maps a row to the ONE card view model of every Platform page (seam I-8):
//   { id, name, sub, badge?, icon?: { path, tone? }, status?: { label, tone:
//     "ok"|"warn"|"error"|"muted", onClick?, testid? }, facts }
// id is the record id (the scoped id for a tool); badge is a short neutral
// word ("built in", a backend kind); facts holds strings and [key, value]
// pairs, and the null NV_fact answers for a missing value, which every
// renderer drops; con is the console (NV_useConsole()), so a status with an
// onClick can open a dialog or an overlay, and is drawn as a button. open()
// addresses the shared overlays; create() either hosts the entity's form on
// this page (setModal) or opens a management overlay (tests/ui/
// test_platform_create_opens_the_form.py tabulates which); delPath() names
// the DELETE route (null = no delete from the card, e.g. providers where
// the catalog owns lifecycle). tests/ui/test_platform_card_view_model.py
// holds every entry to the card shape. An entry may also set emptyText and
// emptyAction: { label, run(con) } (seam I-9): a page with no rows says its
// own words and offers that way out instead of the generic sentence and its
// create button (tests/ui/test_platform_empty_states.py).
var NV_PLAT_PAGES = {
  profiles: {
    title: "LLM Profiles", noun: ["model profile", "model profiles"],
    // The one action is "+ Aggregate" (design 7.1). There is no single-profile
    // create: a single profile comes from the Register provider overlay's
    // import, so an empty page points there. "+ Aggregate" hosts the profile
    // overlay with no profile, which MP_ProfileModal draws as the aggregated
    // profile's create form (model-profiles.jsx).
    createLabel: "+ Aggregate",
    create: function (con, setModal) {
      setModal({ kind: "profile", row: null });
    },
    emptyText: "No LLM profiles yet. Register an LLM provider to import its models.",
    emptyAction: {
      label: "Open Providers › LLM",
      run: function (con) { con.goView("platform", "providers", "llm"); },
    },
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/model_profiles?limit=200", null, { signal: signal });
    },
    card: function (row, con) {
      return {
        id: row.id, name: row.id,
        sub: [row.provider_id, row.model_name || row.model]
          .filter(Boolean).join(" · "),
        facts: [
          NV_fact("context", row.context_length),
          NV_fact("reasoning",
            (row.config && row.config.reasoning) || row.reasoning),
        ],
      };
    },
    // The profile form is its OWN modal (MP_ProfileModal): opening the
    // Providers overlay for a profile action was a straight mis-route
    // (live finding 2026-08-26).
    open: function (con, row, setModal) {
      setModal({ kind: "profile", row: row });
    },
    delPath: function (row) { return "/model_profiles/" + encodeURIComponent(row.id); },
  },
  agents: {
    title: "Agents", noun: ["agent", "agents"], createLabel: "+ Agent",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/agents?limit=200", null, { signal: signal });
    },
    card: function (row, con) {
      var ident = NV_identity({ kind: "agent", agent_id: row.id });
      return {
        id: row.id, name: row.id, sub: row.description || "",
        icon: { path: NV_PLAT_ICONS.agents, tone: ident.color },
        facts: [
          NV_fact("profile", row.model && row.model.profile_id),
          NV_fact("tools", NV_countOf(row.tools)),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("agents", null, row.id); },
    // Section "new" tells the surface to open its create form
    // IMMEDIATELY - landing the operator on the list with a second
    // "new" button was a two-step detour (live finding 2026-08-26).
    create: function (con, setModal) { setModal({ kind: "agent" }); },
    delPath: function (row) { return "/agents/" + encodeURIComponent(row.id); },
  },
  graphs: {
    title: "Graphs", noun: ["graph", "graphs"], createLabel: "New graph",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/graphs?limit=200", null, { signal: signal });
    },
    card: function (row, con) {
      return {
        id: row.id, name: row.id, sub: row.description || "No description",
        icon: { path: NV_PLAT_ICONS.graphs, tone: NV_GRAPH_GLYPH.color },
        facts: [
          NV_fact("nodes", NV_countOf(row.nodes)),
          NV_fact("edges", NV_countOf(row.edges)),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("graphs", null, row.id); },
    create: function (con, setModal) { setModal({ kind: "graph" }); },
    delPath: function (row) { return "/graphs/" + encodeURIComponent(row.id); },
  },
  toolsets: {
    title: "Toolsets", noun: ["toolset", "toolsets"], createLabel: "New toolset",
    // GET /tools is the CATALOGUE: built-ins (system, workspaces, web,
    // ...) and registered rows alike. Listing only the DB table hid
    // every internal toolset from the cards (live finding 2026-08-26).
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/tools", null, { signal: signal });
    },
    card: function (row, con) {
      return {
        id: row.id, name: row.id, sub: row.tagline || row.description || "",
        badge: row.builtin ? "built in" : (row.kind || "registered"),
        // notes 3.1: red = unreachable, not amber (amber is in-progress).
        status: row.available === false
          ? { label: "unavailable", tone: "error" }
          : { label: "available", tone: "ok" },
        facts: [
          NV_fact("tools", NV_countOf(row.tools)),
          NV_fact("source", row.source || row.transport),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("toolsets", null, row.id); },
    // The page's own create dialog (toolsets.jsx), hosted here: the legacy
    // list overlay used to open first and the form only on a second press.
    create: function (con, setModal) { setModal({ kind: "toolset" }); },
    // A built-in ships with the platform; only registered rows delete.
    delPath: function (row) {
      return row.builtin ? null
        : "/toolsets/" + encodeURIComponent(row.id);
    },
  },
  tools: {
    title: "Tools", noun: ["tool", "tools"],
    // Flat catalogue of every tool on the install, searchable and
    // paged like any platform page. Approval policies point here.
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/tools/catalogue", null, { signal: signal });
    },
    card: function (row, con) {
      var scoped = String(row.id || "");
      var sep = scoped.indexOf("__");
      var toolset = sep > 0 ? scoped.slice(0, sep) : "";
      var bare = sep > 0 ? scoped.slice(sep + 2) : scoped;
      var desc = String(row.description || "").split("\n")[0];
      return {
        // the scoped id is the card's id: two toolsets may each have a "search"
        id: scoped, name: bare, sub: desc,
        badge: toolset || undefined,
        facts: [NV_fact("scoped id", scoped)],
      };
    },
    open: function (con, row) {
      var sep = String(row.id || "").indexOf("__");
      con.openOverlay("toolsets", null,
        sep > 0 ? String(row.id).slice(0, sep) : null);
    },
  },
  collections: {
    title: "Collections", noun: ["collection", "collections"], createLabel: "New collection",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/collections?limit=200", null, { signal: signal });
    },
    card: function (row, con) {
      return {
        id: row.id, name: row.id, sub: row.description || "",
        badge: row.system ? "system" : undefined,
        facts: [
          NV_fact("documents", NV_countOf(row.documents || row.doc_count)),
          NV_fact("search", row.search
            ? "enabled" : (row.search === null ? "disabled" : row.search)),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("collections", null, row.id); },
    create: function (con, setModal) { setModal({ kind: "collection" }); },
    delPath: function (row) {
      return row.system ? null
        : "/collections/" + encodeURIComponent(row.id);
    },
  },
  triggers: {
    title: "Triggers", noun: ["trigger", "triggers"], createLabel: "New trigger",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/triggers?limit=200", null, { signal: signal });
    },
    card: function (row, con) {
      return {
        id: row.id, name: row.id, sub: row.description || row.schedule || "",
        status: row.enabled === false
          ? { label: "disabled", tone: "muted" } : { label: "enabled", tone: "ok" },
        facts: [
          NV_fact("kind", row.kind),
          NV_fact("schedule", row.schedule),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("triggers", null, row.id); },
    create: function (con, setModal) { setModal({ kind: "trigger" }); },
    delPath: function (row) { return "/triggers/" + encodeURIComponent(row.id); },
  },
  channels: {
    title: "Channels", createLabel: "New channel",
    noun: ["channel", "channels"],
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/channels?limit=200", null, { signal: signal });
    },
    card: function (row, con) {
      return {
        id: row.id, name: row.id, sub: row.provider_id || row.kind || "",
        status: row.status ? {
          label: row.status, tone: row.status === "ok" ? "ok" : "warn",
        } : undefined,
        facts: [
          NV_fact("kind", row.kind),
          NV_fact("provider", row.provider_id),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("channels", null, row.id); },
    create: function (con, setModal) { setModal({ kind: "channel" }); },
    delPath: function (row) { return "/channels/" + encodeURIComponent(row.id); },
    extraNav: { label: "Rules", run: function (con) { con.openOverlay("channels", "rules", null); } },
  },
  harnesses: {
    title: "Harnesses", noun: ["harness", "harnesses"], createLabel: "New harness",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/harnesses?limit=200", null, { signal: signal });
    },
    card: function (row, con) {
      return {
        id: row.id, name: row.slug || row.id, sub: row.description || "",
        badge: row.kind || undefined,
        facts: [
          NV_fact("agent", row.agent_id),
          NV_fact("workspace", row.workspace_id),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("harnesses", null, row.id); },
    create: function (con, setModal) { setModal({ kind: "harness" }); },
    delPath: function (row) { return "/harnesses/" + encodeURIComponent(row.id); },
    // The legacy list offered a second create beside "Register from git".
    extraNav: {
      label: "Build outbound",
      run: function (con, setModal) { setModal({ kind: "harness-outbound" }); },
    },
  },
  services: {
    title: "Services", noun: ["service", "services"], createLabel: "New service",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/services?limit=200", null, { signal: signal });
    },
    card: function (row, con) {
      return {
        id: row.id, name: row.id, sub: row.description || "",
        status: row.state ? {
          label: row.state, tone: row.state === "running" ? "ok" : "muted",
        } : undefined,
        facts: [
          NV_fact("workspace", row.workspace_id),
          NV_fact("version", row.active_version || row.version),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("services", null, row.id); },
    create: function (con, setModal) { setModal({ kind: "service" }); },
    delPath: function (row) { return "/services/" + encodeURIComponent(row.id); },
  },
  approvals: {
    title: "Approval policies", noun: ["approval policy", "approval policies"], createLabel: "New policy",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/tool_approval_policies?limit=200", null,
        { signal: signal });
    },
    card: function (row, con) {
      return {
        // rev-2 synthesis Wave 3 item 48/49: title is the mono TOOL
        // PATTERN (toolset_id__tool_name), not the policy's own
        // entity id - the id still addresses Open/Delete, it just
        // isn't the thing an operator scans the grid for.
        id: row.id,
        name: NV_approvalToolPattern(row),
        sub: "strategy: " + NV_approvalStrategyLabel(row),
        // Whether the policy is in force is the card's status (seam I-8).
        status: row.enabled
          ? { label: "active", tone: "ok" } : { label: "disabled", tone: "muted" },
        facts: NV_approvalFacts(row),
      };
    },
    // uiv2 Wave 3 routing fix (item 0, approved): thread row.id through
    // like every sibling entity (services et al.) - the "approvals"
    // overlay now dispatches on the id slot itself (nv-overlays.jsx),
    // landing on AP_PolicyDetail's edit-mode AP_NewPolicyModal instead
    // of the generic records sheet. "new" mirrors AgentsPage's own
    // startCreate convention for an immediate create modal.
    open: function (con, row) { con.openOverlay("approvals", null, row.id); },
    create: function (con, setModal) { setModal({ kind: "policy" }); },
    delPath: function (row) {
      return "/tool_approval_policies/" + encodeURIComponent(row.id);
    },
  },
};

// Approval-policy card helpers (rev-2 synthesis Wave 3 item 49) - kept
// module-local since only the approvals card() above uses them. Real
// ToolApprovalPolicy fields (primer/model/tool_approval.py): toolset_id,
// tool_name, enabled, approval.{type,policy,provider_id,model},
// timeout_seconds, approvers.{kind,roles,users}.

function NV_approvalToolPattern(row) {
  return (row.toolset_id || "?") + "__" + (row.tool_name || "?");
}

function NV_approvalStrategyLabel(row) {
  var t = row.approval && row.approval.type;
  if (t === "policy") return "Rego";
  if (t === "llm") return "LLM judge";
  return "always require";
}

// "anyone" | "role: x, y" | "user: x, y" - admins are always admitted
// regardless (ApproverSpec.allows), so that's not spelled out here.
function NV_approvalWhoDecides(row) {
  var spec = row.approvers;
  if (!spec || spec.kind === "anyone") return "anyone";
  var names = (spec.kind === "roles" ? spec.roles : spec.users) || [];
  var noun = spec.kind === "roles" ? "role" : "user";
  return names.length ? noun + ": " + names.join(", ") : noun + ": (none set)";
}

// timeout_seconds is a float; render whole minutes when it divides
// evenly (the common case), falling back to seconds for odd values
// rather than showing a decimal.
function NV_approvalTimeoutLabel(row) {
  var secs = row.timeout_seconds;
  if (secs == null) return "60m (default) → reject";
  var mins = secs / 60;
  var text = Number.isInteger(mins) ? mins + "m" : Math.round(secs) + "s";
  return text + " → reject";
}

function NV_approvalFacts(row) {
  var t = row.approval && row.approval.type;
  if (t === "policy") {
    // No separate human-readable description field on
    // PolicyApprovalConfig (only the raw Rego source) - show its
    // first non-blank line rather than inventing summary prose.
    var firstLine = ((row.approval && row.approval.policy) || "")
      .split("\n").map(function (l) { return l.trim(); })
      .filter(function (l) { return l && l[0] !== "#"; })[0] || "(empty policy)";
    return [
      NV_fact("who decides", "returned per call"),
      NV_fact("policy", firstLine),
    ];
  }
  if (t === "llm") {
    var judge = row.approval
      ? (row.approval.provider_id || "?") + " · " + (row.approval.model || "?")
      : "?";
    return [
      NV_fact("judge", judge),
      NV_fact("who decides", NV_approvalWhoDecides(row) + " (judge may escalate)"),
    ];
  }
  return [
    NV_fact("who decides", NV_approvalWhoDecides(row)),
    NV_fact("timeout", NV_approvalTimeoutLabel(row)),
  ];
}

// Decisions-audit row helpers (Phase-2c: platform-approvals-staged).
// ToolApprovalRecord (primer/model/tool_approval.py) is the real
// shape - `decided_by` is null for SYNTHESIZED verdicts (timeout/
// cancel), and classify_approval_payload (primer/worker/yield_runtime.
// py) always collapses both to decision="rejected", distinguishing
// them only by the literal reason string "timed-out" (timeout) vs
// whatever the cancel payload gave, defaulting to "cancelled". The
// literal decision values "timeout"/"cancelled" are separately
// reachable via the chat-abandon flow and are honoured as-is.
function NV_approvalDerivedStatus(r) {
  if (r.decision === "timeout" || r.decision === "cancelled") return r.decision;
  if (r.decision === "rejected" && !r.decided_by) {
    if (r.reason === "timed-out") return "timeout";
    // HEURISTIC, not spec (approved as a judgment call, not derived
    // from any documented contract): classify_approval_payload's only
    // OTHER synthesized-reject path is YieldCancelled, whose reason is
    // always non-null (either the given cancel reason or the literal
    // fallback "cancelled") - so "no decided_by (synthesized) + a
    // reason that isn't the timeout marker" is, by elimination, the
    // cancel path. No mockup slot for this state exists (its 4 rows
    // are pending/approved/rejected/timeout only), so surfacing it as
    // its own "cancelled" token instead of silently folding back into
    // "rejected" is this file's own call - flag it in the PR body.
    if (r.reason) return "cancelled";
  }
  return r.decision || "";
}

function NV_approvalStatusColor(status) {
  if (status === "approved") return "var(--green)";
  if (status === "rejected" || status === "timeout" || status === "cancelled") return "var(--red)";
  return "var(--text-3)";
}

function NV_ellipsize(s, max) {
  if (!s || s.length <= max) return s || "";
  return s.slice(0, max) + "…";
}

// "{N}m elapsed → rejected" derives from the RECORD's own requested_at/
// decided_at span, not the policy's configured timeout_seconds - the
// staged fixture that surfaced this fired on the 60-minute GLOBAL yield
// cap (policy timeout_seconds was null), where reconstructing a
// hardcoded "30m" from the mockup's own example would have been wrong.
// The record always carries both timestamps, so this is exact
// regardless of which timeout (policy or global cap) actually fired.
function NV_approvalDecidedBy(r, status) {
  if (status === "timeout") {
    if (r.requested_at && r.decided_at) {
      const mins = Math.round((new Date(r.decided_at) - new Date(r.requested_at)) / 60000);
      return `${mins}m elapsed → rejected`;
    }
    return "elapsed → rejected";
  }
  if (status === "cancelled") {
    return r.reason && r.reason !== "cancelled" ? `cancelled — "${NV_ellipsize(r.reason, 40)}"` : "cancelled";
  }
  if (r.decided_by) {
    return r.reason ? `${r.decided_by} — "${NV_ellipsize(r.reason, 40)}"` : r.decided_by;
  }
  return "—";
}

function NV_approvalAt(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  if (sameDay) return `${hh}:${mm}`;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${months[d.getMonth()]} ${d.getDate()} · ${hh}:${mm}`;
}

// notes 3.1: "pager (range + prev/next appears past 6 entities)".
var NV_PLAT_PAGE_SIZE = 6;

function NV_PlatNav() {
  var con = NV_useConsole();
  var active = NV_platNavCurrent(con.view);
  var types = NV_platProviderTypes();
  return (
    <div className="nv-plat-nav" data-testid="nv-plat-nav">
      {NV_PLAT_GROUPS.map(function (group) {
        // A custom page whose component is not loaded has no row (seam I-2).
        var ids = group.ids.filter(NV_platNavAvailable);
        if (!ids.length) return null;
        return (
          <div key={group.label}>
            <div className="nv-plat-group-label">{group.label}</div>
            {ids.map(function (id) {
              var label = NV_platNavLabel(id);
              return (
                <React.Fragment key={id}>
                  <button type="button" className="nv-plat-row"
                    data-active={id === active ? "true" : "false"}
                    data-testid={"nv-plat-row:" + id}
                    aria-current={id === active ? "page" : undefined}
                    onClick={function () { con.goView("platform", id); }}>
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none"
                      stroke="currentColor" strokeWidth="1.2"
                      strokeLinejoin="round" className="nv-plat-row-icon">
                      <path d={NV_PLAT_ICONS[id]} />
                    </svg>
                    <span>{label}</span>
                  </button>
                  {id === "providers" && types.length ? (
                    <div className="nv-plat-subs" role="group"
                      aria-label="Provider types" data-testid="nv-plat-subs">
                      {types.map(function (t) {
                        return (
                          <button type="button" key={t} className="nv-plat-sub"
                            data-active={"providers:" + t === active ? "true" : "false"}
                            data-testid={"nv-plat-sub:" + t}
                            aria-current={"providers:" + t === active ? "page" : undefined}
                            onClick={function () { con.goView("platform", "providers", t); }}>
                            {NV_PROVIDER_TYPE_LABELS[t] || t}
                          </button>
                        );
                      })}
                    </div>
                  ) : null}
                </React.Fragment>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

// One card of the page, drawn from the card view model (seam I-8): the
// type icon in its tone, the name, the badge and the status; a status
// with an onClick is a button above the stretched Open (a click on it
// runs the action and never opens the card); at most three facts, each a
// string (one line) or a [key, value] pair.
function NV_PlatCard(props) {
  var c = props.card;
  var facts = (c.facts || []).filter(Boolean).slice(0, 3);
  var icon = c.icon || null;
  var status = c.status || null;
  return (
    <div className="nv-pcard" data-testid={"nv-pcard:" + c.name}>
      <div className="nv-pcard-head">
        {icon && icon.path ? (
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none"
            stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round"
            aria-hidden="true"
            style={{ flexShrink: 0, color: icon.tone || "var(--text-4)" }}>
            <path d={icon.path} />
          </svg>
        ) : null}
        <span className="nv-pcard-name">{c.name}</span>
        <span style={{ flex: 1 }} />
        {c.badge ? <span className="nv-pcard-badge">{c.badge}</span> : null}
        {status && typeof status.onClick === "function" ? (
          <button type="button" className="nv-pcard-status nv-pcard-status-btn"
            data-tone={status.tone} data-testid={status.testid}
            onClick={function (ev) {
              if (ev && ev.stopPropagation) ev.stopPropagation();
              status.onClick();
            }}>{status.label}</button>
        ) : status ? (
          <span className="nv-pcard-status" data-tone={status.tone}>
            {status.label}
          </span>
        ) : null}
      </div>
      {c.sub ? <div className="nv-pcard-sub">{c.sub}</div> : null}
      {facts.length ? (
        <div className="nv-pcard-facts">
          {facts.map(function (f, i) {
            if (!Array.isArray(f)) {
              return (
                <div key={"s" + i} className="nv-fact-row">
                  <span className="nv-fact-v">{f}</span>
                </div>
              );
            }
            return (
              <div key={f[0]} className="nv-fact-row">
                <span className="nv-fact-k">{f[0]}</span>
                <span className="nv-fact-v">{f[1]}</span>
              </div>
            );
          })}
        </div>
      ) : null}
      <div className="nv-pcard-foot">
        <button type="button" className="nv-pcard-open"
          data-testid={"nv-pcard-open:" + c.name}
          aria-label={"Open " + c.name}
          onClick={props.onOpen}>Open</button>
        <span style={{ flex: 1 }} />
        {props.onDelete ? (
          <button type="button" className="nv-pcard-del"
            data-testid={"nv-pcard-del:" + c.name}
            onClick={props.onDelete}>
            Delete
          </button>
        ) : null}
      </div>
    </div>
  );
}

// Hosts of the create dialogs the entity pages already own (toolsets.jsx,
// triggers.jsx, services.jsx): the dialog is that page's form, unchanged; the Platform page
// only decides what happens when it closes or creates a row (NV_createdRow).
function NV_ToolsetCreateHost(props) {
  var Dialog = window.TS_NewToolsetModal;
  return (
    <Dialog
      pushToast={window.primerApi.toastPush}
      onClose={props.onClose}
      onCreate={function (row) {
        window.primerApi.toastPush({
          kind: "success", title: "Toolset created", detail: row.id,
        });
        props.onCreated(row);
      }} />
  );
}

function NV_TriggerCreateHost(props) {
  var Dialog = window.TR_CreateTriggerDialog;
  return <Dialog onClose={props.onClose} onCreated={props.onCreated} />;
}

function NV_ServiceCreateHost(props) {
  var Dialog = window.SV_ServiceModal;
  return <Dialog onClose={props.onClose} onSaved={props.onCreated} />;
}

// The collection dialog confirms itself ("Collection created") only through
// the pushToast it is given. The just-created cache is primed BEFORE the row is
// handed on: the detail overlay can open on a list that does not have it yet
// (knowledge.jsx, KN_rememberJustCreated).
function NV_CollectionCreateHost(props) {
  var Dialog = window.KN_NewCollectionModal;
  return (
    <Dialog
      pushToast={window.primerApi.toastPush}
      onClose={props.onClose}
      onCreate={function (row) {
        window.KN_rememberJustCreated(row);
        props.onCreated(row);
      }} />
  );
}

// The channel dialog needs the channel provider list, so the host fetches it.
// With no provider the dialog would be a form with an empty select (the legacy
// list disabled its button and said so in prose, ADM-07): the host explains
// and offers the way out. NewChannelModal calls onCreated() with no row and the
// channels overlay has no per-channel detail, so the host says "Channel
// created" itself and the page just refreshes the cards.
function NV_ChannelCreateHost(props) {
  var providers = window.primerApi.useResource(
    "nv-plat:channel-providers",
    function (signal) {
      return window.primerApi.apiFetch(
        "GET", "/channel_providers?limit=200", null, { signal: signal });
    },
    { pollMs: 0 }
  );
  var state = NV_channelCreateState(providers);
  if (state === "loading") return null;
  if (state === "ready") {
    var Dialog = window.NewChannelModal;
    return (
      <Dialog
        providers={providers.data.items}
        pushToast={window.primerApi.toastPush}
        onClose={props.onClose}
        onCreated={function () {
          window.primerApi.toastPush({ kind: "success", title: "Channel created" });
          props.onCreated();
        }} />
    );
  }
  var failed = state === "error";
  return (
    <Modal title="New channel" onClose={props.onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={props.onClose}>Cancel</Btn>
          {failed ? null : (
            <Btn kind="primary" data-testid="nv-plat-add-channel-provider"
              onClick={function () { props.onClose(); NV_addChannelProvider(props.con); }}>
              Add a channel provider
            </Btn>
          )}
        </>
      }>
      <div className="nv-modal-form" data-testid="nv-plat-channel-no-provider">
        {failed ? (
          <div className="nv-form-error">
            {providers.error.detail || providers.error.message}
          </div>
        ) : (
          <p>
            A channel binds one Slack, Telegram or Discord conversation to a
            channel provider, and there is no channel provider yet. Add one,
            then come back to create the channel.
          </p>
        )}
      </div>
    </Modal>
  );
}

// Register-from-git or the outbound builder; both pass the created row to
// onCreated once the harness is installed. Closing either one refetches the
// cards (the page's mounts): the register wizard creates its DRAFT row at
// step 1, so a cancelled wizard leaves one that would stay invisible until a
// reload.
function NV_HarnessCreateHost(props) {
  var Dialog = props.outbound
    ? window.HarnessOutboundBuilder : window.HarnessRegisterDialog;
  return <Dialog onClose={props.onClose} onCreated={props.onCreated} />;
}

// The agent, graph and approval-policy dialogs used to be opened ON TOP of the
// legacy list (the page opened that overlay with section "new" and the list's
// startCreate effect opened the dialog), so the full table sat behind the
// form. The agent dialog does not confirm itself (the legacy list toasted
// "Agent created" in its own onCreate), so its host does. The policy dialog
// has no onCreate: a created policy closes it, so the page refetches on close.
function NV_AgentCreateHost(props) {
  var Dialog = window.AG_NewAgentModal;
  return (
    <Dialog
      pushToast={window.primerApi.toastPush}
      onClose={props.onClose}
      onCreate={function (row) {
        window.primerApi.toastPush({
          kind: "success", title: "Agent created", detail: row.id,
        });
        props.onCreated(row);
      }} />
  );
}

function NV_GraphCreateHost(props) {
  var Dialog = window.GR_NewGraphModal;
  return (
    <Dialog
      pushToast={window.primerApi.toastPush}
      onClose={props.onClose}
      onCreate={props.onCreated} />
  );
}

function NV_PolicyCreateHost(props) {
  var Dialog = window.AP_NewPolicyModal;
  return (
    <Dialog
      pushToast={window.primerApi.toastPush}
      onClose={props.onClose} />
  );
}

// Model-profile create/edit, inline on the platform page. The form is
// MP_ProfileModal (model-profiles.jsx); this host only feeds it the
// provider list it needs.
function NV_ProfileModalHost(props) {
  var providers = window.primerApi.useResource(
    "nv-plat:llm-providers",
    function (signal) {
      return window.primerApi.apiFetch(
        "GET", "/llm_providers?limit=200", null, { signal: signal });
    },
    { pollMs: 0 }
  );
  if (typeof window.MP_ProfileModal !== "function") return null;
  return (
    <window.MP_ProfileModal open
      existing={props.row || null}
      providers={(providers.data && providers.data.items) || []}
      onClose={props.onClose}
      onSaved={props.onSaved} />
  );
}

// uiv2 Wave 3 (a-14 fold): headless per-session pending fetcher, same
// N+1 pattern approvals.jsx's old AP_RecordFetcher used (GET /yields/
// pending is the cheaper cross-workspace call but never carries tool_
// name/arguments/gate_reason - it only reads them internally to
// classify "approval" vs "ask" vs "parked" - so it can't populate the
// TOOL column). 404 means this parked session isn't gated on
// tool_approval (ask_user/sleep/watch_files etc.) - reports null so
// the parent drops it.
function NV_AuditPendingFetcher({ sid, onRecord }) {
  var pending = window.primerApi.useResource(
    "nv-plat:audit-pending:" + sid,
    function (signal) {
      return window.primerApi.apiFetch(
        "GET", "/sessions/" + encodeURIComponent(sid) + "/tool_approval/pending",
        null, { signal: signal });
    },
    { pollMs: 5000, deps: [sid] }
  );
  React.useEffect(function () {
    if (pending.error && pending.error.status === 404) { onRecord(sid, null); return; }
    if (!pending.data) return;
    onRecord(sid, pending.data);
  }, [pending.data, pending.error && pending.error.status, sid]);
  return null;
}

var NV_AUDIT_STATUS_RANK = { pending: 0, approved: 1, rejected: 2, timeout: 3, cancelled: 4 };

// The decisions audit under the approvals cards: who decided what,
// when - uiv2 Wave 3 (a-14): folded in the records-sheet's sort
// controls and live pending merge, then the sheet itself retired.
// READ-ONLY by design: the mockup's own pending row ("workspace__
// write_file · pending · awaiting an admin · 09:41") carries no
// action buttons - this is a passive record, not a second actionable
// surface. Approve/Reject stays exactly where it already lives: the
// Inbox rail and session-detail's NV_DecisionCard/ApprovalBanner.
function NV_ApprovalsAudit() {
  var con = NV_useConsole();
  var sortState = React.useState("time");
  var sortBy = sortState[0], setSortBy = sortState[1];
  var dirState = React.useState("desc");
  var sortDir = dirState[0], setSortDir = dirState[1];
  var pendingState = React.useState({});
  var pendingBySession = pendingState[0], setPendingBySession = pendingState[1];

  var onPendingRecord = React.useCallback(function (sid, record) {
    setPendingBySession(function (prev) {
      if (record == null) {
        if (!(sid in prev)) return prev;
        var next = Object.assign({}, prev);
        delete next[sid];
        return next;
      }
      return Object.assign({}, prev, { [sid]: record });
    });
  }, []);

  var parkedSessions = window.primerApi.useResource(
    "nv-plat:audit-parked-sessions",
    function (signal) {
      return window.primerApi.apiFetch("POST", "/sessions/find", {
        predicate: {
          kind: "predicate",
          left: { kind: "field", name: "parked_status" },
          op: "=",
          right: { kind: "value", value: "parked" },
        },
        page: { kind: "offset", offset: 0, length: 100 },
      }, { signal: signal });
    },
    { pollMs: 5000 }
  );
  var records = window.primerApi.useResource(
    "nv-plat:approval-records",
    function (signal) { return SH_api.approvalRecords(signal); },
    { pollMs: 5000 }
  );

  var sessionIds = ((parkedSessions.data && parkedSessions.data.items) || [])
    .map(function (s) { return s.id; });

  var pendingRows = sessionIds
    .map(function (sid) { return { sid: sid, rec: pendingBySession[sid] }; })
    .filter(function (x) { return x.rec; })
    .map(function (x) {
      var rec = x.rec;
      return {
        key: SH_pendingId(x.sid, rec.gate_id, rec.tool_call_id),
        sessionId: x.sid,
        // A pending record's tool_name already IS the full scoped id
        // (original_call.name, as issued) - unlike a resolved record's
        // separate toolset_id/tool_name pair, there is no join needed.
        toolPattern: rec.tool_name || "",
        status: "pending",
        decidedByText: SH_routingLine(
          { approvers: rec.approvers },
          { username: con.username, role: con.role },
        ),
        at: rec.parked_at,
      };
    });

  var resolvedRows = ((records.data && records.data.items) || []).map(function (r) {
    // Phase-2c (platform-approvals-staged): TOOL was reading
    // r.tool_id/r.tool, neither of which exist on ToolApprovalRecord
    // (real fields: toolset_id/tool_name) - confirmed live bug, not a
    // data gap. Reuses the same mono tool-pattern helper the policy
    // cards use.
    var status = NV_approvalDerivedStatus(r);
    return {
      key: r.id,
      sessionId: r.session_id || "",
      toolPattern: NV_approvalToolPattern(r),
      status: status,
      decidedByText: NV_approvalDecidedBy(r, status),
      at: r.decided_at,
    };
  });

  var allRows = pendingRows.concat(resolvedRows).sort(function (a, b) {
    var cmp;
    if (sortBy === "status") {
      cmp = (NV_AUDIT_STATUS_RANK[a.status] ?? 99) - (NV_AUDIT_STATUS_RANK[b.status] ?? 99);
      if (cmp === 0) cmp = new Date(b.at || 0) - new Date(a.at || 0);
    } else {
      cmp = new Date(b.at || 0) - new Date(a.at || 0);
    }
    return sortDir === "asc" ? -cmp : cmp;
  });
  var rows = allRows.slice(0, 30);

  return (
    <div className="nv-audit" data-testid="nv-plat-audit">
      {sessionIds.map(function (sid) {
        return <NV_AuditPendingFetcher key={sid} sid={sid} onRecord={onPendingRecord} />;
      })}
      <div className="nv-audit-title-row">
        <div className="nv-audit-title">Decisions — audit</div>
        <span style={{ flex: 1 }} />
        <span className="muted text-sm">sort</span>
        <select
          className="select"
          aria-label="Sort decisions"
          value={sortBy}
          onChange={function (e) { setSortBy(e.target.value); }}
          style={{ fontSize: 12 }}
          data-testid="nv-audit-sort-by"
        >
          <option value="time">by time</option>
          <option value="status">by status</option>
        </select>
        <Btn
          size="sm"
          kind="ghost"
          icon={sortDir === "desc" ? "chevron-down" : "chevron-up"}
          onClick={function () { setSortDir(function (d) { return d === "desc" ? "asc" : "desc"; }); }}
          title={sortDir === "desc" ? "descending" : "ascending"}
          data-testid="nv-audit-sort-dir"
        >
          {sortDir === "desc" ? "newest" : "oldest"}
        </Btn>
      </div>
      <div className="nv-audit-table">
        <div className="nv-audit-head">
          <span>Session</span><span>Tool</span><span>Status</span>
          <span>Decided by</span><span>At</span>
        </div>
        {rows.map(function (row) {
          var color = row.status === "pending" ? "var(--amber)" : NV_approvalStatusColor(row.status);
          return (
            <div key={row.key} className="nv-audit-row">
              <span>{row.sessionId}</span>
              <span className="nv-audit-mono">{row.toolPattern}</span>
              <span className="nv-audit-mono" style={{ color: color }}>
                {row.status}
              </span>
              <span>{row.decidedByText}</span>
              <span className="nv-audit-mono">{NV_approvalAt(row.at)}</span>
            </div>
          );
        })}
        {!rows.length ? (
          <div className="nv-audit-row nv-audit-empty">
            <span>No decisions recorded yet.</span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

// IA restructure 01a04d6a (user directive supersedes the mockup where
// they conflict): the platform's "Providers" nav entry renders the
// unified catalog INLINE - killing the three-layer stack (platform page
// -> catalog overlay -> form overlay) this used to be. See provider-
// catalog.jsx's own header comment for the full architecture (All
// default view, type filter, Register-by-type, edit-in-place).
// initialClass is the view's provider type, "all" or a type key (seam I-1:
// view=platform:providers:<type>). The catalog owns its class and its open
// instance in state of its own and only SEEDS them from its props, so a move
// it did not make itself (a click in the sidebar, Back, a pasted link) starts
// it over: a new key remounts it at the type the address names, with no
// instance open, instead of leaving the previous type's instance selected
// under the new type (a vector store's detail page asked for an LLM provider's
// id). A move it made itself is reported through onNavigate
// (NV_providersNavigate answers whether it moved the address) and must keep it
// mounted: a card click on All selects its class AND opens the edit form in the
// same call. The catalog's own type chips stay until Plan 02 (the Register
// overlay) replaces its body; the sidebar's sub-entries are the navigation the
// user asked for.
function NV_ProvidersPlatPage(props) {
  var con = NV_useConsole();
  var track = React.useRef({ view: con.view, epoch: 0, own: false });
  if (track.current.view !== con.view) {
    if (!track.current.own) track.current.epoch += 1;
    track.current.own = false;
    track.current.view = con.view;
  }
  return (
    <div className="nv-plat-main">
      <div className="nv-plat-wrap" data-testid="nv-plat-page:providers">
        <window.ProviderCatalog key={track.current.epoch}
          initialClass={props.type}
          initialInstanceId={null}
          onNavigate={function (ref) {
            if (NV_providersNavigate(con, ref)) track.current.own = true;
          }}
        />
      </div>
    </div>
  );
}

// A custom page (seam I-2): the component another file exports on window, in the same frame the other pages have. It is given no props (it reads the
// console itself) and is only mounted while it is loaded, which NV_platResolveNav has already checked.
function NV_CustomPlatPage(props) {
  var Page = window[NV_PLAT_CUSTOM_PAGES[props.nav]];
  return (
    <div className="nv-plat-main">
      <div className="nv-plat-wrap" data-testid={"nv-plat-page:" + props.nav}>
        <Page />
      </div>
    </div>
  );
}

// The Platform page for the open nav. This function only chooses: the page that has hooks is NV_PlatListPage, keyed on the nav so each nav is
// its own instance that mounts and unmounts whole. Returning Providers from the SAME instance that otherwise calls a dozen hooks (the early
// return used to sit before them) left the page just left with hooks that never ran again, so their cleanups never ran and its list went on
// polling every 15 s while Providers stayed open (admin review 2026-10-08; tests/ui/test_platform_page_hooks.py).
function NV_PlatPage() {
  var con = NV_useConsole();
  var nav = NV_platResolveNav(con.view.nav || "providers");
  if (nav === "providers") return <NV_ProvidersPlatPage type={con.view.sub || "all"} />;
  if (NV_platHas(NV_PLAT_CUSTOM_PAGES, nav)) return <NV_CustomPlatPage key={nav} nav={nav} />;
  return <NV_PlatListPage key={nav} nav={nav} />;
}

function NV_PlatListPage(props) {
  var con = NV_useConsole();
  var nav = props.nav;
  var apiFetch = window.primerApi.apiFetch;
  var qState = React.useState("");
  var q = qState[0];
  var setQ = qState[1];
  var pageState = React.useState(0);
  var pageNo = pageState[0];
  var setPageNo = pageState[1];
  // Inline create/edit modal for entities whose form is a component of
  // its own (model profiles, the create dialogs hosted below): {kind, row}.
  var modalState = React.useState(null);
  var modal = modalState[0];
  var setModal = modalState[1];
  // No reset-on-nav effect: NV_PlatPage keys this component on the nav, so a section switch is a fresh instance and the filter, the page number and
  // the open form start empty by construction (and so does the scroll position). tests/ui_e2e/test_platform_nav_switch_journey.py holds that by behaviour.
  // R4 review finding 3: typing a search query narrowed the result set
  // but never reset pageNo, so `p = Math.min(pageNo, pages - 1)` below
  // clamped toward the END of the shrunk range rather than the start -
  // a search while on a later page could land on the LAST page of
  // matches instead of the first. Mirrors toolsets.jsx:1259's
  // equivalent reset-on-filter-change effect.
  React.useEffect(function () { setPageNo(0); }, [q]);

  var page = NV_PLAT_PAGES[nav] || null;
  var listKey = "nv-plat:" + nav;
  var res = window.primerApi.useResource(
    listKey,
    function (signal) {
      return page.list(apiFetch, signal);
    },
    { pollMs: 15000, deps: [nav] }
  );
  // ADM-16: closing an overlay refreshes the list behind it (see NV_overlayClosed).
  var overlayOpen = !!con.overlay;
  var overlayWasOpen = React.useRef(overlayOpen);
  React.useEffect(function () {
    if (NV_overlayClosed(overlayWasOpen.current, overlayOpen)) res.refetch();
    overlayWasOpen.current = overlayOpen;
  }, [overlayOpen]);
  var items = (res.data && res.data.items) || [];

  var cards = items.map(function (row) {
    return page.card(row, con);
  }).map(function (c, i) { c._row = items[i]; return c; });
  var total = cards.length;
  var ql = q.trim().toLowerCase();
  if (ql) {
    cards = cards.filter(function (c) {
      return (c.name + " " + (c.sub || "")).toLowerCase().indexOf(ql) >= 0;
    });
  }
  var pages = Math.max(1, Math.ceil(cards.length / NV_PLAT_PAGE_SIZE));
  var p = Math.min(pageNo, pages - 1);
  var visible = cards.slice(p * NV_PLAT_PAGE_SIZE, (p + 1) * NV_PLAT_PAGE_SIZE);

  function openRow(row) {
    page.open(con, row, setModal);
  }
  function runCreate() {
    if (page.create) page.create(con, setModal);
  }
  function del(row) {
    var path = page.delPath ? page.delPath(row) : null;
    if (!path) return;
    NV_deleteRow({
      confirmDialog: confirmDialog,
      apiFetch: apiFetch,
      toast: function (msg, extra) { con.toast(msg, extra); },
      refetch: function () { res.refetch(); },
    }, nav, row, path);
  }

  var title = page.title;
  var createLabel = page.createLabel;
  return (
    <div className="nv-plat-main">
      <div className="nv-plat-wrap" data-testid={"nv-plat-page:" + nav}>
        <div className="nv-plat-head">
          <div className="nv-plat-title">{title}</div>
          <span className="nv-plat-count" data-testid="nv-plat-count">
            {NV_countText(page.noun, cards.length, total)}
          </span>
          <span style={{ flex: 1 }} />
          {page && page.extraNav ? (
            <button type="button" className="nv-btn-secondary"
              data-testid="nv-plat-extra"
              onClick={function () { page.extraNav.run(con, setModal); }}>
              {page.extraNav.label}
            </button>
          ) : null}
          <div className="nv-plat-filter">
            <svg width="11" height="11" viewBox="0 0 12 12" fill="none"
              stroke="currentColor" strokeWidth="1.4">
              <circle cx="5.2" cy="5.2" r="3.6" />
              <path d="M8 8 11 11" />
            </svg>
            <input value={q} placeholder="Filter…"
              aria-label={"Filter " + (page.title || "list").toLowerCase()}
              data-testid="nv-plat-filter"
              onChange={function (ev) { setQ(ev.target.value); setPageNo(0); }} />
          </div>
          {createLabel ? (
            <button type="button" className="nv-btn-primary"
              data-testid="nv-plat-create"
              onClick={runCreate}>{createLabel}</button>
          ) : null}
        </div>
        {res.error ? (
          <div className="nv-form-error">
            {res.error.detail || res.error.message}
          </div>
        ) : null}
        {!visible.length && !res.loading && !res.error ? (
          <div className="nv-plat-empty" data-testid="nv-plat-empty">
            <div>
              {total === 0 && page.emptyText
                ? page.emptyText : NV_emptyText(page.noun, q, total)}
            </div>
            {ql && total > 0 ? (
              <button type="button" className="nv-btn-secondary"
                data-testid="nv-plat-clear-filter"
                onClick={function () { setQ(""); setPageNo(0); }}>
                Clear filter
              </button>
            ) : page.emptyAction ? (
              <button type="button" className="nv-btn-primary"
                data-testid="nv-plat-empty-action"
                onClick={function () { page.emptyAction.run(con); }}>
                {page.emptyAction.label}
              </button>
            ) : createLabel ? (
              <button type="button" className="nv-btn-primary"
                onClick={runCreate}>{createLabel}</button>
            ) : null}
          </div>
        ) : null}
        <div className="nv-pcard-grid">
          {visible.map(function (c) {
            var deletable = page.delPath && page.delPath(c._row);
            return (
              <NV_PlatCard key={c.id} card={c}
                onOpen={function () { openRow(c._row); }}
                onDelete={deletable
                  ? function () { del(c._row); } : null} />
            );
          })}
        </div>
        {modal && modal.kind === "profile" ? (
          <NV_ProfileModalHost row={modal.row}
            onClose={function () { setModal(null); }}
            onSaved={function () { setModal(null); res.refetch(); }} />
        ) : null}
        {modal && modal.kind === "toolset" ? (
          <NV_ToolsetCreateHost
            onClose={function () { setModal(null); }}
            onCreated={function (row) {
              setModal(null);
              NV_createdRow(con, function () { res.refetch(); }, "toolsets", row);
            }} />
        ) : null}
        {modal && modal.kind === "trigger" ? (
          <NV_TriggerCreateHost
            onClose={function () { setModal(null); }}
            onCreated={function (row) {
              setModal(null);
              NV_createdRow(con, function () { res.refetch(); }, "triggers", row);
            }} />
        ) : null}
        {modal && modal.kind === "service" ? (
          <NV_ServiceCreateHost
            onClose={function () { setModal(null); }}
            onCreated={function (row) {
              setModal(null);
              NV_createdRow(con, function () { res.refetch(); }, "services", row);
            }} />
        ) : null}
        {modal && modal.kind === "collection" ? (
          <NV_CollectionCreateHost
            onClose={function () { setModal(null); }}
            onCreated={function (row) {
              setModal(null);
              NV_createdRow(con, function () { res.refetch(); }, "collections", row);
            }} />
        ) : null}
        {modal && modal.kind === "channel" ? (
          <NV_ChannelCreateHost con={con}
            onClose={function () { setModal(null); }}
            onCreated={function () { setModal(null); res.refetch(); }} />
        ) : null}
        {modal && modal.kind === "harness" ? (
          <NV_HarnessCreateHost
            onClose={function () { setModal(null); res.refetch(); }}
            onCreated={function (row) {
              setModal(null);
              NV_createdRow(con, function () { res.refetch(); }, "harnesses", row);
            }} />
        ) : null}
        {modal && modal.kind === "harness-outbound" ? (
          <NV_HarnessCreateHost outbound
            onClose={function () { setModal(null); res.refetch(); }}
            onCreated={function (row) {
              setModal(null);
              NV_createdRow(con, function () { res.refetch(); }, "harnesses", row);
            }} />
        ) : null}
        {modal && modal.kind === "agent" ? (
          <NV_AgentCreateHost
            onClose={function () { setModal(null); }}
            onCreated={function (row) {
              setModal(null);
              NV_createdRow(con, function () { res.refetch(); }, "agents", row);
            }} />
        ) : null}
        {modal && modal.kind === "graph" ? (
          <NV_GraphCreateHost
            onClose={function () { setModal(null); }}
            onCreated={function (row) {
              setModal(null);
              NV_createdRow(con, function () { res.refetch(); }, "graphs", row);
            }} />
        ) : null}
        {modal && modal.kind === "policy" ? (
          <NV_PolicyCreateHost
            onClose={function () { setModal(null); res.refetch(); }} />
        ) : null}
        {cards.length > NV_PLAT_PAGE_SIZE ? (
          <div className="nv-pager">
            <span className="nv-pager-range">
              {p * NV_PLAT_PAGE_SIZE + 1}–
              {Math.min((p + 1) * NV_PLAT_PAGE_SIZE, cards.length)} of {cards.length}
            </span>
            <span style={{ flex: 1 }} />
            <button type="button" className="nv-pager-btn" disabled={p === 0}
              onClick={function () { setPageNo(Math.max(0, p - 1)); }}>‹</button>
            <button type="button" className="nv-pager-btn"
              disabled={p >= pages - 1}
              onClick={function () { setPageNo(p + 1); }}>›</button>
          </div>
        ) : null}
        {nav === "approvals" ? <NV_ApprovalsAudit /> : null}
      </div>
    </div>
  );
}

function NV_Platform() {
  return (
    <div className="nv-plat" data-testid="nv-platform">
      <NV_PlatNav />
      <NV_PlatPage />
    </div>
  );
}

window.NV_PLAT_PAGES = NV_PLAT_PAGES;
window.NV_PROVIDER_TYPE_LABELS = NV_PROVIDER_TYPE_LABELS;
window.NV_platProviderTypes = NV_platProviderTypes;
window.NV_platNavCurrent = NV_platNavCurrent;
window.NV_providersNavigate = NV_providersNavigate;
window.NV_PLAT_CUSTOM_PAGES = NV_PLAT_CUSTOM_PAGES;
window.NV_PLAT_CUSTOM_LABELS = NV_PLAT_CUSTOM_LABELS;
window.NV_platNavAvailable = NV_platNavAvailable;
window.NV_platResolveNav = NV_platResolveNav;
window.NV_platNavLabel = NV_platNavLabel;
window.NV_emptyText = NV_emptyText;
window.NV_countText = NV_countText;
window.NV_Platform = NV_Platform;
