/* global React, SH_api, NV_useConsole, NV_identity */
// The System view (wiring plan P5 T11): SYSNAV rail + one body per nav.
// The dashboard is fresh (health cards off /health, the worker fleet
// with drain/purge, and the cross-workspace "needs a human" panel via
// client fan-out over pending yields); the admin surfaces re-host the
// existing pages (users, API tokens, SSO, MCP, internal collections,
// activity, setup) - they keep their handlers, lose their chrome; the
// profile page is the operator's own: password + linked accounts.

var NV_SYS_ICONS = {
  dashboard: "M2 2h4.5v4.5H2Z M7.5 2H12v4.5H7.5Z M2 7.5h4.5V12H2Z "
    + "M7.5 7.5H12V12H7.5Z",
  users: "M5 6.5a2.2 2.2 0 1 0 0-.01 M1.5 12c0-2 1.6-3.2 3.5-3.2S8.5 10 "
    + "8.5 12 M9.5 6a1.8 1.8 0 1 0 0-.01 M9.8 8.8c1.6.2 2.7 1.3 2.7 3.2",
  apikeys: "M8.5 5.5a3 3 0 1 0-2.9 3.8L7 7.9h1.5V6.4L10 5 8.5 5.5Z "
    + "M6 8.5 2 12.5 M3.5 11l1 1",
  sso: "M7 1.5 12 3v4c0 3-2.2 5-5 6-2.8-1-5-3-5-6V3Z M5 7h4 M7 5v4",
  mcp: "M2 4h10v6H2Z M4 10v2h6v-2 M5 6.5h4",
  internal: "M3.5 1.5h7v11h-5.5a1.5 1.5 0 0 0-1.5 1.5Z M3.5 1.5V14 M6 5h3",
  activity: "M1.5 7h2.5l1.5-4 2.5 8L9.5 7H12",
  setup: "M7 4.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5Z M7 1v2 M7 11v2 "
    + "M1 7h2 M11 7h2 M2.8 2.8l1.4 1.4 M9.8 9.8l1.4 1.4 M11.2 2.8 9.8 "
    + "4.2 M4.2 9.8l-1.4 1.4",
  profile: "M7 6.5a2.4 2.4 0 1 0 0-.01 M2.5 12.5c0-2.4 2-3.8 4.5-3.8s4.5 "
    + "1.4 4.5 3.8",
};

var NV_SYS_LABELS = {
  dashboard: "Dashboard",
  users: "Users",
  apikeys: "API keys",
  sso: "SSO",
  mcp: "MCP server",
  internal: "Internal collections",
  activity: "Activity",
  setup: "Setup",
  profile: "Profile",
};

// Which navs a role sees. Everything else is the admin's.
var NV_SYS_USER_NAVS = ["apikeys", "profile"];

function NV_sysNavsFor(role) {
  var all = ["dashboard", "users", "apikeys", "sso", "mcp", "internal",
    "activity", "setup", "profile"];
  if (role === "admin") return all;
  return all.filter(function (id) {
    return NV_SYS_USER_NAVS.indexOf(id) >= 0;
  });
}

// --- Dashboard bits --------------------------------------------------------

function NV_HealthCards() {
  var health = window.primerApi.useResource(
    "nv-sys:health",
    function (signal) {
      return window.primerApi.apiFetch("GET", "/health", null,
        { signal: signal });
    },
    { pollMs: 5000 }
  );
  var data = health.data || {};
  var wp = data.worker_pool || {};
  var sched = data.scheduler || {};
  var ok = data.status === "ok" && sched.alive === true;
  var metrics = sched.metrics || {};
  var cards = [
    {
      k: "platform", v: data.status || "…",
      sub: ok ? "scheduler alive" : "scheduler " + String(sched.alive),
      tone: ok ? "var(--green)" : "var(--red)",
    },
    {
      k: "in flight", v: String(wp.in_flight == null ? "…" : wp.in_flight),
      sub: "of " + (wp.capacity == null ? "?" : wp.capacity) + " capacity",
      tone: "var(--blue)",
    },
    {
      k: "claims", v: String(
        metrics["primer_scheduler_claims_total"] == null
          ? "—" : metrics["primer_scheduler_claims_total"]),
      sub: "p99 " + (metrics["primer_scheduler_claim_latency_p99_ms"] == null
        ? "—" : metrics["primer_scheduler_claim_latency_p99_ms"] + "ms"),
      tone: "var(--violet)",
    },
    {
      k: "missed heartbeats", v: String(
        metrics["primer_scheduler_missed_heartbeats_total"] == null
          ? "—" : metrics["primer_scheduler_missed_heartbeats_total"]),
      sub: "since boot",
      tone: "var(--amber)",
    },
  ];
  return (
    <div className="nv-health-grid" data-testid="nv-sys-health">
      {cards.map(function (c) {
        return (
          <div key={c.k} className="nv-health-card">
            <div className="nv-health-k">
              <span className="nv-health-dot" style={{ background: c.tone }} />
              {c.k}
            </div>
            <div className="nv-health-v">{c.v}</div>
            <div className="nv-health-sub">{c.sub}</div>
          </div>
        );
      })}
    </div>
  );
}

