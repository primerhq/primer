/* global React, SH_api, NV_useConsole, NV_identity, relativeTime, PK_Section, PK_Field, Btn */
// The System view (wiring plan P5 T11): SYSNAV rail + one body per nav.
// The dashboard is fresh (health cards off /health, the worker fleet
// with drain/purge, and the cross-workspace "needs a human" panel via
// client fan-out over pending yields); the admin surfaces re-host the
// existing pages (users, API tokens, SSO, MCP, internal collections,
// activity) - they keep their handlers, lose their chrome; the
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
  profile: "Profile",
};

// R5 ruling: notes section 4's own intro says "all admin-gated except
// Profile (restricted users see only Profile)" - every section but
// Profile requires admin, for EVERY non-admin role, not just restricted.
// The prior NV_SYS_USER_NAVS = ["apikeys", "profile"] was drift: apikeys
// used to double as the personal-tokens surface, so a non-admin needed
// it to self-serve. That surface now lives under Profile (R5 tokens
// ruling, NV_SysProfile below) and the System apikeys nav is the admin
// ALL-USERS token table, so the gate can finally match the notes exactly
// with nothing lost.
function NV_sysNavsFor(role) {
  var all = ["dashboard", "users", "apikeys", "sso", "mcp", "internal",
    "activity", "profile"];
  if (role === "admin") return all;
  return ["profile"];
}

// --- Dashboard bits --------------------------------------------------------

