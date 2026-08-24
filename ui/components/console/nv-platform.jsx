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
  { label: "Workbench", ids: ["workspaces", "toolsets", "collections"] },
  { label: "Automation", ids: ["triggers", "channels", "harnesses", "services"] },
  { label: "Governance", ids: ["approvals"] },
];

// The S4 catalog's class registry (provider-catalog.jsx keeps its copy
// module-local, so the pills carry their own): key -> list plural.
var NV_PROV_CLASSES = [
  { key: "llm", label: "LLM", plural: "llm_providers" },
  { key: "embedding", label: "Embedding", plural: "embedding_providers" },
  { key: "cross_encoder", label: "Cross-Encoder", plural: "cross_encoder_providers" },
  { key: "ssp", label: "Vector Stores", plural: "ssp" },
  { key: "stt", label: "Speech-to-Text", plural: "stt_providers" },
  { key: "tts", label: "Text-to-Speech", plural: "tts_providers" },
  { key: "web_search", label: "Web Search", plural: "web_search_providers" },
  { key: "web_fetch", label: "Web Fetch", plural: "web_fetch_providers" },
  { key: "artifact_storage", label: "Artifact Storage", plural: "artifact_storage_providers" },
  { key: "workspace", label: "Workspaces", plural: "workspace_providers" },
  { key: "channel", label: "Channels", plural: "channel_providers" },
];

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
        sub: [row.provider_id, row.model].filter(Boolean).join(" · "),
        chip: null,
        facts: [
          NV_fact("reasoning", row.reasoning_effort || row.reasoning),
          NV_fact("max tokens", row.max_tokens),
        ],
      };
    },
    open: function (con) { con.openOverlay("providers", "llm", null); },
    create: function (con) { con.openOverlay("providers", "llm", null); },
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
    create: function (con) { con.openOverlay("agents", null, null); },
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
    create: function (con) { con.openOverlay("graphs", null, null); },
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
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/toolsets?limit=200", null, { signal: signal });
    },
    card: function (row) {
      return {
        name: row.id, sub: row.description || "",
        chip: row.kind ? { label: row.kind, color: "var(--text-3)" } : null,
        facts: [
          NV_fact("tools", NV_countOf(row.tools)),
          NV_fact("source", row.source || row.transport),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("toolsets", null, row.id); },
    create: function (con) { con.openOverlay("toolsets", null, null); },
    delPath: function (row) { return "/toolsets/" + encodeURIComponent(row.id); },
    // The flat tool catalog is its own overlay; this is its affordance.
    extraNav: { label: "Tool catalog", run: function (con) { con.openOverlay("tools", null, null); } },
  },
  collections: {
    title: "Collections", createLabel: "New collection",
    list: function (apiFetch, signal) {
      return apiFetch("GET", "/collections?limit=200", null, { signal: signal });
    },
    card: function (row) {
      return {
        name: row.id, sub: row.description || "",
        chip: null,
        facts: [
          NV_fact("documents", NV_countOf(row.documents || row.doc_count)),
          NV_fact("embedding", row.embedding_provider_id),
        ],
      };
    },
    open: function (con, row) { con.openOverlay("collections", null, row.id); },
    create: function (con) { con.openOverlay("collections", null, null); },
    delPath: function (row) { return "/collections/" + encodeURIComponent(row.id); },
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
        name: row.id, sub: row.description || row.tool_pattern || "",
        chip: row.mode ? { label: row.mode, color: "var(--text-3)" } : null,
        facts: [
          NV_fact("tool", row.tool_pattern || row.tool_id),
          NV_fact("action", row.action || row.decision),
        ],
      };
    },
    open: function (con) { con.openOverlay("approvals", null, null); },
    create: function (con) { con.openOverlay("approvals", null, null); },
    delPath: function (row) {
      return "/tool_approval_policies/" + encodeURIComponent(row.id);
    },
  },
};