function NV_WorkerFleet() {
  var con = NV_useConsole();
  var apiFetch = window.primerApi.apiFetch;
  var workers = window.primerApi.useResource(
    "nv-sys:workers",
    function (signal) { return apiFetch("GET", "/workers", null, { signal: signal }); },
    { pollMs: 8000 }
  );
  var rows = (workers.data && workers.data.items) || [];
  function drain(id) {
    apiFetch("POST", "/workers/" + encodeURIComponent(id) + "/drain").then(
      function () { con.toast("Draining " + id); workers.refetch(); },
      function (e) { con.toast("Drain failed: " + (e.detail || e.message)); }
    );
  }
  function purge() {
    apiFetch("POST", "/workers/purge_dead").then(
      function () { con.toast("Purged dead workers"); workers.refetch(); },
      function (e) { con.toast("Purge failed: " + (e.detail || e.message)); }
    );
  }
  return (
    <div data-testid="nv-sys-workers">
      <div className="nv-sys-row-head">
        <div className="nv-sys-subtitle">Workers</div>
        <span style={{ flex: 1 }} />
        <button type="button" className="nv-btn-secondary"
          data-testid="nv-sys-purge" onClick={purge}>Purge dead</button>
      </div>
      <div className="nv-worker-list">
        {rows.map(function (w) {
          var status = w.status || w.state || "?";
          return (
            <div key={w.id} className="nv-worker-row"
              data-testid={"nv-worker:" + w.id}>
              <span className="nv-health-dot" style={{
                background: status === "active" ? "var(--green)"
                  : status === "draining" ? "var(--amber)" : "var(--text-4)",
              }} />
              <span className="nv-worker-id">{w.id}</span>
              <span className="nv-worker-fact">{status}</span>
              {w.lanes != null ? (
                <span className="nv-worker-fact">
                  {Array.isArray(w.lanes) ? w.lanes.join(" ") : w.lanes}
                </span>
              ) : null}
              {w.in_flight != null ? (
                <span className="nv-worker-fact">{w.in_flight} in flight</span>
              ) : null}
              <span style={{ flex: 1 }} />
              {status !== "dead" ? (
                <button type="button" className="nv-btn-secondary"
                  onClick={function () { drain(w.id); }}>Drain</button>
              ) : null}
            </div>
          );
        })}
        {!rows.length ? (
          <div className="nv-bind-empty">No workers registered.</div>
        ) : null}
      </div>
    </div>
  );
}

