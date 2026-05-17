/* global React, Icon, StatusPill, Btn, relativeTime, Banner */

const { apiFetch, useResource, useRouter } = window.matrixApi;

const STATUS_CHIPS = [
  { s: "created",   color: "var(--text-3)" },
  { s: "running",   color: "var(--blue)" },
  { s: "paused",    color: "var(--amber)" },
  { s: "ended",     color: "var(--green)" },
  { s: "failed",    color: "var(--red)" },
  { s: "cancelled", color: "var(--text-4)" },
];

const NON_TERMINAL = new Set(["created", "running", "paused", "waiting"]);
const PAGE_SIZE = 12;

// Apply UI filters on top of whatever the backend returned. The backend
// `?status=` query param accepts a single enum value (not a comma list)
// per matrix/api/routers/sessions.py — so a multi-status UI selection
// fetches the union without a server filter and narrows client-side.
function _filterRows(items, { statusSet, agentFilter, workspaceFilter, graphFilter, query }) {
  let arr = items;
  if (statusSet.size > 0 && statusSet.size < STATUS_CHIPS.length) {
    arr = arr.filter((s) => statusSet.has(s.status));
  }
  if (agentFilter) {
    arr = arr.filter((s) => s.binding?.agent_id === agentFilter);
  }
  if (workspaceFilter) {
    arr = arr.filter((s) => s.workspace_id === workspaceFilter);
  }
  if (graphFilter) {
    arr = arr.filter((s) => s.binding?.graph_id === graphFilter);
  }
  if (query) {
    const q = query.toLowerCase();
    arr = arr.filter((s) =>
      s.id.toLowerCase().includes(q) ||
      (s.binding?.agent_id || "").toLowerCase().includes(q) ||
      (s.binding?.graph_id || "").toLowerCase().includes(q) ||
      (s.workspace_id || "").toLowerCase().includes(q)
    );
  }
  return arr;
}

function _ageSecOrNull(iso) {
  if (!iso) return null;
  return (Date.now() - new Date(iso).getTime()) / 1000;
}