var NV_PLAT_PAGE_SIZE = 12;

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
          var status = r.status || r.decision || "";
          return (
            <div key={r.id || i} className="nv-audit-row">
              <span>{r.session_id || ""}</span>
              <span className="nv-audit-mono">{r.tool_id || r.tool || ""}</span>
              <span className="nv-audit-mono" style={{
                color: status === "approved" ? "var(--green)"
                  : status === "rejected" ? "var(--red)" : "var(--text-3)",
              }}>{status}</span>
              <span>{r.decided_by || r.responded_by || "—"}</span>
              <span className="nv-audit-mono">
                {(r.decided_at || r.updated_at || "").slice(0, 16)}
              </span>
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

function NV_PlatPage() {
  var con = NV_useConsole();
  var apiFetch = window.primerApi.apiFetch;
  var nav = con.view.nav || "providers";
  var isProviders = nav === "providers";
  var famState = React.useState("llm");
  var fam = famState[0];
  var setFam = famState[1];
  var qState = React.useState("");
  var q = qState[0];
  var setQ = qState[1];
  var pageState = React.useState(0);
  var pageNo = pageState[0];
  var setPageNo = pageState[1];
  React.useEffect(function () { setQ(""); setPageNo(0); }, [nav]);

  var page = NV_PLAT_PAGES[nav] || null;
  var provClass = NV_PROV_CLASSES.find(function (c) { return c.key === fam; });
  var listKey = isProviders
    ? "nv-plat:providers:" + fam
    : "nv-plat:" + nav;
  var res = window.primerApi.useResource(
    listKey,
    function (signal) {
      if (isProviders) {
        return apiFetch("GET", "/" + provClass.plural + "?limit=200", null,
          { signal: signal });
      }
      return page.list(apiFetch, signal);
    },
    { pollMs: 15000, deps: [nav, fam] }
  );
  var items = (res.data && res.data.items) || [];

  function provCard(row) {
    return {
      name: row.name || row.id,
      sub: [row.kind, row.model].filter(Boolean).join(" · "),
      chip: row.active ? { label: "active config", color: "var(--green)" } : null,
      facts: [
        NV_fact("kind", row.kind),
        NV_fact("base url", row.base_url),
      ],
      _row: row,
    };
  }

  var cards = items.map(function (row) {
    return isProviders ? provCard(row) : page.card(row);
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
    if (isProviders) con.openOverlay("providers", fam, row.id || null);
    else page.open(con, row);
  }
  function del(row) {
    var path = isProviders
      ? "/" + provClass.plural + "/" + encodeURIComponent(row.id)
      : (page.delPath ? page.delPath(row) : null);
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

  var title = isProviders ? "Providers" : page.title;
  var createLabel = isProviders ? "New provider" : page.createLabel;
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
          <button type="button" className="nv-btn-primary"
            data-testid="nv-plat-create"
            onClick={function () {
              if (isProviders) con.openOverlay("providers", fam, null);
              else page.create(con);
            }}>{createLabel}</button>
        </div>
        {isProviders ? (
          <div className="nv-fam-pills" data-testid="nv-plat-fams">
            {NV_PROV_CLASSES.map(function (cls) {
              return (
                <button type="button" key={cls.key} className="nv-fam-pill"
                  data-active={cls.key === fam ? "true" : "false"}
                  data-testid={"nv-fam:" + cls.key}
                  onClick={function () { setFam(cls.key); setPageNo(0); }}>
                  {cls.label}
                </button>
              );
            })}
          </div>
        ) : null}
        {res.error ? (
          <div className="nv-form-error">
            {res.error.detail || res.error.message}
          </div>
        ) : null}
        {!visible.length && !res.loading && !res.error ? (
          <div className="nv-plat-empty" data-testid="nv-plat-empty">
            <div>Nothing here yet.</div>
            <button type="button" className="nv-btn-primary"
              onClick={function () {
                if (isProviders) con.openOverlay("providers", fam, null);
                else page.create(con);
              }}>{createLabel}</button>
          </div>
        ) : null}
        <div className="nv-pcard-grid">
          {visible.map(function (c) {
            return (
              <NV_PlatCard key={c.name} card={c}
                onOpen={function () { openRow(c._row); }}
                onDelete={function () { del(c._row); }} />
            );
          })}
        </div>
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