// notes section 4: "health cards (scheduler, worker pool, sessions
// active, attention count)" - the previous 4 cards (platform/in-flight/
// claims/missed-heartbeats) were a scheduler-internals dump, not this
// set. Sessions-active and attention-count need no new backend route:
// GET /sessions?session_state=running&limit=1 (the turns in flight right now;
// the older `status` filter also counted a session parked on a yielding tool
// and one queued for a worker, C-036) and SH_api.pendingAttention() (the
// batch-1 aggregate) both already carry a `.total` the UI never asked for
// before.
function NV_HealthCards() {
  var health = window.primerApi.useResource(
    "nv-sys:health",
    function (signal) {
      return window.primerApi.apiFetch("GET", "/health", null,
        { signal: signal });
    },
    { pollMs: 5000 }
  );
  var activeSessions = window.primerApi.useResource(
    "nv-sys:sessions-active",
    function (signal) {
      return window.primerApi.apiFetch(
        "GET", "/sessions?session_state=running&limit=1", null, { signal: signal });
    },
    { pollMs: 10000 }
  );
  var attention = window.primerApi.useResource(
    "nv-sys:health-attention",
    function (signal) { return SH_api.pendingAttention(signal); },
    { pollMs: 10000 }
  );
  var data = health.data || {};
  var wp = data.worker_pool || {};
  var sched = data.scheduler || {};
  var schedOk = sched.alive === true && !sched.degraded;
  var attnTotal = attention.data && attention.data.total != null
    ? attention.data.total : null;
  // Three-state read on each card's OWN backing resource (2026-09-10):
  // "ready" (data has arrived - render it, even if a later poll is
  // currently failing in the background), "stuck" (no data AND
  // resourceState's debounced `degraded` signal - genuinely not
  // resolving, not a blip), "loading" (no data yet, not degraded - the
  // honest transient case). scheduler + worker pool share `health`, so
  // both go "stuck" together when the SAME fetch is the one failing.
  var healthState = window.primerApi.resourceState(health);
  var sessionsState = window.primerApi.resourceState(activeSessions);
  var attentionState = window.primerApi.resourceState(attention);
  var cards = [
    {
      k: "scheduler",
      v: healthState === "stuck" ? "unreachable"
        : healthState === "loading" ? "…"
        : (!sched.alive ? "down" : (sched.degraded ? "degraded" : "alive")),
      sub: healthState === "stuck" ? "health check failing"
        : healthState === "loading" ? "checking…"
        : sched.degraded ? (sched.degraded_reason || "degraded")
        : (sched.alive ? (sched.detail || "healthy") : "no scheduler attached"),
      tone: healthState === "stuck" ? "var(--red)"
        : healthState === "loading" ? "var(--text-4)"
        : (schedOk ? "var(--green)" : (sched.alive ? "var(--amber)" : "var(--red)")),
    },
    {
      k: "worker pool",
      v: healthState === "stuck" ? "unreachable"
        : String(wp.in_flight == null ? "n/a" : wp.in_flight),
      sub: healthState === "stuck" ? "health check failing"
        : "of " + (wp.capacity == null ? "n/a" : wp.capacity) + " capacity",
      tone: healthState === "stuck" ? "var(--red)" : "var(--blue)",
    },
    {
      k: "sessions active",
      v: sessionsState === "stuck" ? "unreachable"
        : String(activeSessions.data && activeSessions.data.total != null
          ? activeSessions.data.total : "…"),
      sub: sessionsState === "stuck" ? "list failing" : "running now",
      tone: sessionsState === "stuck" ? "var(--red)" : "var(--violet)",
    },
    {
      k: "attention",
      v: attentionState === "stuck" ? "unreachable"
        : String(attnTotal == null ? "…" : attnTotal),
      sub: attentionState === "stuck" ? "aggregate failing" : "needs a human",
      tone: attentionState === "stuck" ? "var(--red)"
        : (attnTotal ? "var(--attention)" : "var(--text-4)"),
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
  // notes section 4: "worker rows show ... turns + uptime". WorkerInfo
  // itself (primer/int/scheduler.py) carries started_at (uptime, computed
  // client-side) but no per-worker task count; /workers/stats answers
  // per-(worker, kind, status) lane counters, so a row's "turns" is the
  // sum of `tasks` across every lane entry naming that worker id -
  // same source workers.jsx's own lane-stats section already reads, just
  // grouped by worker here instead of shown as one global table.
  var laneStats = window.primerApi.useResource(
    "nv-sys:worker-stats",
    function (signal) { return apiFetch("GET", "/workers/stats", null, { signal: signal }); },
    { pollMs: 8000 }
  );
  var turnsByWorker = {};
  ((laneStats.data && laneStats.data.items) || []).forEach(function (lane) {
    var wid = lane.worker;
    turnsByWorker[wid] = (turnsByWorker[wid] || 0) + (lane.tasks || 0);
  });
  var rows = (workers.data && workers.data.items) || [];
  var workersState = window.primerApi.resourceState(workers);
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
              <span className="nv-worker-fact" data-testid={"nv-worker-turns:" + w.id}>
                {turnsByWorker[w.id] || 0} turns
              </span>
              {w.started_at ? (
                <span className="nv-worker-fact" data-testid={"nv-worker-uptime:" + w.id}>
                  started {typeof relativeTime === "function"
                    ? relativeTime(Math.max(0,
                        (Date.now() - new Date(w.started_at).getTime()) / 1000))
                    : w.started_at}
                </span>
              ) : null}
              <span style={{ flex: 1 }} />
              {status !== "dead" ? (
                <button type="button" className="nv-btn-secondary"
                  onClick={function () { drain(w.id); }}>Drain</button>
              ) : null}
            </div>
          );
        })}
        {!rows.length && workersState === "stuck" ? (
          <div className="nv-bind-empty" style={{ color: "var(--red)" }}>
            Couldn't load workers — the list keeps failing (retrying).
          </div>
        ) : !rows.length && workersState === "loading" ? (
          <div className="nv-bind-empty">Loading…</div>
        ) : !rows.length ? (
          <div className="nv-bind-empty">No workers registered.</div>
        ) : null}
      </div>
    </div>
  );
}

function NV_setupFix(key) {
  if (key === "llm_provider" || key === "model_profile") {
    return { label: "Open Providers", action: "providers" };
  }
  return { label: "Re-run seed", action: "seed" };
}