// Cross-workspace attention: fan out over every workspace's pending
// yields and flatten - the system-level "needs a human" panel.
function NV_AttentionEverywhere() {
  var con = NV_useConsole();
  var wids = (con.workspaces || []).map(function (w) { return w.id; });
  var pending = window.primerApi.useResource(
    "nv-sys:attention:" + wids.join(","),
    function (signal) {
      return Promise.all(wids.map(function (wid) {
        return SH_api.pendingYields(wid, signal).then(function (out) {
          return ((out && out.items) || []).map(function (row) {
            row._wid = wid;
            return row;
          });
        }, function () { return []; });
      })).then(function (nested) {
        return { items: [].concat.apply([], nested) };
      });
    },
    { pollMs: 10000, deps: [wids.join(",")] }
  );
  var items = (pending.data && pending.data.items) || [];
  function wsName(wid) {
    var ws = (con.workspaces || []).find(function (w) { return w.id === wid; });
    return (ws && (ws.name || ws.id)) || wid;
  }
  function openItem(row) {
    var verb = con.registry.get("workspace.switch");
    if (verb) verb.run({ wid: row._wid });
    con.goView("studio");
    con.setDoc({ kind: "session", ref: row.session_id });
  }
  return (
    <div data-testid="nv-sys-attention">
      <div className="nv-sys-subtitle nv-sys-attn-title">
        Needs a human — every workspace
      </div>
      {!items.length ? (
        <div className="nv-bind-empty">Nothing is waiting on a person.</div>
      ) : (
        <div className="nv-attn-list">
          {items.map(function (row, i) {
            var ident = NV_identity(null);
            return (
              <div key={(row.session_id || i) + ":" + i} className="nv-attn-row">
                <svg width="11" height="11" viewBox="0 0 12 12"
                  style={{ color: "var(--attention)", flexShrink: 0 }}>
                  <path d={ident.d} fill="currentColor" />
                </svg>
                <span className="nv-attn-session">{row.session_id}</span>
                <span className="nv-attn-kind">
                  {row.kind || row.reason || "approval"}
                </span>
                <span style={{ flex: 1 }} />
                <span className="nv-attn-ws">{wsName(row._wid)}</span>
                <button type="button" className="nv-btn-secondary"
                  data-testid={"nv-attn-open:" + (row.session_id || i)}
                  onClick={function () { openItem(row); }}>Open</button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function NV_SysDashboard() {
  return (
    <div data-testid="nv-sys-dashboard">
      <div className="nv-plat-title nv-sys-title">Dashboard</div>
      <NV_HealthCards />
      <NV_WorkerFleet />
      <NV_AttentionEverywhere />
    </div>
  );
}

// The operator's own page: password + linked accounts. API tokens have
// their own nav (they are also each user's own).
function NV_SysProfile() {
  var con = NV_useConsole();
  var curState = React.useState("");
  var cur = curState[0];
  var setCur = curState[1];
  var nextState = React.useState("");
  var next = nextState[0];
  var setNext = nextState[1];
  var errState = React.useState(null);
  var err = errState[0];
  var setErr = errState[1];
  var busyState = React.useState(false);
  var busy = busyState[0];
  var setBusy = busyState[1];
  function submit() {
    if (busy || !cur || next.length < 8) {
      if (next && next.length < 8) setErr({ message: "New password needs at least 8 characters." });
      return;
    }
    setBusy(true);
    setErr(null);
    window.primerApi.apiFetch("POST", "/auth/change-password",
      { current_password: cur, new_password: next }).then(function () {
      setBusy(false);
      setCur("");
      setNext("");
      con.toast("Password changed");
    }, function (e) {
      setBusy(false);
      setErr(e);
    });
  }
  return (
    <div data-testid="nv-sys-profile">
      <div className="nv-plat-title nv-sys-title">
        Profile — {con.username}
      </div>
      <div className="nv-sys-subtitle">Change password</div>
      {err ? (
        <div className="nv-form-error">{err.detail || err.message}</div>
      ) : null}
      <div className="nv-profile-pw">
        <input className="nv-input" type="password" value={cur}
          placeholder="Current password" data-testid="nv-pw-current"
          autoComplete="current-password"
          onChange={function (ev) { setCur(ev.target.value); }} />
        <input className="nv-input" type="password" value={next}
          placeholder="New password (min 8 chars)" data-testid="nv-pw-next"
          autoComplete="new-password"
          onChange={function (ev) { setNext(ev.target.value); }} />
        <button type="button" className="nv-btn-primary" disabled={busy}
          data-testid="nv-pw-submit" onClick={submit}>
          {busy ? "Changing…" : "Change password"}
        </button>
      </div>
      {/* One-title rule: the re-hosted page renders its own
          action-bearing "Linked accounts" header. */}
      {typeof window.LA_LinkedAccountsPage === "function"
        ? <window.LA_LinkedAccountsPage />
        : null}
    </div>
  );
}

// nav -> body. The re-hosted pages carry their own action headers, so
// each mounts bare (one-title rule).
function NV_SysBody(props) {
  var con = NV_useConsole();
  var nav = props.nav;
  if (nav === "dashboard") return <NV_SysDashboard />;
  if (nav === "profile") return <NV_SysProfile />;
  if (nav === "users") return <window.ADM_AdminUsersPage />;
  if (nav === "apikeys") return <window.AT_ApiTokensPage />;
  if (nav === "sso") return <window.SSO_ProvidersPage />;
  if (nav === "mcp") return <window.MC_McpPage />;
  if (nav === "internal") {
    return (
      <window.InternalCollectionsPage pushToast={window.primerApi.toastPush} />
    );
  }
  if (nav === "activity") return <window.SH_ActivityPanel />;
  if (nav === "setup") {
    return (
      <window.SetupWizardSteps
        onComplete={function () { con.toast("Setup re-run complete"); }} />
    );
  }
  return null;
}

function NV_System() {
  var con = NV_useConsole();
  var navs = NV_sysNavsFor(con.role);
  var nav = con.view.nav && navs.indexOf(con.view.nav) >= 0
    ? con.view.nav
    : navs[0];
  return (
    <div className="nv-plat" data-testid="nv-system">
      <div className="nv-plat-nav" data-testid="nv-sys-nav">
        {navs.map(function (id) {
          return (
            <div key={id} className="nv-plat-row"
              data-active={id === nav ? "true" : "false"}
              data-testid={"nv-sys-row:" + id}
              onClick={function () { con.goView("system", id); }}>
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none"
                stroke="currentColor" strokeWidth="1.2"
                strokeLinejoin="round" className="nv-plat-row-icon">
                <path d={NV_SYS_ICONS[id]} />
              </svg>
              <span>{NV_SYS_LABELS[id]}</span>
            </div>
          );
        })}
      </div>
      <div className="nv-plat-main">
        <div className="nv-plat-wrap" data-testid={"nv-sys-page:" + nav}>
          <NV_SysBody nav={nav} />
        </div>
      </div>
    </div>
  );
}

window.NV_sysNavsFor = NV_sysNavsFor;
window.NV_System = NV_System;
