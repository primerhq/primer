/* global React, SH_api, NV_useConsole, NV_identity, NV_GRAPH_GLYPH */
// The Platform view (wiring plan P4 T10): grouped left nav + one
// config-driven card page per entity, ported from the prototype's
// PLATFORM region. Cards open the SHARED overlays (the same component
// whether reached from a card, the palette, or a pasted link), create
// buttons open the same overlays' create flows, delete is inline with
// confirm - a referenced entity's 409/422 surfaces as the refusal toast.
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
  workspaces: "M2 4.5 7 2l5 2.5v5L7 12 2 9.5Z M7 7v5 M2 4.5 7 7l5-2.5",
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
  approvals: "M7 1.5 12 3v4c0 3-2.2 5-5 6-2.8-1-5-3-5-6V3Z "
    + "M4.8 7l1.6 1.6L9.6 5.4",
};

var NV_PLAT_GROUPS = [
  { label: "Intelligence", ids: ["providers", "profiles", "agents", "graphs"] },
  // "templates" retired as a peer nav id (uiv2 US-011a): it folded into
  // Workspaces (WorkspacesPage's own "Manage templates" link opens the
  // workspaces:templates overlay section instead). The grammar
  // (shell-url.js SH_VIEWS.platform) and this file's own per-entity
  // config still admit view=platform:templates so an old deep link
  // keeps working - there is just no row for it here anymore.
  { label: "Workbench",
    ids: ["workspaces", "toolsets", "tools", "collections"] },
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

// Per-entity page config. list() returns a promise of {items}; card()
// maps a row to the prototype's card VM; open()/create() address the
// shared overlays; delPath() names the DELETE route (null = no delete
// from the card, e.g. providers where the catalog owns lifecycle).
var NV_PLAT_PAGES = {
  profiles: {
    title: "Model profiles", createLabel: "New profile",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/model_profiles?limit=200", null, { signal: signal });
    },
    card: function (row) {
      return {
        name: row.id,
        sub: [row.provider_id, row.model_name || row.model]
          .filter(Boolean).join(" · "),
        chip: null,
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
    create: function (con, setModal) {
      setModal({ kind: "profile", row: null });
    },
    delPath: function (row) { return "/model_profiles/" + encodeURIComponent(row.id); },
  },
  agents: {
    title: "Agents", createLabel: "New agent",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/agents?limit=200", null, { signal: signal });
    },
    card: function (row) {
      var ident = NV_identity({ kind: "agent", agent_id: row.id });
      return {
        name: row.id, sub: row.description || "",
        glyph: ident.d, color: ident.color,
        chip: row.status ? {
          label: row.status,
          color: row.status === "ok" ? "var(--green)" : "var(--attention)",
        } : null,
        facts: [
          NV_fact("profile", row.model_profile_id),
          NV_fact("tools", NV_countOf(row.tools)),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("agents", null, row.id); },
    // Section "new" tells the surface to open its create form
    // IMMEDIATELY - landing the operator on the list with a second
    // "new" button was a two-step detour (live finding 2026-08-26).
    create: function (con) { con.openOverlay("agents", "new", null); },
    delPath: function (row) { return "/agents/" + encodeURIComponent(row.id); },
  },
  graphs: {
    title: "Graphs", createLabel: "New graph",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/graphs?limit=200", null, { signal: signal });
    },
    card: function (row) {
      return {
        name: row.id, sub: row.description || "",
        glyph: NV_GRAPH_GLYPH.d, color: NV_GRAPH_GLYPH.color,
        chip: null,
        facts: [
          NV_fact("nodes", NV_countOf(row.nodes)),
          NV_fact("edges", NV_countOf(row.edges)),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("graphs", null, row.id); },
    create: function (con) { con.openOverlay("graphs", "new", null); },
    delPath: function (row) { return "/graphs/" + encodeURIComponent(row.id); },
  },
  workspaces: {
    title: "Workspaces", createLabel: "New workspace",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/workspaces?limit=200", null, { signal: signal });
    },
    card: function (row) {
      return {
        name: row.name || row.id,
        sub: row.name ? row.id : (row.template_id || ""),
        chip: row.status ? { label: row.status, color: "var(--text-3)" } : null,
        facts: [
          NV_fact("template", row.template_id),
          NV_fact("provider", row.provider_id),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("workspaces", null, row.id); },
    create: function (con) { con.openOverlay("new-workspace", null, null); },
    delPath: function (row) { return "/workspaces/" + encodeURIComponent(row.id); },
  },
  toolsets: {
    title: "Toolsets", createLabel: "New toolset",
    // GET /tools is the CATALOGUE: built-ins (system, workspaces, web,
    // ...) and registered rows alike. Listing only the DB table hid
    // every internal toolset from the cards (live finding 2026-08-26).
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/tools", null, { signal: signal });
    },
    card: function (row) {
      return {
        name: row.id, sub: row.tagline || row.description || "",
        chip: row.builtin
          ? { label: "built-in", color: "var(--blue)" }
          : { label: row.kind || "registered", color: "var(--text-3)" },
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
    create: function (con) { con.openOverlay("toolsets", null, null); },
    // A built-in ships with the platform; only registered rows delete.
    delPath: function (row) {
      return row.builtin ? null
        : "/toolsets/" + encodeURIComponent(row.id);
    },
  },
  tools: {
    title: "Tools",
    // Flat catalogue of every tool on the install, searchable and
    // paged like any platform page. Approval policies point here.
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/tools/catalogue", null, { signal: signal });
    },
    card: function (row) {
      var scoped = String(row.id || "");
      var sep = scoped.indexOf("__");
      var toolset = sep > 0 ? scoped.slice(0, sep) : "";
      var bare = sep > 0 ? scoped.slice(sep + 2) : scoped;
      var desc = String(row.description || "").split("\n")[0];
      return {
        name: bare, sub: desc, _key: scoped,
        chip: toolset ? { label: toolset, color: "var(--violet)" } : null,
        facts: [NV_fact("scoped id", scoped)],
      };
    },
    open: function (con, row) {
      var sep = String(row.id || "").indexOf("__");
      con.openOverlay("toolsets", null,
        sep > 0 ? String(row.id).slice(0, sep) : null);
    },
  },
  templates: {
    title: "Workspace templates", createLabel: "New template",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/workspace_templates?limit=200", null,
        { signal: signal });
    },
    card: function (row) {
      var backend = (row.backend && row.backend.kind) || "local";
      return {
        name: row.id, sub: row.description || "",
        chip: { label: backend, color: "var(--teal)" },
        facts: [
          NV_fact("provider", row.provider_id),
          NV_fact("image", row.backend && row.backend.image),
        ],
      };
    },
    open: function (con, row, setModal) {
      setModal({ kind: "template", row: row });
    },
    create: function (con, setModal) {
      setModal({ kind: "template", row: null });
    },
    delPath: function (row) {
      return "/workspace_templates/" + encodeURIComponent(row.id);
    },
  },
  collections: {
    title: "Collections", createLabel: "New collection",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/collections?limit=200", null, { signal: signal });
    },
    card: function (row) {
      return {
        name: row.id, sub: row.description || "",
        chip: row.system
          ? { label: "system", color: "var(--blue)" } : null,
        facts: [
          NV_fact("documents", NV_countOf(row.documents || row.doc_count)),
          NV_fact("search", row.search
            ? "enabled" : (row.search === null ? "disabled" : row.search)),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("collections", null, row.id); },
    create: function (con) { con.openOverlay("collections", null, null); },
    delPath: function (row) {
      return row.system ? null
        : "/collections/" + encodeURIComponent(row.id);
    },
  },
  triggers: {
    title: "Triggers", createLabel: "New trigger",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/triggers?limit=200", null, { signal: signal });
    },
    card: function (row) {
      return {
        name: row.id, sub: row.description || row.schedule || "",
        chip: {
          label: row.enabled === false ? "disabled" : "enabled",
          color: row.enabled === false ? "var(--text-4)" : "var(--green)",
        },
        facts: [
          NV_fact("kind", row.kind),
          NV_fact("schedule", row.schedule),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("triggers", null, row.id); },
    create: function (con) { con.openOverlay("triggers", null, null); },
    delPath: function (row) { return "/triggers/" + encodeURIComponent(row.id); },
  },
  channels: {
    title: "Channels — rooms & rules", createLabel: "New channel",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/channels?limit=200", null, { signal: signal });
    },
    card: function (row) {
      return {
        name: row.id, sub: row.provider_id || row.kind || "",
        chip: row.status ? {
          label: row.status,
          color: row.status === "ok" ? "var(--green)" : "var(--attention)",
        } : null,
        facts: [
          NV_fact("kind", row.kind),
          NV_fact("provider", row.provider_id),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("channels", null, row.id); },
    create: function (con) { con.openOverlay("channels", null, null); },
    delPath: function (row) { return "/channels/" + encodeURIComponent(row.id); },
    extraNav: { label: "Rules", run: function (con) { con.openOverlay("channels", "rules", null); } },
  },
  harnesses: {
    title: "Harnesses", createLabel: "New harness",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/harnesses?limit=200", null, { signal: signal });
    },
    card: function (row) {
      return {
        name: row.slug || row.id, sub: row.description || "",
        chip: row.kind ? { label: row.kind, color: "var(--text-3)" } : null,
        facts: [
          NV_fact("agent", row.agent_id),
          NV_fact("workspace", row.workspace_id),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("harnesses", null, row.id); },
    create: function (con) { con.openOverlay("harnesses", null, null); },
    delPath: function (row) { return "/harnesses/" + encodeURIComponent(row.id); },
  },
  services: {
    title: "Services", createLabel: "New service",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/services?limit=200", null, { signal: signal });
    },
    card: function (row) {
      return {
        name: row.id, sub: row.description || "",
        chip: row.state ? {
          label: row.state,
          color: row.state === "running" ? "var(--green)" : "var(--text-3)",
        } : null,
        facts: [
          NV_fact("workspace", row.workspace_id),
          NV_fact("version", row.active_version || row.version),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("services", null, row.id); },
    create: function (con) { con.openOverlay("services", null, null); },
    delPath: function (row) { return "/services/" + encodeURIComponent(row.id); },
  },
  approvals: {
    title: "Approval policies", createLabel: "New policy",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/tool_approval_policies?limit=200", null,
        { signal: signal });
    },
    card: function (row) {
      return {
        // rev-2 synthesis Wave 3 item 48/49: title is the mono TOOL
        // PATTERN (toolset_id__tool_name), not the policy's own
        // entity id - the id still addresses Open/Delete, it just
        // isn't the thing an operator scans the grid for.
        name: NV_approvalToolPattern(row),
        sub: "strategy: " + NV_approvalStrategyLabel(row),
        // .nv-pcard-chip (bare mono, no pill border) is the mockup's
        // "active" treatment - .nv-pcard-status (bordered pill) is a
        // DIFFERENT, unrelated affordance used by other entities.
        chip: {
          label: row.enabled ? "active" : "disabled",
          color: row.enabled ? "var(--green)" : "var(--text-3)",
        },
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
    create: function (con) { con.openOverlay("approvals", "new", null); },
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
    // A synthesized reject with SOME reason but no timeout marker is
    // the cancel path. No mockup slot for this state exists (its 4
    // rows are pending/approved/rejected/timeout only) - rendering it
    // as its own "cancelled" token rather than silently folding back
    // into "rejected" is a judgment call, flagged for the PR body.
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
  var active = con.view.nav || "providers";
  return (
    <div className="nv-plat-nav" data-testid="nv-plat-nav">
      {NV_PLAT_GROUPS.map(function (group) {
        return (
          <div key={group.label}>
            <div className="nv-plat-group-label">{group.label}</div>
            {group.ids.map(function (id) {
              var page = NV_PLAT_PAGES[id];
              var label = id === "providers" ? "Providers"
                : (page ? page.title : id);
              return (
                <div key={id} className="nv-plat-row"
                  data-active={id === active ? "true" : "false"}
                  data-testid={"nv-plat-row:" + id}
                  onClick={function () { con.goView("platform", id); }}>
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none"
                    stroke="currentColor" strokeWidth="1.2"
                    strokeLinejoin="round" className="nv-plat-row-icon">
                    <path d={NV_PLAT_ICONS[id]} />
                  </svg>
                  <span>{label}</span>
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

function NV_PlatCard(props) {
  var c = props.card;
  var facts = (c.facts || []).filter(Boolean);
  return (
    <div className="nv-pcard" data-testid={"nv-pcard:" + c.name}
      onClick={props.onOpen}>
      <div className="nv-pcard-head">
        {c.glyph ? (
          <svg width="12" height="12" viewBox="0 0 12 12"
            style={{ flexShrink: 0, color: c.color }}>
            <path d={c.glyph} fill="currentColor" />
          </svg>
        ) : null}
        <span className="nv-pcard-name">{c.name}</span>
        <span style={{ flex: 1 }} />
        {c.status ? (
          <span className="nv-pcard-status" data-tone={c.status.tone}>
            {c.status.label}
          </span>
        ) : null}
        {c.chip ? (
          <span className="nv-pcard-chip" style={{ color: c.chip.color }}>
            {c.chip.label}
          </span>
        ) : null}
      </div>
      {c.sub ? <div className="nv-pcard-sub">{c.sub}</div> : null}
      {facts.length ? (
        <div className="nv-pcard-facts">
          {facts.map(function (f) {
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
        <span className="nv-pcard-open">Open</span>
        <span style={{ flex: 1 }} />
        {props.onDelete ? (
          <button type="button" className="nv-pcard-del"
            data-testid={"nv-pcard-del:" + c.name}
            onClick={function (ev) { ev.stopPropagation(); props.onDelete(); }}>
            Delete
          </button>
        ) : null}
      </div>
    </div>
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

// Workspace-template create/edit. Until now templates had NO page at
// all (live finding 2026-08-26): id + description + provider are
// first-class fields, the backend spec stays JSON for everything the
// schema can express.
function NV_TemplateModal(props) {
  var apiFetch = window.primerApi.apiFetch;
  var row = props.row || null;
  var isEdit = !!row;
  var idState = React.useState(row ? row.id : "");
  var descState = React.useState((row && row.description) || "");
  var provState = React.useState((row && row.provider_id) || "");
  var backendState = React.useState(JSON.stringify(
    (row && row.backend) || { kind: "local" }, null, 2));
  var errState = React.useState(null);
  var busyState = React.useState(false);
  var providers = window.primerApi.useResource(
    "nv-plat:ws-providers",
    function (signal) {
      return apiFetch("GET", "/workspace_providers?limit=200", null,
        { signal: signal });
    },
    { pollMs: 0 }
  );
  var provItems = (providers.data && providers.data.items) || [];
  React.useEffect(function () {
    if (!provState[0] && provItems.length) provState[1](provItems[0].id);
  }, [provItems.length]);

  function save() {
    var backend;
    try {
      backend = JSON.parse(backendState[0] || "{}");
    } catch (e) {
      errState[1]("Backend spec is not valid JSON: " + e.message);
      return;
    }
    var body = {
      id: idState[0], description: descState[0],
      provider_id: provState[0], backend: backend,
    };
    busyState[1](true);
    errState[1](null);
    var call = isEdit
      ? apiFetch("PUT", "/workspace_templates/"
        + encodeURIComponent(row.id), body)
      : apiFetch("POST", "/workspace_templates", body);
    call.then(props.onSaved, function (e) {
      busyState[1](false);
      errState[1](e.detail || e.message);
    });
  }

  return (
    <Modal
      title={isEdit ? "Edit template · " + row.id : "New workspace template"}
      onClose={props.onClose}
      footer={
        <React.Fragment>
          <Btn kind="ghost" onClick={props.onClose}>Cancel</Btn>
          <Btn kind="primary" onClick={save} disabled={busyState[0]}
            data-testid="template-save">
            {busyState[0] ? "Saving…" : isEdit ? "Save template" : "Create template"}
          </Btn>
        </React.Fragment>
      }>
      <div className="nv-modal-form" data-testid="template-form">
        <label className="nv-mf-field">
          <span className="nv-mf-label">id</span>
          <input value={idState[0]} disabled={isEdit}
            data-testid="template-id"
            placeholder="my-template"
            onChange={function (ev) { idState[1](ev.target.value); }} />
        </label>
        <label className="nv-mf-field">
          <span className="nv-mf-label">description</span>
          <input value={descState[0]}
            placeholder="What workspaces from this template are for"
            onChange={function (ev) { descState[1](ev.target.value); }} />
        </label>
        <label className="nv-mf-field">
          <span className="nv-mf-label">workspace provider</span>
          <select value={provState[0]}
            onChange={function (ev) { provState[1](ev.target.value); }}>
            {provItems.map(function (p) {
              return <option key={p.id} value={p.id}>{p.id}</option>;
            })}
          </select>
        </label>
        <label className="nv-mf-field">
          <span className="nv-mf-label">backend spec (JSON)</span>
          <textarea rows={8} value={backendState[0]} spellCheck={false}
            className="nv-mf-mono"
            onChange={function (ev) { backendState[1](ev.target.value); }} />
          <span className="nv-mf-help">
            {"{\"kind\": \"local\"} runs on the server's disk; a "
              + "kubernetes backend names image, resources and pvc."}
          </span>
        </label>
        {errState[0] ? (
          <div className="nv-form-error">{errState[0]}</div>
        ) : null}
      </div>
    </Modal>
  );
}

// The decisions audit under the approvals cards: who decided what, when.
function NV_ApprovalsAudit() {
  var records = window.primerApi.useResource(
    "nv-plat:approval-records",
    function (signal) { return SH_api.approvalRecords(signal); },
    { pollMs: 15000 }
  );
  var rows = ((records.data && records.data.items) || []).slice(0, 30);
  return (
    <div className="nv-audit" data-testid="nv-plat-audit">
      <div className="nv-audit-title">Decisions — audit</div>
      <div className="nv-audit-table">
        <div className="nv-audit-head">
          <span>Session</span><span>Tool</span><span>Status</span>
          <span>Decided by</span><span>At</span>
        </div>
        {rows.map(function (r, i) {
          // Phase-2c (platform-approvals-staged): TOOL was reading
          // r.tool_id/r.tool, neither of which exist on
          // ToolApprovalRecord (real fields: toolset_id/tool_name) -
          // confirmed live bug, not a data gap. Reuses the same mono
          // tool-pattern helper the policy cards use.
          var status = NV_approvalDerivedStatus(r);
          return (
            <div key={r.id || i} className="nv-audit-row">
              <span>{r.session_id || ""}</span>
              <span className="nv-audit-mono">{NV_approvalToolPattern(r)}</span>
              <span className="nv-audit-mono" style={{ color: NV_approvalStatusColor(status) }}>
                {status}
              </span>
              <span>{NV_approvalDecidedBy(r, status)}</span>
              <span className="nv-audit-mono">{NV_approvalAt(r.decided_at)}</span>
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
// initialClass/initialInstanceId are only ever the SEED: ProviderCatalog
// owns classKey/instanceId itself once mounted, so a static "all" here
// is enough. onNavigate is a no-op - the overlay's URL-synced deep-link
// granularity (a specific class/instance in the address bar) does not
// carry over to an inline page; a scope reduction that comes with
// killing the overlay, not a bug.
function NV_ProvidersPlatPage() {
  return (
    <div className="nv-plat-main">
      <div className="nv-plat-wrap" data-testid="nv-plat-page:providers">
        <window.ProviderCatalog
          initialClass="all"
          initialInstanceId={null}
          onNavigate={function () {}}
        />
      </div>
    </div>
  );
}

function NV_PlatPage() {
  var con = NV_useConsole();
  var nav = con.view.nav || "providers";
  if (nav === "providers") return <NV_ProvidersPlatPage />;
  var apiFetch = window.primerApi.apiFetch;
  var qState = React.useState("");
  var q = qState[0];
  var setQ = qState[1];
  var pageState = React.useState(0);
  var pageNo = pageState[0];
  var setPageNo = pageState[1];
  // Inline create/edit modal for entities whose form is a component of
  // its own (model profiles, workspace templates): {kind, row}.
  var modalState = React.useState(null);
  var modal = modalState[0];
  var setModal = modalState[1];
  React.useEffect(function () {
    setQ(""); setPageNo(0); setModal(null);
  }, [nav]);
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
  var items = (res.data && res.data.items) || [];

  var cards = items.map(function (row) {
    return page.card(row);
  }).map(function (c, i) { c._row = items[i]; return c; });
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
    confirmDialog({
      title: "Delete " + (row.name || row.id),
      message: "Permanently delete " + (row.id || row.name)
        + "? Referenced entities refuse deletion.",
      danger: true,
    }).then(function (ok) {
      if (!ok) return;
      apiFetch("DELETE", path).then(function () {
        con.toast("Deleted " + (row.id || row.name));
        res.refetch();
      }, function (e) {
        con.toast("Delete refused: " + (e.detail || e.message),
          { kind: "error", requestId: e.requestId });
      });
    });
  }

  var title = page.title;
  var createLabel = page.createLabel;
  return (
    <div className="nv-plat-main">
      <div className="nv-plat-wrap" data-testid={"nv-plat-page:" + nav}>
        <div className="nv-plat-head">
          <div className="nv-plat-title">{title}</div>
          <span className="nv-plat-count">
            {cards.length}{cards.length === 1 ? " entity" : " entities"}
          </span>
          <span style={{ flex: 1 }} />
          {page && page.extraNav ? (
            <button type="button" className="nv-btn-secondary"
              data-testid="nv-plat-extra"
              onClick={function () { page.extraNav.run(con); }}>
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
            <div>Nothing here yet.</div>
            {createLabel ? (
              <button type="button" className="nv-btn-primary"
                onClick={runCreate}>{createLabel}</button>
            ) : null}
          </div>
        ) : null}
        <div className="nv-pcard-grid">
          {visible.map(function (c) {
            var deletable = page.delPath && page.delPath(c._row);
            return (
              <NV_PlatCard key={c._key || c.name} card={c}
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
        {modal && modal.kind === "template" ? (
          <NV_TemplateModal row={modal.row}
            onClose={function () { setModal(null); }}
            onSaved={function () { setModal(null); res.refetch(); }} />
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
window.NV_Platform = NV_Platform;
