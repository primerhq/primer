/* global React, Icon, StatusPill, Btn, relativeTime, Banner */

const SL_STATUS_CHIPS = [
  { s: "created",   color: "var(--text-3)" },
  { s: "running",   color: "var(--blue)" },
  { s: "waiting",   color: "var(--amber)" },
  { s: "paused",    color: "var(--amber)" },
  { s: "ended",     color: "var(--green)" },
  { s: "failed",    color: "var(--red)" },
  { s: "cancelled", color: "var(--text-4)" },
];

const SL_NON_TERMINAL = new Set(["created", "running", "paused", "waiting"]);
const SL_PAGE_SIZE = 12;

function _slAgeSec(iso) {
  if (!iso) return null;
  return (Date.now() - new Date(iso).getTime()) / 1000;
}

function SessionsList({ onOpenSession, onNewSession, demoState, filterPreset }) {
  const { useResource, useRouter, apiFetch } = window.matrixApi;
  const { navigate } = useRouter();

  // Local filter state. Seed status filter from filterPreset prop (set by
  // dashboard "running" tile). Otherwise empty (= show everything).
  const [statusSet, setStatusSet] = React.useState(() => {
    if (filterPreset === "running") return new Set(["running", "paused"]);
    return new Set();
  });
  const [agentFilter, setAgentFilter] = React.useState("");
  const [workspaceFilter, setWorkspaceFilter] = React.useState("");
  const [textQuery, setTextQuery] = React.useState("");
  const [sortBy, setSortBy] = React.useState("created_at");
  const [sortDir, setSortDir] = React.useState("desc");
  const [selected, setSelected] = React.useState(() => new Set());
  const [page, setPage] = React.useState(1);

  // Suspend polling while filter input is focused so an in-progress
  // search doesn't get rug-pulled by a refresh.
  const filterFocused = React.useRef(false);

  const list = useResource(
    "sessions:list",
    (signal) => apiFetch("GET", "/sessions?limit=200", null, { signal }),
    {
      pollMs: 3000,
      pauseWhile: () => filterFocused.current,
    }
  );

  // Combobox sources — cached for the page's lifetime.
  const agents = useResource(
    "sessions-list:agents",
    (s) => apiFetch("GET", "/agents?limit=200", null, { signal: s }),
    {}
  );
  const workspaces = useResource(
    "sessions-list:workspaces",
    (s) => apiFetch("GET", "/workspaces?limit=200", null, { signal: s }),
    {}
  );

  const items = list.data?.items ?? [];

  // Apply UI filters client-side.
  const filtered = React.useMemo(() => {
    let arr = items;
    if (statusSet.size > 0) {
      arr = arr.filter((s) => statusSet.has(s.status));
    }
    if (agentFilter) {
      arr = arr.filter((s) => (s.binding?.agent_id || s.agent_id) === agentFilter);
    }
    if (workspaceFilter) {
      arr = arr.filter((s) => s.workspace_id === workspaceFilter);
    }
    if (textQuery) {
      const q = textQuery.toLowerCase();
      arr = arr.filter((s) =>
        (s.id || "").toLowerCase().includes(q) ||
        ((s.binding?.agent_id || s.agent_id || "")).toLowerCase().includes(q) ||
        ((s.binding?.graph_id || s.graph_id || "")).toLowerCase().includes(q) ||
        ((s.workspace_id || "")).toLowerCase().includes(q)
      );
    }
    return arr;
  }, [items, statusSet, agentFilter, workspaceFilter, textQuery]);

  const sorted = React.useMemo(() => {
    const arr = [...filtered];
    arr.sort((a, b) => {
      let av, bv;
      if (sortBy === "created_at" || sortBy === "last_turn_at") {
        av = a[sortBy] ? new Date(a[sortBy]).getTime() : 0;
        bv = b[sortBy] ? new Date(b[sortBy]).getTime() : 0;
      } else if (sortBy === "agent_id") {
        av = a.binding?.agent_id || a.agent_id || "";
        bv = b.binding?.agent_id || b.agent_id || "";
      } else if (sortBy === "worker_id") {
        av = a.last_worker_id || a.worker_id || "";
        bv = b.last_worker_id || b.worker_id || "";
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
    return arr;
  }, [filtered, sortBy, sortDir]);

  const total = sorted.length;
  const totalPages = Math.max(1, Math.ceil(total / SL_PAGE_SIZE));
  const pageItems = sorted.slice((page - 1) * SL_PAGE_SIZE, page * SL_PAGE_SIZE);

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

  const toggleRow = (id) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const allSelected = pageItems.length > 0 && pageItems.every((s) => selected.has(s.id));

  const openSession = (id) => {
    if (typeof onOpenSession === "function") onOpenSession(id);
    else navigate("/sessions/" + id);
  };

  // ---- Demo overrides (still honoured for tweaks panel) ----
  if (demoState === "loading") return <SLLoadingTable />;
  if (demoState === "error") return (
    <Banner
      kind="error"
      title="Couldn't load sessions"
      detail="GET /v1/sessions failed with 502 — provider-server-error."
      actions={<><Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn></>}
    />
  );
  if (demoState === "empty") return <SLEmptySessions onNewSession={onNewSession} />;

  // ---- Real API states ----
  if (list.error && items.length === 0) {
    return (
      <Banner
        kind="error"
        title={list.error.title || "Couldn't load sessions"}
        detail={list.error.detail || list.error.message}
        requestId={list.error.requestId}
        actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
      />
    );
  }
  if (list.loading && items.length === 0) return <SLLoadingTable />;
  if (items.length === 0) return <SLEmptySessions onNewSession={onNewSession} />;

  return (
    <div className="session-list-layout">
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
          {SL_STATUS_CHIPS.map(({ s, color }) => (
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
            <option key={w.id} value={w.id}>{w.id.slice(0, 14)}{w.id.length > 14 ? "…" : ""}</option>
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
            {pageItems.length === 0 ? (
              <tr><td colSpan={9} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
                No sessions match the current filter{textQuery ? ` "${textQuery}"` : ""}.
                {" · "}<a
                  onClick={() => { setTextQuery(""); setStatusSet(new Set()); setAgentFilter(""); setWorkspaceFilter(""); }}
                  style={{ cursor: "pointer", color: "var(--accent)" }}
                >Clear filters</a>
              </td></tr>
            ) : pageItems.map((s) => {
              const isGraph = (s.binding?.kind || s.binding_kind) === "graph";
              const boundAgent = s.binding?.agent_id || s.agent_id;
              const boundGraph = s.binding?.graph_id || s.graph_id;
              const workerId = s.last_worker_id || s.worker_id;
              const createdSec = _slAgeSec(s.created_at);
              const lastTurnSec = _slAgeSec(s.last_turn_at);
              return (
                <tr
                  key={s.id}
                  className={selected.has(s.id) ? "selected" : ""}
                  onClick={() => openSession(s.id)}
                  style={{ cursor: "pointer" }}
                >
                  <td onClick={(e) => { e.stopPropagation(); toggleRow(s.id); }} style={{ width: 36 }}>
                    <input type="checkbox" checked={selected.has(s.id)} onChange={() => toggleRow(s.id)} />
                  </td>
                  <td><StatusPill status={s.status} /></td>
                  <td className="mono">{(s.id || "").length > 22 ? (s.id.slice(0, 22) + "…") : s.id}</td>
                  <td className="mono">
                    {isGraph ? (
                      <span>
                        <Icon name="graph" size={11} style={{ display: "inline", verticalAlign: "-1px", color: "var(--violet)" }} />{" "}
                        {boundGraph || <span className="muted">—</span>}
                      </span>
                    ) : (
                      <span>
                        <Icon name="agent" size={11} style={{ display: "inline", verticalAlign: "-1px", color: "var(--text-3)" }} />{" "}
                        {boundAgent || <span className="muted">—</span>}
                      </span>
                    )}
                  </td>
                  <td className="mono muted">{(s.workspace_id || "").slice(0, 16)}{(s.workspace_id || "").length > 16 ? "…" : ""}</td>
                  <td className="mono num tabular">{s.turn_count ?? 0}</td>
                  <td className="mono muted">{workerId || "—"}</td>
                  <td className="mono muted">{lastTurnSec != null ? relativeTime(lastTurnSec) : "—"}</td>
                  <td className="mono muted">{createdSec != null ? relativeTime(createdSec) : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className="tbl-foot">
          <span className="tabular">
            Showing <strong style={{ color: "var(--text)" }}>{total === 0 ? 0 : (page - 1) * SL_PAGE_SIZE + 1}</strong>–
            <strong style={{ color: "var(--text)" }}>{Math.min(page * SL_PAGE_SIZE, total)}</strong> of{" "}
            <strong style={{ color: "var(--text)" }}>{total}</strong>
            {list.data?.total > items.length && (
              <span className="muted text-sm"> (server reports {list.data.total} total)</span>
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

function SLLoadingTable() {
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

function SLEmptySessions({ onNewSession }) {
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