function NV_setupCardView(state) {
  var preds = (state && Array.isArray(state.predicates)) ? state.predicates : [];
  var total = preds.length || 6;
  var failing = [];
  for (var i = 0; i < preds.length; i++) {
    var p = preds[i];
    if (!p.ok) {
      var detailStr = p.detail != null ? (typeof SW_tidy === "function" ? SW_tidy(p.detail) : String(p.detail)) : "";
      failing.push({
        key: p.key,
        label: p.label,
        detail: detailStr,
        fix: NV_setupFix(p.key),
      });
    }
  }
  if (failing.length === 0) {
    return {
      tone: "ok",
      headline: "All " + total + " setup checks pass",
      failing: [],
    };
  }
  var headline = failing.length === 1
    ? "1 of " + total + " setup checks fails"
    : failing.length + " of " + total + " setup checks fail";
  return {
    tone: "error",
    headline: headline,
    failing: failing,
  };
}

function NV_seedOutcome(result) {
  var res = result || {};
  var errors = res.errors || [];
  if (errors.length) {
    var errParts = errors.map(function (e) {
      var key = e[0];
      var raw = String(e[1] == null ? "" : e[1]);
      var msg = typeof SW_tidy === "function" ? SW_tidy(raw) : raw;
      if (msg.length > 200) {
        msg = msg.slice(0, 200) + "…";
      }
      return key + ": " + msg;
    });
    return {
      kind: "error",
      text: "Seed finished with errors: " + errParts.join("; "),
    };
  }
  var created = res.created || [];
  if (created.length) {
    return {
      kind: "info",
      text: "Seed ran: created " + created.join(", ") + ".",
    };
  }
  return {
    kind: "info",
    text: "Seed ran: nothing was missing.",
  };
}

function NV_resetOutcome(result) {
  var reset = (result && result.reset) || [];
  return "Reset the default agents: " + reset.join(", ") + ".";
}

var NV_RESET_AGENTS_CONFIRM = {
  title: "Reset default agents",
  message: "This restores the description, system prompt and tools of operator, builder, planner, explorer and tool-runner to their defaults. Your edits to those agents are lost; the model profile each one runs on is kept.",
  confirmLabel: "Reset agents",
  danger: true,
};

var NV_CAPABILITY_GATES = {
  huggingface: "local embedder + cross-encoder + speech models",
  lance: "local semantic-search vector store",
  docker: "container workspace backend",
  kubernetes: "kubernetes workspace backend",
  channels: "Slack / Discord / Telegram channel bridges",
};

function NV_capabilityRows(caps) {
  if (!caps) return [];
  var data = caps.data || caps;
  var rows = [];
  var extras = (data && data.extras) || {};
  var extraKeys = Object.keys(extras).sort();
  for (var i = 0; i < extraKeys.length; i++) {
    var name = extraKeys[i];
    var status = extras[name] || {};
    rows.push({
      name: name,
      ok: !!status.installed,
      text: NV_CAPABILITY_GATES[name] || "",
    });
  }
  if (data && data.speech) {
    var sp = data.speech;
    var speechOk = !!(sp.stt_configured || sp.tts_configured);
    rows.push({
      name: "speech",
      ok: speechOk,
      text: "stt " + (sp.stt_configured ? "configured" : "not configured") + ", tts " + (sp.tts_configured ? "configured" : "not configured"),
    });
  }
  return rows;
}