function SessionsList({ onNewSession }) {
  const { query: routerQuery, navigate } = useRouter();
  // Filters seed from URL query so deep-links work. State is local because
  // the spec calls for per-page UX, not bookmarkable links beyond the
  // initial preset.
  const initialStatus = (routerQuery.status || "").split(",").filter(Boolean);
  const [statusSet, setStatusSet] = React.useState(() => new Set(initialStatus));
  const [agentFilter, setAgentFilter] = React.useState(routerQuery.agent_id || "");
  const [workspaceFilter, setWorkspaceFilter] = React.useState(routerQuery.workspace_id || "");
  const [graphFilter, setGraphFilter] = React.useState(routerQuery.graph_id || "");
  const [textQuery, setTextQuery] = React.useState("");
  const [sortBy, setSortBy] = React.useState("created_at");
  const [sortDir, setSortDir] = React.useState("desc");
  const [page, setPage] = React.useState(1);

  // Active focus suspends polling so the operator typing in the filter
  // box doesn't get rug-pulled by a refresh.
  const filterFocused = React.useRef(false);

  // Build the backend query. Single-status filter applied server-side
  // when exactly one chip is on (cheaper transfer); multi-chip falls
  // back to client filter (server doesn't accept a comma-list).
  const params = new URLSearchParams();
  params.set("limit", "200");
  if (statusSet.size === 1) params.set("status", [...statusSet][0]);
  if (workspaceFilter) params.set("workspace_id", workspaceFilter);
  if (agentFilter) params.set("agent_id", agentFilter);
  if (graphFilter) params.set("graph_id", graphFilter);
  const apiPath = "/sessions?" + params.toString();

  // Spec calls for 3s polling while non-terminal sessions exist and 10s
  // otherwise. Implementing that requires reading useResource's own
  // output to decide its cadence — circular at call time. The clean
  // workaround is to hardcode 3000ms; the extra cost when nothing's
  // running is ~14 reqs/hour against a single short endpoint, which
  // is well below the threshold worth optimising. Revisit if the
  // backend ever publishes load metrics that justify the adaptive
  // cadence.
  const result = useResource(
    apiPath,
    (signal) => apiFetch("GET", apiPath, null, { signal }),
    {
      pollMs: 3000,
      pauseWhile: () => filterFocused.current,
      deps: [apiPath],
    }
  );

  // Combobox sources — fetched once, cached for the page's lifetime.
  const workspaces = useResource("sessions-list:workspaces",
    (s) => apiFetch("GET", "/workspaces?limit=200", null, { signal: s }), {});
  const agents = useResource("sessions-list:agents",
    (s) => apiFetch("GET", "/agents?limit=200", null, { signal: s }), {});
  const graphs = useResource("sessions-list:graphs",
    (s) => apiFetch("GET", "/graphs?limit=200", null, { signal: s }), {});

  const items = result.data?.items ?? [];
  const filtered = _filterRows(items, { statusSet, agentFilter, workspaceFilter, graphFilter, query: textQuery });

  // Sort the visible page on the chosen column. Sort runs before
  // pagination so the page reflects the global sort, not just the current
  // 12 rows.
  const sorted = [...filtered].sort((a, b) => {
    let av, bv;
    if (sortBy === "created_at" || sortBy === "last_turn_at") {
      av = a[sortBy] ? new Date(a[sortBy]).getTime() : 0;
      bv = b[sortBy] ? new Date(b[sortBy]).getTime() : 0;
    } else if (sortBy === "agent_id") {
      av = a.binding?.agent_id || "";
      bv = b.binding?.agent_id || "";
    } else {
      av = a[sortBy];
      bv = b[sortBy];
      if (av == null) av = "";
      if (bv == null) bv = "";
    }
    if (av < bv) return sortDir === "asc" ? -1 : 1;
    if (av > bv) return sortDir === "asc" ? 1 : -1;
    return 0;
  });
  const total = sorted.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const pageItems = sorted.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const toggleStatus = (s) => {
    setStatusSet((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s); else next.add(s);
      return next;
    });
    setPage(1);
  };

  const setSort = (col) => {
    if (sortBy === col) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortBy(col); setSortDir("desc"); }
  };

  const Th = ({ col, children, align }) => (
    <th
      className={sortBy === col ? "sorted" : ""}
      onClick={() => setSort(col)}
      style={{ textAlign: align || "left", cursor: "pointer" }}
    >
      {children}
      <span className="sort-arrow">{sortBy === col ? (sortDir === "asc" ? "↑" : "↓") : "↕"}</span>
    </th>
  );

  const liveCount = items.filter((s) => NON_TERMINAL.has(s.status)).length;

  return (
    <div className="col" style={{ gap: 14 }}>
      <SessionsHeader
        liveCount={liveCount}
        totalCount={items.length}
        onRefresh={result.refetch}
        onNewSession={onNewSession}
      />

      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter id, agent, workspace…"
            value={textQuery}
            onChange={(e) => { setTextQuery(e.target.value); setPage(1); }}
            onFocus={() => { filterFocused.current = true; }}
            onBlur={() => { filterFocused.current = false; }}
          />
        </div>
        <div className="sep-v" />
        <div className="chip-group" title="filter by status">
          {STATUS_CHIPS.map(({ s, color }) => (
            <span
              key={s}
              className={`chip-dot ${statusSet.has(s) ? "active" : ""}`}
              onClick={() => toggleStatus(s)}
              title={s}
              style={{ cursor: "pointer" }}
            >
              <span className="d" style={{ background: color }}></span>
            </span>
          ))}
        </div>
        <div className="sep-v" />
        <select
          className="select"
          value={agentFilter}
          onChange={(e) => { setAgentFilter(e.target.value); setPage(1); }}
        >
          <option value="">agent</option>
          {(agents.data?.items ?? []).map((a) => (
            <option key={a.id} value={a.id}>{a.id}</option>
          ))}
        </select>
        <select
          className="select"
          value={workspaceFilter}
          onChange={(e) => { setWorkspaceFilter(e.target.value); setPage(1); }}
        >
          <option value="">workspace</option>
          {(workspaces.data?.items ?? []).map((w) => (
            <option key={w.id} value={w.id}>{w.id.slice(0, 16)}{w.id.length > 16 ? "…" : ""}</option>
          ))}
        </select>
        <select
          className="select"
          value={graphFilter}
          onChange={(e) => { setGraphFilter(e.target.value); setPage(1); }}
        >
          <option value="">graph</option>
          {(graphs.data?.items ?? []).map((g) => (
            <option key={g.id} value={g.id}>{g.id}</option>
          ))}
        </select>
        <span title="Date-range filter — design deferred to v2" style={{ opacity: 0.5, cursor: "not-allowed", fontSize: 12, color: "var(--text-3)" }}>
          (date range — soon)
        </span>
        <span className="muted text-sm tabular" style={{ marginLeft: "auto" }}>
          <span className="mono" style={{ color: result.error ? "var(--red)" : "var(--green)" }}>● live</span>
          {" "}· /v1/sessions {liveCount > 0 ? "every 3s" : "every 10s"}
        </span>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <Th col="status">Status</Th>
              <Th col="id">Session</Th>
              <Th col="agent_id">Bound</Th>
              <Th col="workspace_id">Workspace</Th>
              <Th col="turn_count" align="right">Turns</Th>
              <Th col="last_worker_id">Worker</Th>
              <Th col="last_turn_at">Last turn</Th>
              <Th col="created_at">Created</Th>
            </tr>
          </thead>
          <tbody>
            {result.loading && items.length === 0 ? (
              <LoadingRows cols={8} />
            ) : result.error && items.length === 0 ? (
              <tr><td colSpan={8} style={{ padding: 20, textAlign: "center" }}>
                <span style={{ color: "var(--red)" }}>{result.error.title || result.error.message}</span>
                {" · "}<a onClick={result.refetch} style={{ cursor: "pointer" }}>Retry</a>
              </td></tr>
            ) : pageItems.length === 0 ? (
              items.length === 0 ? (
                <tr><td colSpan={8}><EmptyState onNewSession={onNewSession} /></td></tr>
              ) : (
                <tr><td colSpan={8} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
                  No sessions match the current filter{textQuery ? ` "${textQuery}"` : ""}.
                  {" · "}<a onClick={() => { setTextQuery(""); setStatusSet(new Set()); setAgentFilter(""); setWorkspaceFilter(""); setGraphFilter(""); }} style={{ cursor: "pointer" }}>Clear filters</a>
                </td></tr>
              )
            ) : pageItems.map((s) => <SessionRow key={s.id} s={s} navigate={navigate} />)}
          </tbody>
        </table>
        <div className="tbl-foot">
          <span className="tabular">
            Showing <strong style={{ color: "var(--text)" }}>{total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1}</strong>–
            <strong style={{ color: "var(--text)" }}>{Math.min(page * PAGE_SIZE, total)}</strong> of{" "}
            <strong style={{ color: "var(--text)" }}>{total}</strong>
            {result.data?.total > items.length && (
              <span className="muted text-sm"> (server reports {result.data.total} total)</span>
            )}
          </span>
          <div className="pager">
            <button disabled={page === 1} onClick={() => setPage(page - 1)}><Icon name="chevron-left" size={12} /></button>
            <span className="muted text-sm tabular" style={{ padding: "0 8px" }}>Page {page} of {totalPages}</span>
            <button disabled={page === totalPages} onClick={() => setPage(page + 1)}><Icon name="chevron-right" size={12} /></button>
          </div>
        </div>
      </div>
    </div>
  );
}

