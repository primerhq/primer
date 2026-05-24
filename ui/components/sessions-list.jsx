/* global React, Icon, StatusPill, Btn, relativeTime, Banner */

function SessionsList({ sessions, onOpenSession, filterPreset, onNewSession, demoState }) {
  const [statusFilter, setStatusFilter] = React.useState(() => {
    if (filterPreset === "running") return new Set(["running", "paused"]);
    return new Set();
  });
  const [agentFilter, setAgentFilter] = React.useState("");
  const [workspaceFilter, setWorkspaceFilter] = React.useState("");
  const [query, setQuery] = React.useState("");
  const [sortBy, setSortBy] = React.useState("created_at");
  const [sortDir, setSortDir] = React.useState("desc");
  const [selected, setSelected] = React.useState(new Set());
  const [page, setPage] = React.useState(1);
  const PAGE_SIZE = 12;

  const toggleStatus = (s) => {
    const next = new Set(statusFilter);
    if (next.has(s)) next.delete(s); else next.add(s);
    setStatusFilter(next);
    setPage(1);
  };

  const allFiltered = React.useMemo(() => {
    let arr = [...sessions];
    if (statusFilter.size > 0) {
      arr = arr.filter((s) => {
        // "waiting" filter matches parked sessions specifically
        if (statusFilter.has("waiting") && s.parked_status === "parked") return true;
        return statusFilter.has(s.status);
      });
    }
    if (agentFilter) arr = arr.filter((s) => s.agent_id === agentFilter);
    if (workspaceFilter) arr = arr.filter((s) => s.workspace_id === workspaceFilter);
    if (query) {
      const q = query.toLowerCase();
      arr = arr.filter((s) => s.id.toLowerCase().includes(q) || (s.agent_id && s.agent_id.toLowerCase().includes(q)) || (s.workspace_id && s.workspace_id.toLowerCase().includes(q)));
    }
    arr.sort((a, b) => {
      let av = a[sortBy], bv = b[sortBy];
      if (av instanceof Date) av = av.getTime();
      if (bv instanceof Date) bv = bv.getTime();
      if (av == null) av = 0;
      if (bv == null) bv = 0;
      if (av < bv) return sortDir === "asc" ? -1 : 1;
      if (av > bv) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
    return arr;
  }, [sessions, statusFilter, agentFilter, workspaceFilter, query, sortBy, sortDir]);

  const total = allFiltered.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const pageItems = allFiltered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const setSort = (col) => {
    if (sortBy === col) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortBy(col); setSortDir("desc"); }
  };

  const Th = ({ col, children, align }) => (
    <th
      className={sortBy === col ? "sorted" : ""}
      onClick={() => setSort(col)}
      style={{ textAlign: align || "left" }}
    >
      {children}
      <span className="sort-arrow">{sortBy === col ? (sortDir === "asc" ? "↑" : "↓") : "↕"}</span>
    </th>
  );

  const toggleRow = (id) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  };
  const allSelected = pageItems.length > 0 && pageItems.every((s) => selected.has(s.id));

  const liveRunning = sessions.filter((s) => s.status === "running" || s.status === "paused").length;

  // Demo: loading / error / empty states from tweaks
  if (demoState === "loading") return <LoadingTable />;
  if (demoState === "error") return (
    <Banner
      kind="error"
      title="Couldn't load sessions"
      detail="GET /v1/sessions failed with 502 — provider-server-error. The scheduler is reachable but session storage is currently unreachable."
      actions={<><Btn size="sm" icon="refresh">Retry</Btn><Btn size="sm" kind="ghost" icon="copy">Copy request-id</Btn></>}
    />
  );
  if (demoState === "empty") return <EmptySessions onNewSession={onNewSession} />;

  return (
    <div className="session-list-layout">
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter id, agent, workspace…"
            value={query}
            onChange={(e) => { setQuery(e.target.value); setPage(1); }}
          />
        </div>
        <div className="sep-v" />
        <div className="chip-group" title="filter by status">
          {[
            { s: "created", color: "var(--text-3)" },
            { s: "running", color: "var(--blue)" },
            { s: "waiting", color: "var(--amber)" },
            { s: "paused", color: "var(--amber)" },
            { s: "ended", color: "var(--green)" },
            { s: "failed", color: "var(--red)" },
            { s: "cancelled", color: "var(--text-4)" },
          ].map(({ s, color }) => (
            <span
              key={s}
              className={`chip-dot ${statusFilter.has(s) ? "active" : ""}`}
              onClick={() => toggleStatus(s)}
              title={s}
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
          {window.MOCK.AGENTS.map((a) => (
            <option key={a.id} value={a.id}>{a.id}</option>
          ))}
        </select>
        <select
          className="select"
          value={workspaceFilter}
          onChange={(e) => { setWorkspaceFilter(e.target.value); setPage(1); }}
        >
          <option value="">workspace</option>
          {window.MOCK.WORKSPACES.map((w) => (
            <option key={w} value={w}>{w.slice(0, 14)}…</option>
          ))}
        </select>
        {selected.size > 0 && (
          <Btn size="sm" kind="danger" icon="trash">Delete {selected.size}</Btn>
        )}
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th style={{ width: 36 }}>
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={() => {
                    if (allSelected) setSelected(new Set());
                    else setSelected(new Set(pageItems.map((s) => s.id)));
                  }}
                />
              </th>
              <Th col="status">Status</Th>
              <Th col="id">Session</Th>
              <Th col="agent_id">Bound</Th>
              <Th col="workspace_id">Workspace</Th>
              <Th col="turn_count" align="right">Turns</Th>
              <Th col="worker_id">Worker</Th>
              <Th col="last_turn_at">Last turn</Th>
              <Th col="created_at">Created</Th>
            </tr>
          </thead>
          <tbody>
            {pageItems.map((s) => (
              <tr
                key={s.id}
                className={selected.has(s.id) ? "selected" : ""}
                onClick={() => onOpenSession(s.id)}
              >
                <td onClick={(e) => { e.stopPropagation(); toggleRow(s.id); }} style={{ width: 36 }}>
                  <input type="checkbox" checked={selected.has(s.id)} onChange={() => toggleRow(s.id)} />
                </td>
                <td><StatusPill status={s.status} parked={s.parked_status === "parked" ? s.parked_state.yielded.tool_name : null} /></td>
                <td className="mono">{s.id.slice(0, 18)}<span className="muted">…</span></td>
                <td className="mono">
                  {s.binding_kind === "graph" ? (
                    <span>
                      <Icon name="graph" size={11} style={{ display: "inline", verticalAlign: "-1px", color: "var(--violet)" }} />{" "}
                      {s.graph_id}
                    </span>
                  ) : (
                    <span>
                      <Icon name="agent" size={11} style={{ display: "inline", verticalAlign: "-1px", color: "var(--text-3)" }} />{" "}
                      {s.agent_id || <span className="muted">—</span>}
                    </span>
                  )}
                </td>
                <td className="mono muted">{s.workspace_id.slice(0, 16)}…</td>
                <td className="mono num tabular">{s.turn_count}</td>
                <td className="mono muted">{s.worker_id || "—"}</td>
                <td className="mono muted">
                  {s.last_turn_at ? relativeTime((Date.now() - s.last_turn_at.getTime()) / 1000) : "—"}
                </td>
                <td className="mono muted">{relativeTime((Date.now() - s.created_at.getTime()) / 1000)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="tbl-foot">
          <span className="tabular">
            Showing <strong style={{ color: "var(--text)" }}>{(page - 1) * PAGE_SIZE + 1}</strong>–
            <strong style={{ color: "var(--text)" }}>{Math.min(page * PAGE_SIZE, total)}</strong> of{" "}
            <strong style={{ color: "var(--text)" }}>{total}</strong>
            <span style={{ marginLeft: 12 }}>· <a style={{ color: "var(--accent)", cursor: "pointer" }}>Switch to cursor mode</a></span>
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

function LoadingTable() {
  return (
    <div className="tbl-wrap">
      <table className="tbl">
        <thead>
          <tr>
            <th>Status</th><th>Session</th><th>Bound</th><th>Workspace</th><th>Turns</th><th>Worker</th><th>Last turn</th><th>Created</th>
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: 8 }).map((_, i) => (
            <tr key={i}>
              {Array.from({ length: 8 }).map((__, j) => (
                <td key={j}><span className="skel" style={{ display: "block", height: 12, width: j === 0 ? 60 : j === 1 ? 140 : j === 2 ? 110 : 80 }} /></td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EmptySessions({ onNewSession }) {
  return (
    <div className="panel">
      <div className="empty">
        <div className="ico-wrap"><Icon name="zap" size={22} /></div>
        <div className="head">No sessions yet</div>
        <div className="sub">A session runs an agent (or graph) against a workspace for one or more turns. Create one to see live status, turns, and tool calls.</div>
        <div className="actions">
          <Btn kind="primary" icon="plus" onClick={onNewSession}>New session</Btn>
          <Btn kind="ghost" icon="external">Read the docs</Btn>
        </div>
      </div>
    </div>
  );
}

window.SessionsList = SessionsList;