function NV_SysSetupCard() {
  var con = NV_useConsole();
  // Read once, then only on Check again, a seed or a reset (plan 10, Review Focus 4): each read lists the LLM's models and re-attaches the
  // default workspace, so a tab re-show or an idle wake-up must not read it again (refetchOnWake: false; audit G1-09).
  var setupState = window.primerApi.useResource(
    "nv-sys:setup-state",
    function (signal) {
      return window.primerApi.apiFetch("GET", "/setup/state", null, { signal: signal });
    },
    { refetchOnWake: false }
  );
  var setupResourceState = window.primerApi.resourceState(setupState);
  var setupUnavailable = !!(setupState.error && setupState.data == null)
    || setupResourceState === "stuck";
  var caps = window.primerApi.useCapabilities();
  var capRows = NV_capabilityRows(caps);
  var busyState = React.useState(null);
  var busy = busyState[0];
  var setBusy = busyState[1];

  NV_useMenuDismiss(con, "sys-setup", "nv-sys-setup-menu-btn");
  // The menu follows the kit's menu pattern (audit G1-14): its first item takes the focus when it opens, and the arrow keys, Home and End move
  // the focus between its items (PK_menuKeys, platform-kit.jsx).
  var menuListRef = React.useRef(null);
  var menuOpen = con.openMenu === "sys-setup";
  React.useEffect(function () {
    if (menuOpen && typeof PK_focusFirstItem === "function") PK_focusFirstItem(menuListRef);
  }, [menuOpen]);

  var cardView = NV_setupCardView(setupState.data);

  function toggleMenu(ev) {
    if (ev) ev.stopPropagation();
    if (busy != null) return;
    con.toggleMenu("sys-setup");
  }

  function recheck() {
    if (busy != null) return;
    con.toggleMenu(null);
    setupState.refetch();
  }

  function rerunSeed() {
    if (busy != null) return;
    con.toggleMenu(null);
    setBusy("seed");
    window.primerApi.apiFetch("POST", "/setup/seed", null, {}).then(
      function (res) {
        var outcome = NV_seedOutcome(res);
        con.toast(outcome.text, outcome.kind);
        setupState.refetch();
      },
      function (e) {
        con.toast("Re-run seed failed: " + window.primerApi.readRefusal(e).message, {
          kind: "error",
          requestId: e && e.requestId,
        });
      }
    ).finally(function () {
      setBusy(null);
    });
  }

  function resetAgents() {
    if (busy != null) return;
    con.toggleMenu(null);
    window.confirmDialog(NV_RESET_AGENTS_CONFIRM).then(function (ok) {
      if (!ok) return;
      setBusy("reset");
      window.primerApi.apiFetch("POST", "/setup/reset_agents", null, {}).then(
        function (res) {
          con.toast(NV_resetOutcome(res), "info");
          setupState.refetch();
        },
        function (e) {
          con.toast("Reset failed: " + window.primerApi.readRefusal(e).message, {
            kind: "error",
            requestId: e && e.requestId,
          });
        }
      ).finally(function () {
        setBusy(null);
      });
    });
  }

  var headline = busy === "seed"
    ? "Running the seed…"
    : busy === "reset"
    ? "Resetting the default agents…"
    : setupUnavailable
    ? "Couldn't check setup"
    : setupResourceState === "loading"
    ? "Checking setup…"
    : cardView.headline;

  var tone = (busy || (setupResourceState === "loading" && !setupUnavailable))
    ? ""
    : setupUnavailable
    ? "error"
    : cardView.tone;

  var dotColor = (busy || (setupResourceState === "loading" && !setupUnavailable))
    ? "var(--text-4)"
    : (setupUnavailable || cardView.tone === "error")
    ? "var(--red)"
    : "var(--green)";

  var PK_FoldComp = window.PK_Fold || (typeof PK_Fold !== "undefined" ? PK_Fold : null);
  var IconComp = window.Icon || (typeof Icon !== "undefined" ? Icon : null);

  return (
    <div className="nv-sys-setup" data-testid="nv-sys-setup" data-tone={tone}>
      <div className="nv-sys-setup-title" data-testid="nv-sys-setup-title">Setup</div>
      <div className="nv-sys-setup-head">
        <span className="nv-health-dot" style={{ background: dotColor }} />
        <span className="nv-sys-setup-headline" data-testid="nv-sys-setup-headline">
          {headline}
        </span>
        <span style={{ flex: 1 }} />
        <div style={{ position: "relative" }}>
          <button
            type="button"
            className="pk-card-menu-btn"
            data-testid="nv-sys-setup-menu-btn"
            aria-label="Setup actions"
            aria-haspopup="menu"
            aria-expanded={menuOpen ? "true" : "false"}
            onClick={toggleMenu}
          >
            •••
          </button>
          {menuOpen && (
            <div
              className="nv-sys-setup-menu pk-card-menu-list"
              data-testid="nv-sys-setup-menu"
              role="menu"
              ref={menuListRef}
              onKeyDown={typeof PK_menuKeys === "function" ? PK_menuKeys : undefined}
              onClick={function (ev) { ev.stopPropagation(); }}
              onBlur={function (ev) {
                if (NV_menuFocusLeft(ev, "nv-sys-setup-menu-btn")) con.toggleMenu(null);
              }}
            >
              <button
                type="button"
                className="pk-card-menu-item"
                role="menuitem"
                data-testid="nv-sys-setup-recheck"
                onClick={recheck}
              >
                Check again
              </button>
              <button
                type="button"
                className="pk-card-menu-item"
                role="menuitem"
                data-testid="nv-sys-setup-rerun-seed"
                onClick={rerunSeed}
              >
                Re-run seed
              </button>
              <button
                type="button"
                className="pk-card-menu-item"
                role="menuitem"
                data-danger="true"
                data-testid="nv-sys-setup-reset-agents"
                onClick={resetAgents}
              >
                Reset default agents
              </button>
            </div>
          )}
        </div>
      </div>

      {setupUnavailable ? (
        <button type="button" className="nv-btn-secondary"
          data-testid="nv-sys-setup-retry" onClick={recheck}>
          Retry
        </button>
      ) : null}

      {cardView.failing.length > 0 && (
        <div className="nv-sys-setup-list">
          {cardView.failing.map(function (f) {
            return (
              <div
                key={f.key}
                className="nv-sys-setup-item"
                data-testid={"nv-sys-setup-check:" + f.key}
              >
                <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1, minWidth: 0 }}>
                  <span style={{ fontWeight: 500 }}>{f.label}</span>
                  {/* A detail may hold a long URL: it wraps anywhere rather than widening the row past a phone's width (audit G1-15). */}
                  {f.detail ? (
                    <span className="muted text-sm" data-testid={"nv-sys-setup-detail:" + f.key}
                      style={{ color: "var(--text-3)", fontSize: 11, overflowWrap: "anywhere" }}>
                      {f.detail}
                    </span>
                  ) : null}
                </div>
                <button
                  type="button"
                  className="nv-btn-secondary"
                  data-testid={"nv-sys-setup-fix:" + f.key}
                  onClick={function () {
                    if (f.fix.action === "providers") {
                      con.goView("platform", "providers");
                    } else {
                      rerunSeed();
                    }
                  }}
                >
                  {f.fix.label}
                </button>
              </div>
            );
          })}
        </div>
      )}

      {PK_FoldComp ? (
        <div style={{ marginTop: 10 }}>
          <PK_FoldComp label="Installed extras" testid="nv-sys-setup-extras">
            <div className="tbl-wrap" style={{ marginTop: 6 }}>
              <table className="tbl">
                <thead>
                  <tr><th></th><th>Capability</th><th>Gates</th></tr>
                </thead>
                <tbody>
                  {capRows.map(function (row) {
                    return (
                      <tr key={row.name} data-testid={"nv-sys-capability-row:" + row.name}>
                        <td>
                          {IconComp ? (
                            <IconComp
                              name={row.ok ? "check" : "x-circle"}
                              className={row.ok ? undefined : "muted"}
                              size={14}
                            />
                          ) : (
                            <span className="ico" data-icon={row.ok ? "check" : "x-circle"} />
                          )}
                        </td>
                        <td className="mono">{row.name}</td>
                        <td className="muted text-sm">{row.text || "-"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </PK_FoldComp>
        </div>
      ) : null}
    </div>
  );
}

// Cross-workspace attention: GET /v1/yields/pending (the real aggregate,
// batch-1) - workspace_id/workspace_name/session_id/session_name/kind/
// agent_binding/created_at per row, already sorted newest-first and
// capped server-side. Falls back to the old per-workspace fan-out ONLY
// against a server that predates the aggregate (same 404 guard
// nv-rail.jsx's Inbox already uses for the same endpoint), so this
// panel degrades the same way the rail does rather than differently.
function NV_AttentionEverywhere() {
  var con = NV_useConsole();
  var wids = (con.workspaces || []).map(function (w) { return w.id; });
  var pending = window.primerApi.useResource(
    "nv-sys:attention:" + wids.join(","),
    function (signal) {
      return SH_api.pendingAttention(signal).catch(function (err) {
        if (err && err.status !== 404) throw err;
        return Promise.all(wids.map(function (wid) {
          return SH_api.pendingYields(wid, signal).then(function (out) {
            return ((out && out.items) || []).map(function (row) {
              return Object.assign({}, row, {
                workspace_id: wid, session_id: row.session_id,
              });
            });
          }, function () { return []; });
        })).then(function (nested) {
          return { items: [].concat.apply([], nested) };
        });
      });
    },
    { pollMs: 10000, deps: [wids.join(",")] }
  );
  var items = (pending.data && pending.data.items) || [];
  var pendingState = window.primerApi.resourceState(pending);
  function wsName(row) {
    if (row.workspace_name) return row.workspace_name;
    var ws = (con.workspaces || []).find(function (w) {
      return w.id === row.workspace_id;
    });
    return (ws && (ws.name || ws.id)) || row.workspace_id;
  }
  // Same promoted-open contract nv-studio.jsx wires for the rail's own
  // Inbox rows (notes 2.1/2.2: every rail-triggered open is promoted).
  function openItem(row) {
    if (row.workspace_id && row.workspace_id !== con.wid) {
      var verb = con.registry.get("workspace.switch");
      if (verb) verb.run({ wid: row.workspace_id });
    }
    con.goView("studio");
    con.setDoc({ kind: "session", ref: row.session_id });
    if (con.promoteDoc) con.promoteDoc("session:" + row.session_id);
  }
  return (
    <div data-testid="nv-sys-attention">
      <div className="nv-sys-subtitle nv-sys-attn-title">
        Needs a human — every workspace
      </div>
      {pendingState === "stuck" ? (
        <div className="nv-bind-empty" style={{ color: "var(--red)" }}>
          Couldn't load — the aggregate keeps failing (retrying).
        </div>
      ) : !items.length && pendingState === "loading" ? (
        <div className="nv-bind-empty">Loading…</div>
      ) : !items.length ? (
        <div className="nv-bind-empty">Nothing is waiting on a person.</div>
      ) : (
        <div className="nv-attn-list">
          {items.map(function (row, i) {
            var ident = NV_identity(row.agent_binding || null);
            return (
              <div key={(row.session_id || i) + ":" + i} className="nv-attn-row">
                <svg width="11" height="11" viewBox="0 0 12 12"
                  style={{ color: ident.color || "var(--attention)", flexShrink: 0 }}>
                  <path d={ident.d} fill="currentColor" />
                </svg>
                <span className="nv-attn-session">
                  {row.session_name || row.session_id}
                </span>
                <span className="nv-attn-kind">{row.kind || "approval"}</span>
                <span style={{ flex: 1 }} />
                <span className="nv-attn-ws">{wsName(row)}</span>
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
      <NV_SysSetupCard />
      <NV_WorkerFleet />
      <NV_AttentionEverywhere />
    </div>
  );
}

// The operator's own page: password + personal API tokens + linked
// accounts. R5 tokens ruling: personal tokens move HERE (every role
// reaches Profile); the System apikeys nav (admin-only, per the R5 role
// gate above) becomes the admin all-users token table instead - the two
// surfaces now share AT_ApiTokensPage's machinery via its mode prop
// rather than duplicating table/row/dialog code.
// The sentence for a failed password change (admin review ADM-25). POST /auth/change-password answers a wrong current password with 401 and
// the code invalid_credentials (in the problem envelope's detail and in extensions.error), which the page used to print as it came. The same
// code also answers an account that has no password (single sign-on only) and a lost race with another change, so the sentence says "not
// right" and names that case. The mapping is on the CODE: an expired session answers 401 too, with another detail, and keeps the server's own
// words. A bare machine code that is not known is never the sentence (the person is told which refusal it was, in brackets), except the auth
// gate's two, which the one reader (window.primerApi.readRefusal) has sentences for, and a field error, which names its field.
// tests/ui/test_profile_page_messages.py runs this in MiniRacer.
function NV_passwordChangeMessage(err) {
  var r = window.primerApi.readRefusal(err, "Could not change the password.");
  if (r.code === "invalid_credentials") {
    return "The current password is not right. (An account that only signs in through single sign-on has no password to change.)";
  }
  if (!r.sentence && r.code && !r.field && r.code !== "auth_required" && r.code !== "forbidden_role") {
    return "Could not change the password (" + r.code + ").";
  }
  return r.message;
}

function NV_passwordFormErrors(form) {
  var f = form || {};
  var errors = {};
  if (!f.current) {
    errors.current = "Enter your current password.";
  }
  if (!f.next || f.next.length < 8) {
    errors.next = "The new password needs at least 8 characters.";
  }
  if (f.confirm !== f.next) {
    errors.confirm = "The two new passwords do not match.";
  }
  return errors;
}

function NV_signInText(status) {
  if (!status) return "";
  var m = status.sign_in_method;
  if (m === "password") return "Signed in with a password";
  if (m === "sso") {
    return status.sign_in_provider
      ? "Signed in with single sign-on (" + status.sign_in_provider + ")"
      : "Signed in with single sign-on";
  }
  if (m === "api_token") return "Signed in with an API key";
  if (m === "auth_disabled") return "Sign-in is off on this server";
  return "";
}

function NV_SysProfile() {
  var con = NV_useConsole();
  var authStatus = window.primerApi.useResource(
    "nv-shell:auth-status",
    function (signal) {
      return window.primerApi.apiFetch("GET", "/auth/status", null, { signal: signal });
    },
    { retryMs: 2000 }
  );
  var curState = React.useState("");
  var cur = curState[0];
  var setCur = curState[1];
  var nextState = React.useState("");
  var next = nextState[0];
  var setNext = nextState[1];
  var confirmState = React.useState("");
  var confirm = confirmState[0];
  var setConfirm = confirmState[1];
  var submittedState = React.useState(false);
  var submitted = submittedState[0];
  var setSubmitted = submittedState[1];
  var errState = React.useState(null);
  var err = errState[0];
  var setErr = errState[1];
  var busyState = React.useState(false);
  var busy = busyState[0];
  var setBusy = busyState[1];

  var displayName = con.username || "Signed-in account";
  var initials = String(con.username || "?").slice(0, 2).toLowerCase();
  var signInText = NV_signInText(authStatus && authStatus.data);

  var fieldErrors = NV_passwordFormErrors({ current: cur, next: next, confirm: confirm });
  var hasErrors = Object.keys(fieldErrors).length > 0;

  function submit() {
    if (busy) return;
    setSubmitted(true);
    if (hasErrors) return;
    setBusy(true);
    setErr(null);
    window.primerApi.apiFetch("POST", "/auth/change-password", {
      current_password: cur,
      new_password: next,
    }).then(
      function () {
        setBusy(false);
        setCur("");
        setNext("");
        setConfirm("");
        setSubmitted(false);
        con.toast("Password changed");
      },
      function (e) {
        setBusy(false);
        setErr(e);
      }
    );
  }

  var PK_Section = window.PK_Section;
  var PK_Field = window.PK_Field;

  return (
    <div className="col" data-testid="nv-sys-profile" style={{ gap: 20 }}>
      <div className="nv-plat-title nv-sys-title" data-testid="nv-sys-profile-title">
        Profile
      </div>

      <div className="nv-sys-identity" data-testid="nv-sys-identity">
        <div className="nv-avatar">{initials}</div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="nv-sys-identity-name" data-testid="nv-sys-identity-name">
            {displayName}
          </div>
          <div className="nv-sys-identity-facts">
            <span data-testid="nv-sys-identity-role">{con.role}</span>
            {signInText ? (
              <span data-testid="nv-sys-identity-sign-in">{signInText}</span>
            ) : null}
          </div>
        </div>
      </div>

      <PK_Section title="Password" testid="nv-sys-password" cols={3}>
        <PK_Field label="Current password" error={submitted ? fieldErrors.current : null}>
          <input
            className="input"
            type="password"
            value={cur}
            aria-label="Current password"
            autoComplete="current-password"
            data-testid="nv-pw-current"
            onChange={function (ev) { setCur(ev.target.value); }}
          />
        </PK_Field>
        <PK_Field label="New password" help="At least 8 characters" error={submitted ? fieldErrors.next : null}>
          <input
            className="input"
            type="password"
            value={next}
            aria-label="New password"
            autoComplete="new-password"
            data-testid="nv-pw-next"
            onChange={function (ev) { setNext(ev.target.value); }}
          />
        </PK_Field>
        <PK_Field label="Confirm new password" error={submitted ? fieldErrors.confirm : null}>
          <input
            className="input"
            type="password"
            value={confirm}
            aria-label="Confirm new password"
            autoComplete="new-password"
            data-testid="nv-pw-confirm"
            onChange={function (ev) { setConfirm(ev.target.value); }}
          />
        </PK_Field>
        <div className="nv-sys-pw-actions">
          {err ? (
            <div data-testid="nv-pw-error" role="alert" style={{ marginRight: "auto" }}>
              <div className="nv-form-error">{NV_passwordChangeMessage(err)}</div>
            </div>
          ) : null}
          <Btn kind="primary" busy={busy} data-testid="nv-pw-submit" onClick={submit}>
            {busy ? "Changing…" : "Change password"}
          </Btn>
        </div>
      </PK_Section>

      {/* One-title rule: the re-hosted pages render their own
          action-bearing headers ("API tokens" / "Linked accounts"). */}
      {typeof window.AT_ApiTokensPage === "function" ? (
        <window.AT_ApiTokensPage mode="personal" username={con.username} />
      ) : null}
      {typeof window.LA_LinkedAccountsPage === "function" ? (
        <window.LA_LinkedAccountsPage />
      ) : null}
    </div>
  );
}

// nav -> body. The re-hosted pages carry their own action headers, so
// each mounts bare (one-title rule).
function NV_SysBody(props) {
  var nav = props.nav;
  var username = props.username;
  if (nav === "dashboard") return <NV_SysDashboard />;
  if (nav === "profile") return <NV_SysProfile />;
  if (nav === "users") return <window.ADM_AdminUsersPage />;
  // R5 tokens ruling: this is now the admin all-users token table
  // (personal tokens live under Profile, above).
  if (nav === "apikeys") return <window.AT_ApiTokensPage mode="admin" username={username} />;
  if (nav === "sso") return <window.SSO_ProvidersPage />;
  if (nav === "mcp") return <window.MC_McpPage />;
  if (nav === "internal") {
    return (
      <window.InternalCollectionsPage pushToast={window.primerApi.toastPush} />
    );
  }
  if (nav === "activity") return <window.SH_ActivityPanel />;
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
            <button type="button" key={id} className="nv-plat-row"
              data-active={id === nav ? "true" : "false"}
              data-testid={"nv-sys-row:" + id}
              aria-current={id === nav ? "page" : undefined}
              onClick={function () { con.goView("system", id); }}>
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none"
                stroke="currentColor" strokeWidth="1.2"
                strokeLinejoin="round" className="nv-plat-row-icon">
                <path d={NV_SYS_ICONS[id]} />
              </svg>
              <span>{NV_SYS_LABELS[id]}</span>
            </button>
          );
        })}
      </div>
      <div className="nv-plat-main">
        <div className="nv-plat-wrap" data-testid={"nv-sys-page:" + nav}>
          <NV_SysBody nav={nav} username={con.username} />
        </div>
      </div>
    </div>
  );
}

window.NV_sysNavsFor = NV_sysNavsFor;
window.NV_System = NV_System;
window.NV_SysSetupCard = NV_SysSetupCard;
window.NV_setupFix = NV_setupFix;
window.NV_setupCardView = NV_setupCardView;
window.NV_seedOutcome = NV_seedOutcome;
window.NV_resetOutcome = NV_resetOutcome;
window.NV_RESET_AGENTS_CONFIRM = NV_RESET_AGENTS_CONFIRM;
window.NV_CAPABILITY_GATES = NV_CAPABILITY_GATES;
window.NV_capabilityRows = NV_capabilityRows;
window.NV_passwordFormErrors = NV_passwordFormErrors;
window.NV_signInText = NV_signInText;
window.NV_SysProfile = NV_SysProfile;