function SessionsHeader({ liveCount, totalCount, onRefresh, onNewSession }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div>
        <div className="crumb">
          <span>Operations</span><span className="sep">/</span><span style={{ color: "var(--text)" }}>Sessions</span>
        </div>
        <h1 className="page-title">Sessions</h1>
        <div className="page-sub tabular">
          <span className="mono" style={{ color: "var(--blue)" }}>● {liveCount}</span> live ·
          {" "}<span className="mono">{totalCount}</span> shown
          <span className="mono" style={{ marginLeft: 4, color: "var(--text-3)" }}>· autorefresh every 3s when live</span>
        </div>
      </div>
      <div className="page-actions">
        <Btn icon="refresh" kind="ghost" onClick={onRefresh}>Refresh</Btn>
        <Btn icon="plus" kind="primary" onClick={onNewSession}>New session</Btn>
      </div>
    </div>
  );
}

function SessionRow({ s, navigate }) {
  const isGraph = s.binding?.kind === "graph";
  const boundLabel = isGraph ? s.binding?.graph_id : s.binding?.agent_id;
  const created = _ageSecOrNull(s.created_at);
  const lastTurn = _ageSecOrNull(s.last_turn_at);
  return (
    <tr onClick={() => navigate("/sessions/" + s.id)} style={{ cursor: "pointer" }}>
      <td><StatusPill status={s.status} /></td>
      <td className="mono">{s.id.length > 24 ? s.id.slice(0, 24) + "…" : s.id}</td>
      <td className="mono">
        {isGraph ? (
          <span><Icon name="graph" size={11} style={{ display: "inline", verticalAlign: "-1px", color: "var(--violet)" }} />{" "}{boundLabel || <span className="muted">—</span>}</span>
        ) : (
          <span><Icon name="agent" size={11} style={{ display: "inline", verticalAlign: "-1px", color: "var(--text-3)" }} />{" "}{boundLabel || <span className="muted">—</span>}</span>
        )}
      </td>
      <td className="mono muted">{(s.workspace_id || "").slice(0, 18)}{s.workspace_id && s.workspace_id.length > 18 ? "…" : ""}</td>
      <td className="mono num tabular">{s.turn_count ?? 0}</td>
      <td className="mono muted">{s.last_worker_id || "—"}</td>
      <td className="mono muted">{lastTurn != null ? relativeTime(lastTurn) : "—"}</td>
      <td className="mono muted">{created != null ? relativeTime(created) : "—"}</td>
    </tr>
  );
}

function LoadingRows({ cols }) {
  return (
    <>
      {Array.from({ length: 6 }).map((_, i) => (
        <tr key={i}>
          {Array.from({ length: cols }).map((__, j) => (
            <td key={j}><span className="skel" style={{ display: "block", height: 12, width: j === 1 ? 140 : j === 0 ? 50 : 80, opacity: 0.4, background: "var(--bg-2)" }} /></td>
          ))}
        </tr>
      ))}
    </>
  );
}

function EmptyState({ onNewSession }) {
  return (
    <div className="empty" style={{ padding: "40px 20px" }}>
      <div className="ico-wrap"><Icon name="zap" size={22} /></div>
      <div className="head">No sessions yet</div>
      <div className="sub">A session runs an agent (or graph) against a workspace for one or more turns.</div>
      <div className="actions">
        <Btn kind="primary" icon="plus" onClick={onNewSession}>New session</Btn>
      </div>
    </div>
  );
}

window.SessionsList = SessionsList;
