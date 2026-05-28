/* global React, Icon, Btn, Banner, Modal, relativeTime */
// Task 7 (UI reconciliation Phase 2):
// Wires GraphsPage + GraphDetail to the real API. The detail body
// is the full visual graph editor ported wholesale from the pre-swap
// console (commit 75f2326^), restyled to Designer's CSS tokens.
//
// Cache-key convention (per Tasks 2-6):
//   graphs:list, graph-detail:<gid>, graph-status:<gid>, new-graph:agents
//
// Top-level consts are GR_-prefixed to avoid babel-standalone
// global-scope clashes across components.
//
// Notes on host integration:
//   * app.jsx already renders the page header (crumb + h1) for both
//     /graphs and /graphs/:id, so this file does NOT render an
//     additional <h1> or breadcrumb — see lines 609-637 of app.jsx.
//   * GraphsPage receives `onOpen(gid)`; GraphDetail receives
//     `graphId` + `pushToast` directly. Internal sub-navigations
//     (subgraph double-click, list row, breadcrumb-back) go through
//     window.primerApi.useRouter().navigate(path) which uses bare
//     URL paths (the app.jsx wrapper navigate(page, extra) is a
//     different API — paths are the canonical one).

// ============================================================================
// GR_NewGraphModal — seeds a minimal agent→terminal skeleton
// ============================================================================

function GR_NewGraphModal({ onClose, onCreate, pushToast }) {
  const { apiFetch, useResource, useMutation } = window.primerApi;
  const agents = useResource(
    "new-graph:agents",
    (s) => apiFetch("GET", "/agents?limit=200", null, { signal: s }),
    {},
  );

  const [id, setId] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [seedAgentId, setSeedAgentId] = React.useState("");
  const [fieldErrors, setFieldErrors] = React.useState({});

  React.useEffect(() => {
    if (!seedAgentId && agents.data?.items?.length) {
      setSeedAgentId(agents.data.items[0].id);
    }
  }, [agents.data, seedAgentId]);

  const create = useMutation(
    (body) => apiFetch("POST", "/graphs", body),
    {
      invalidates: ["graphs:list"],
      onSuccess: (g) => onCreate(g),
      onError: (err) => {
        if (err.status === 422 && Array.isArray(err.fieldErrors)) {
          const next = {};
          for (const fe of err.fieldErrors) {
            next[(fe.loc || []).join(".")] = fe.msg;
          }
          setFieldErrors(next);
        } else if (err.status === 409) {
          setFieldErrors({ "body.id": err.detail || err.title || "Already exists" });
        } else if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err.title || "Create failed",
            detail: err.detail || err.message,
            requestId: err.requestId,
          });
        }
      },
    },
  );

  const submit = async () => {
    setFieldErrors({});
    // Seed a minimal valid graph: one agent node + one terminal,
    // wired by a static edge. The Graph model validator requires
    // entry_node_id to match a node id and at least one node.
    const body = {
      ...(id ? { id } : {}),
      description: description || "(no description)",
      nodes: [
        { kind: "agent", id: "start", agent_id: seedAgentId },
        { kind: "terminal", id: "end" },
      ],
      edges: [
        { kind: "static", from_node: "start", to_node: "end" },
      ],
      entry_node_id: "start",
    };
    try { await create.mutate(body); } catch (_e) { /* surfaced via onError */ }
  };

  return (
    <Modal
      title="New graph"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn
            kind="primary"
            icon="plus"
            onClick={submit}
            disabled={!seedAgentId || create.loading}
          >
            {create.loading ? "Creating…" : "Create"}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">
          ID <span className="hint">optional — backend assigns if blank</span>
        </label>
        <input
          className="input"
          value={id}
          onChange={(e) => setId(e.target.value)}
          placeholder="auto-generated"
          style={{ width: "100%" }}
        />
        {fieldErrors["body.id"] && (
          <div className="field-help" style={{ color: "var(--red)" }}>
            {fieldErrors["body.id"]}
          </div>
        )}
      </div>
      <div className="field">
        <label className="field-label">Description</label>
        <input
          className="input"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          style={{ width: "100%" }}
        />
        {fieldErrors["body.description"] && (
          <div className="field-help" style={{ color: "var(--red)" }}>
            {fieldErrors["body.description"]}
          </div>
        )}
      </div>
      <div className="field">
        <label className="field-label">
          Seed agent{" "}
          <span className="hint">
            required · the new graph starts with one agent node + one terminal
          </span>
        </label>
        <select
          className="select"
          value={seedAgentId}
          onChange={(e) => setSeedAgentId(e.target.value)}
          style={{ width: "100%" }}
        >
          <option value="">-- pick an agent --</option>
          {(agents.data?.items ?? []).map((a) => (
            <option key={a.id} value={a.id}>{a.id}</option>
          ))}
        </select>
        {(agents.data?.items ?? []).length === 0 && !agents.loading && (
          <div className="field-help" style={{ color: "var(--amber)" }}>
            No agents configured. Create one at{" "}
            <span className="mono">/agents</span> first.
          </div>
        )}
        <div className="field-help">
          Once created, you can bind sessions to this graph — the graph
          executor runs every node in one turn, persisting per-node state
          to the workspace's{" "}
          <span className="mono">.state/graphs/&lt;session_id&gt;/</span>{" "}
          git repo.
        </div>
      </div>
    </Modal>
  );
}

// ============================================================================
// GraphsPage — list, wired to /graphs?limit=200
// ============================================================================

function GraphsPage({ onOpen, pushToast }) {
  const { apiFetch, useResource } = window.primerApi;
  const list = useResource(
    "graphs:list",
    (s) => apiFetch("GET", "/graphs?limit=200", null, { signal: s }),
    {},
  );

  const [textFilter, setTextFilter] = React.useState("");
  const items = list.data?.items ?? [];
  const filtered = items.filter((g) =>
    !textFilter
      || g.id.toLowerCase().includes(textFilter.toLowerCase())
      || (g.description || "").toLowerCase().includes(textFilter.toLowerCase()),
  );

  // Per-row status — batched fetch of /v1/graphs/{id}/status.
  // Triggered when the list payload changes (or on first paint).
  const [perRowStatus, setPerRowStatus] = React.useState({});
  React.useEffect(() => {
    if (items.length === 0) return undefined;
    const ctrl = new AbortController();
    Promise.all(
      items.map((g) =>
        apiFetch("GET", "/graphs/" + encodeURIComponent(g.id) + "/status", null, { signal: ctrl.signal })
          .then((r) => [g.id, r])
          .catch((e) => [g.id, { ok: null, error: e.title || e.message }]),
      ),
    ).then((entries) => setPerRowStatus(Object.fromEntries(entries)));
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list.data]);

  const [createOpen, setCreateOpen] = React.useState(false);

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter graphs…"
            value={textFilter}
            onChange={(e) => setTextFilter(e.target.value)}
          />
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={() => list.refetch()}>Refresh</Btn>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>
            New graph
          </Btn>
        </div>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>ID</th>
              <th>Description</th>
              <th style={{ textAlign: "right" }}>Nodes</th>
              <th style={{ textAlign: "right" }}>Edges</th>
              <th>Entry</th>
              <th style={{ width: 110 }}>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {list.loading && items.length === 0 ? (
              <tr><td colSpan={7} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</td></tr>
            ) : list.error && items.length === 0 ? (
              <tr><td colSpan={7} style={{ padding: 20, textAlign: "center" }}>
                <span style={{ color: "var(--red)" }}>{list.error.title || list.error.message}</span>
                {" · "}<a onClick={() => list.refetch()} style={{ cursor: "pointer" }}>Retry</a>
              </td></tr>
            ) : filtered.length === 0 ? (
              items.length === 0 ? (
                <tr><td colSpan={7}>
                  <div className="empty" style={{ padding: "40px 20px" }}>
                    <div className="ico-wrap"><Icon name="graph" size={22} /></div>
                    <div className="head">No graphs yet</div>
                    <div className="sub">
                      Graphs orchestrate multiple agents through static or
                      conditional edges. Sessions bound to a graph run the whole
                      graph in one turn via the workspace's git-backed state repo.
                    </div>
                    <div className="actions">
                      <Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New graph</Btn>
                    </div>
                  </div>
                </td></tr>
              ) : (
                <tr><td colSpan={7} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>No graphs match.</td></tr>
              )
            ) : filtered.map((g) => {
              const status = perRowStatus[g.id];
              const nodeCount = (g.nodes || []).length;
              const edgeCount = (g.edges || []).length;
              return (
                <tr key={g.id} onClick={() => onOpen(g.id)} style={{ cursor: "pointer" }}>
                  <td className="mono">{g.id}</td>
                  <td className="muted text-sm" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {g.description || <span style={{ color: "var(--text-4)" }}>—</span>}
                  </td>
                  <td className="mono num tabular">{nodeCount}</td>
                  <td className="mono num tabular">{edgeCount}</td>
                  <td className="mono muted text-sm">
                    {g.entry_node_id || <span style={{ color: "var(--text-4)" }}>—</span>}
                  </td>
                  <td>
                    {status == null ? (
                      <span className="muted">…</span>
                    ) : status.ok === true ? (
                      <span className="pill pill-ended"><span className="dot"></span>ok</span>
                    ) : status.ok === false ? (
                      <span className="pill pill-failed"><span className="dot"></span>{(status.issues || []).length} issue{(status.issues || []).length === 1 ? "" : "s"}</span>
                    ) : (
                      <span className="muted" title={status.error}>err</span>
                    )}
                  </td>
                  <td style={{ textAlign: "right", paddingRight: 12 }}>
                    <Icon name="chevron-right" size={12} className="muted" />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <GR_NewGraphModal
          pushToast={pushToast}
          onClose={() => setCreateOpen(false)}
          onCreate={(g) => {
            setCreateOpen(false);
            list.refetch();
            onOpen(g.id);
          }}
        />
      )}
    </div>
  );
}

// ============================================================================
// GraphDetail — Designer shell; body is the full GraphEditor + status panel
// ============================================================================

function GraphDetail({ graphId, pushToast }) {
  const { apiFetch, useResource, useMutation, useRouter } = window.primerApi;
  const { navigate } = useRouter();
  const id = graphId;

  const graph = useResource(
    "graph-detail:" + id,
    (s) => apiFetch("GET", "/graphs/" + encodeURIComponent(id), null, { signal: s }),
    { pollMs: null, deps: [id] },
  );
  const status = useResource(
    "graph-status:" + id,
    (s) => apiFetch("GET", "/graphs/" + encodeURIComponent(id) + "/status", null, { signal: s }),
    { pollMs: 30000, deps: [id] },
  );

  const delMut = useMutation(
    () => apiFetch("DELETE", "/graphs/" + encodeURIComponent(id)),
    {
      invalidates: ["graphs:list"],
      onSuccess: () => {
        if (typeof pushToast === "function") {
          pushToast({ kind: "warning", title: "Graph deleted", detail: id });
        }
        navigate("/graphs");
      },
      onError: (err) => {
        if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: "Delete failed",
            detail: err.detail || err.message,
            requestId: err.requestId,
          });
        }
      },
    },
  );
  const [confirmDelete, setConfirmDelete] = React.useState(false);

  if (graph.loading && !graph.data) {
    return <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>;
  }
  if (graph.error && !graph.data) {
    return (
      <Banner
        kind="error"
        title={graph.error.title || "Couldn't load graph"}
        detail={graph.error.detail || graph.error.message}
        actions={
          <Btn size="sm" icon="chevron-left" onClick={() => navigate("/graphs")}>
            Back to list
          </Btn>
        }
      />
    );
  }

  return (
    <div className="col" style={{ gap: 14 }}>
      <GR_GraphStatusPanel
        id={id}
        status={status}
        onRefresh={() => { graph.refetch(); status.refetch(); }}
        onDelete={() => setConfirmDelete(true)}
      />
      <GR_GraphEditor
        graphId={id}
        loaded={graph.data}
        onSaved={() => { graph.refetch(); status.refetch(); }}
        onRefresh={() => { graph.refetch(); status.refetch(); }}
        pushToast={pushToast}
      />
      {confirmDelete && (
        <Modal
          title="Delete graph?"
          danger
          onClose={() => setConfirmDelete(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setConfirmDelete(false)}>Cancel</Btn>
              <Btn kind="primary" onClick={() => delMut.mutate()} disabled={delMut.loading}>
                {delMut.loading ? "Deleting…" : "Delete"}
              </Btn>
            </>
          }
        >
          <div>
            Delete <span className="mono">{id}</span>? Sessions bound to this graph
            still work as historical records, but a re-DELETE returns 404 (per
            app spec §5 — DELETE is not idempotent).
          </div>
        </Modal>
      )}
    </div>
  );
}

// ============================================================================
// GR_GraphStatusPanel — 30s poll on /graphs/{id}/status
// ============================================================================

function GR_GraphStatusPanel({ id, status, onRefresh, onDelete }) {
  const ok = status.data?.ok;
  const issues = status.data?.issues || [];
  const loading = status.loading && !status.data;
  return (
    <div className="panel" style={{
      background: ok === true
        ? "linear-gradient(90deg, var(--green-dim) 0%, var(--bg-1) 50%)"
        : ok === false
          ? "linear-gradient(90deg, var(--red-dim) 0%, var(--bg-1) 50%)"
          : "var(--bg-1)",
      borderColor: ok === true
        ? "oklch(0.75 0.15 145 / 0.3)"
        : ok === false
          ? "oklch(0.7 0.2 25 / 0.3)"
          : "var(--border)",
    }}>
      <div className="panel-body" style={{ display: "flex", alignItems: "center", gap: 14, padding: "14px 18px" }}>
        <Icon
          name={ok === true ? "check-circle" : ok === false ? "x-circle" : "info"}
          size={20}
          style={{
            color: ok === true
              ? "var(--green)"
              : ok === false
                ? "var(--red)"
                : "var(--text-3)",
          }}
        />
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 600 }}>
            {loading
              ? "Checking references…"
              : ok === true
                ? "All references resolve"
                : ok === false
                  ? `${issues.length} issue${issues.length === 1 ? "" : "s"} found`
                  : "Status unknown"}
          </div>
          <div className="muted text-sm">
            <span className="mono">GET /v1/graphs/{id}/status</span>
            {status.error
              ? <> · <span style={{ color: "var(--red)" }}>{status.error.title || status.error.message}</span></>
              : null}
          </div>
          {ok === false && issues.map((iss, i) => (
            <div key={i} className="muted text-sm mt-2 mono" style={{ color: "var(--red)" }}>{iss}</div>
          ))}
        </div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          {onRefresh && <Btn size="sm" icon="refresh" kind="ghost" onClick={onRefresh}>Refresh</Btn>}
          {onDelete && <Btn size="sm" icon="trash" kind="danger" onClick={onDelete}>Delete</Btn>}
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// GR_GraphEditor — the showpiece (ported from pre-swap)
// ============================================================================

function GR_GraphEditor({ graphId, loaded, onSaved, onRefresh, pushToast }) {
  const { apiFetch, useMutation, useRouter } = window.primerApi;
  const { navigate } = useRouter();

  // Augment server payload with UI-only x/y coordinates (server
  // doesn't store them). Re-applies auto-layout on first load.
  const seed = React.useMemo(() => {
    if (!loaded) return null;
    const base = {
      ...loaded,
      nodes: (loaded.nodes || []).map((n) => ({ ...n })),
      edges: (loaded.edges || []).map((e) => ({ ...e })),
    };
    if (window.primerVendor && window.primerVendor.autoLayout) {
      return window.primerVendor.autoLayout(base);
    }
    return base;
  }, [loaded]);

  const [draft, setDraft] = React.useState(seed);
  const [selectedNodeId, setSelectedNodeId] = React.useState(null);
  const [addEdgeMode, setAddEdgeMode] = React.useState(null);  // null | {fromId?}
  const [dragging, setDragging] = React.useState(null);
  const [showAddMenu, setShowAddMenu] = React.useState(false);
  const canvasRef = React.useRef(null);

  // Sync draft when server payload changes.
  React.useEffect(() => {
    setDraft(seed);
    setSelectedNodeId(null);
    setAddEdgeMode(null);
  }, [seed]);

  // Diff via JSON deep-equal (cheap and good enough for v1).
  // Strip UI-only x/y before comparison so dragging or Auto-layout
  // alone doesn't count as a change. The PUT body strips them too.
  const diffCount = React.useMemo(() => {
    if (!draft || !loaded) return 0;
    const stripped = (d) => ({
      ...d,
      nodes: (d.nodes || []).map(GR_stripCoords),
      edges: (d.edges || []).map((e) => ({ ...e })),
    });
    return JSON.stringify(stripped(draft)) === JSON.stringify(stripped(loaded)) ? 0 : 1;
  }, [draft, loaded]);

  const save = useMutation(
    (body) => apiFetch("PUT", "/graphs/" + encodeURIComponent(graphId), body),
    {
      invalidates: ["graph-detail:" + graphId, "graph-status:" + graphId, "graphs:list"],
      onSuccess: () => {
        if (typeof pushToast === "function") {
          pushToast({ kind: "success", title: "Graph saved", detail: graphId });
        }
        if (onSaved) onSaved();
      },
      onError: (err) => {
        if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err.title || "Save failed",
            detail: err.detail || err.message,
            requestId: err.requestId,
          });
        }
      },
    },
  );

  const onSave = () => {
    if (!draft) return;
    const body = {
      id: draft.id,
      description: draft.description,
      nodes: (draft.nodes || []).map(GR_stripCoords),
      edges: (draft.edges || []).map((e) => ({ ...e })),
      entry_node_id: draft.entry_node_id,
      ...(draft.max_iterations != null ? { max_iterations: draft.max_iterations } : {}),
    };
    save.mutate(body);
  };

  const onDiscard = () => {
    setDraft(seed);
    setSelectedNodeId(null);
    setAddEdgeMode(null);
  };

  const onAutoLayout = () => {
    if (!window.primerVendor || !window.primerVendor.autoLayout) return;
    setDraft((d) => window.primerVendor.autoLayout(d));
  };

  const onAddNode = (kind) => {
    if (!draft) return;
    setShowAddMenu(false);
    const existingIds = new Set((draft.nodes || []).map((n) => n.id));
    let i = 1;
    let newId = `${kind}_${i}`;
    while (existingIds.has(newId)) { i += 1; newId = `${kind}_${i}`; }
    const newNode = kind === "agent"
      ? { kind: "agent", id: newId, agent_id: "" }
      : kind === "graph"
        ? { kind: "graph", id: newId, graph_id: "" }
        : { kind: "terminal", id: newId };
    newNode.x = 60;
    newNode.y = 60;
    setDraft((d) => ({ ...d, nodes: [...(d.nodes || []), newNode] }));
    setSelectedNodeId(newId);
  };

  const onNodeClick = (nodeId) => {
    if (addEdgeMode) {
      if (!addEdgeMode.fromId) {
        setAddEdgeMode({ fromId: nodeId });
      } else if (addEdgeMode.fromId !== nodeId) {
        const newEdge = { kind: "static", from_node: addEdgeMode.fromId, to_node: nodeId };
        setDraft((d) => ({ ...d, edges: [...(d.edges || []), newEdge] }));
        setAddEdgeMode(null);
      }
      return;
    }
    setSelectedNodeId(nodeId);
  };

  const onNodeDoubleClick = (nodeId) => {
    if (!draft) return;
    const node = draft.nodes.find((n) => n.id === nodeId);
    if (node && node.kind === "graph" && node.graph_id) {
      navigate("/graphs/" + node.graph_id);
    }
  };

  // ---- Drag handling
  const onNodeMouseDown = (e, nodeId) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    if (addEdgeMode) return;
    if (!canvasRef.current) return;
    setSelectedNodeId(nodeId);
    const node = draft.nodes.find((n) => n.id === nodeId);
    if (!node) return;
    const rect = canvasRef.current.getBoundingClientRect();
    setDragging({
      id: nodeId,
      dx: e.clientX - rect.left - (node.x || 0),
      dy: e.clientY - rect.top - (node.y || 0),
    });
  };

  React.useEffect(() => {
    if (!dragging) return;
    const move = (e) => {
      if (!canvasRef.current) return;
      const rect = canvasRef.current.getBoundingClientRect();
      // Snap to 8px grid.
      const rawX = e.clientX - rect.left - dragging.dx;
      const rawY = e.clientY - rect.top - dragging.dy;
      const x = Math.max(0, Math.round(rawX / 8) * 8);
      const y = Math.max(0, Math.round(rawY / 8) * 8);
      setDraft((d) => ({
        ...d,
        nodes: d.nodes.map((n) => n.id === dragging.id ? { ...n, x, y } : n),
      }));
    };
    const up = () => setDragging(null);
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, [dragging]);

  if (!draft) return null;
  const selected = draft.nodes.find((n) => n.id === selectedNodeId);

  return (
    <div className="panel" style={{ overflow: "hidden" }}>
      {/* Toolbar */}
      <div
        className="panel-h"
        style={{
          padding: "8px 12px",
          display: "flex",
          alignItems: "center",
          gap: 6,
          position: "relative",
        }}
      >
        <div style={{ position: "relative" }}>
          <Btn size="sm" kind="ghost" icon="plus" onClick={() => setShowAddMenu((v) => !v)}>
            Add node
          </Btn>
          {showAddMenu && (
            <div
              className="dropdown"
              style={{
                position: "absolute",
                top: "100%",
                left: 0,
                marginTop: 4,
                background: "var(--bg-1)",
                border: "1px solid var(--border)",
                borderRadius: 6,
                padding: 4,
                zIndex: 10,
                minWidth: 140,
                boxShadow: "0 4px 12px rgba(0, 0, 0, 0.2)",
              }}
            >
              <a className="dd-item" onClick={() => onAddNode("agent")} style={GR_DD_ITEM_STYLE}>Agent</a>
              <a className="dd-item" onClick={() => onAddNode("graph")} style={GR_DD_ITEM_STYLE}>Subgraph</a>
              <a className="dd-item" onClick={() => onAddNode("terminal")} style={GR_DD_ITEM_STYLE}>Terminal</a>
            </div>
          )}
        </div>
        <Btn
          size="sm"
          kind={addEdgeMode ? "primary" : "ghost"}
          icon="zap"
          onClick={() => setAddEdgeMode(addEdgeMode ? null : {})}
        >
          {addEdgeMode
            ? (addEdgeMode.fromId ? `Pick target for ${addEdgeMode.fromId}…` : "Pick source…")
            : "Add edge"}
        </Btn>
        <Btn size="sm" kind="ghost" icon="refresh" onClick={onAutoLayout}>Auto-layout</Btn>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6 }}>
          {diffCount > 0 && <span className="muted text-sm tabular">· unsaved changes</span>}
          <Btn size="sm" kind="ghost" onClick={onDiscard} disabled={diffCount === 0}>Discard</Btn>
          <Btn
            size="sm"
            kind="primary"
            icon="check"
            disabled={diffCount === 0 || save.loading}
            onClick={onSave}
          >
            {save.loading ? "Saving…" : "Save"}
          </Btn>
        </div>
      </div>

      {/* Editor + side panel */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 260px" }}>
        <GR_Canvas
          ref={canvasRef}
          draft={draft}
          selectedNodeId={selectedNodeId}
          addEdgeMode={addEdgeMode}
          onNodeClick={onNodeClick}
          onNodeDoubleClick={onNodeDoubleClick}
          onNodeMouseDown={onNodeMouseDown}
          onBackgroundClick={() => {
            setSelectedNodeId(null);
            if (addEdgeMode) setAddEdgeMode(null);
          }}
        />
        <GR_SidePanel
          draft={draft}
          selected={selected}
          onUpdateNode={(patch) => {
            setDraft((d) => ({
              ...d,
              nodes: d.nodes.map((n) => n.id === selectedNodeId ? { ...n, ...patch } : n),
            }));
            if (patch.id && patch.id !== selectedNodeId) {
              // Renaming a node: rewrite edges to keep them valid.
              setDraft((d) => ({
                ...d,
                edges: d.edges.map((e) => ({
                  ...e,
                  from_node: e.from_node === selectedNodeId ? patch.id : e.from_node,
                  to_node: e.to_node === selectedNodeId ? patch.id : e.to_node,
                })),
                entry_node_id: d.entry_node_id === selectedNodeId ? patch.id : d.entry_node_id,
              }));
              setSelectedNodeId(patch.id);
            }
          }}
          onDeleteNode={() => {
            const idToDel = selectedNodeId;
            setDraft((d) => ({
              ...d,
              nodes: d.nodes.filter((n) => n.id !== idToDel),
              edges: d.edges.filter((e) => e.from_node !== idToDel && e.to_node !== idToDel),
            }));
            setSelectedNodeId(null);
          }}
          onSetEntry={() => {
            setDraft((d) => ({ ...d, entry_node_id: selectedNodeId }));
          }}
          onDeleteEdgeAt={(idx) => {
            setDraft((d) => ({ ...d, edges: d.edges.filter((_, i) => i !== idx) }));
          }}
          onNavigateSubgraph={(gid) => navigate("/graphs/" + gid)}
        />
      </div>
    </div>
  );
}

const GR_DD_ITEM_STYLE = {
  display: "block",
  padding: "6px 10px",
  cursor: "pointer",
  fontSize: 13,
  color: "var(--text)",
  textDecoration: "none",
};

// ----------------------------------------------------------------------------
// GR_Canvas (forwardRef so the editor can attach its ref)
// ----------------------------------------------------------------------------

const GR_NODE_SIZE = {
  agent: { w: 150, h: 56 },
  graph: { w: 150, h: 56 },
  terminal: { w: 22, h: 22 },
};

const GR_Canvas = React.forwardRef(function GR_Canvas(
  { draft, selectedNodeId, addEdgeMode, onNodeClick, onNodeDoubleClick, onNodeMouseDown, onBackgroundClick },
  ref,
) {
  // Compute canvas extent (auto-grow for far-flung nodes).
  let maxX = 600;
  let maxY = 380;
  for (const n of draft.nodes) {
    const sz = GR_NODE_SIZE[n.kind] || GR_NODE_SIZE.agent;
    if ((n.x || 0) + sz.w + 40 > maxX) maxX = (n.x || 0) + sz.w + 40;
    if ((n.y || 0) + sz.h + 40 > maxY) maxY = (n.y || 0) + sz.h + 40;
  }

  return (
    <div
      ref={ref}
      onClick={onBackgroundClick}
      style={{
        position: "relative",
        minHeight: 500,
        height: 500,
        overflow: "auto",
        background: "var(--bg)",
        backgroundImage: "radial-gradient(circle, var(--border) 1px, transparent 1px)",
        backgroundSize: "20px 20px",
        userSelect: "none",
        cursor: addEdgeMode ? "crosshair" : "default",
      }}
    >
      <div style={{ position: "relative", width: maxX, height: maxY }}>
        <svg
          style={{ position: "absolute", inset: 0, pointerEvents: "none" }}
          width={maxX}
          height={maxY}
        >
          <defs>
            <marker id="arrow-static" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto" markerUnits="strokeWidth">
              <path d="M0,0 L8,4 L0,8 z" fill="var(--text-3)" />
            </marker>
            <marker id="arrow-cond" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto" markerUnits="strokeWidth">
              <path d="M0,0 L8,4 L0,8 z" fill="var(--accent)" />
            </marker>
          </defs>
          {(draft.edges || []).map((e, i) => (
            <GR_EdgePath key={i} edge={e} nodes={draft.nodes} />
          ))}
        </svg>

        {draft.nodes.map((n) => (
          <GR_NodeBox
            key={n.id}
            node={n}
            selected={selectedNodeId === n.id}
            entry={draft.entry_node_id === n.id}
            edgePicking={!!addEdgeMode}
            edgePickStage={addEdgeMode && addEdgeMode.fromId === n.id ? "from" : null}
            onClick={(ev) => { ev.stopPropagation(); onNodeClick(n.id); }}
            onDoubleClick={(ev) => { ev.stopPropagation(); onNodeDoubleClick(n.id); }}
            onMouseDown={(ev) => onNodeMouseDown(ev, n.id)}
          />
        ))}

        <div style={{
          position: "absolute",
          bottom: 8,
          left: 8,
          fontSize: 10.5,
          color: "var(--text-3)",
          fontFamily: "IBM Plex Mono",
          display: "flex",
          gap: 14,
          pointerEvents: "none",
        }}>
          <span>
            <span style={{ display: "inline-block", width: 14, height: 2, background: "var(--text-3)", verticalAlign: "middle", marginRight: 4 }}></span>
            static
          </span>
          <span>
            <span style={{ display: "inline-block", width: 14, height: 0, borderTop: "1.5px dashed var(--accent)", verticalAlign: "middle", marginRight: 4 }}></span>
            conditional
          </span>
          {addEdgeMode && (
            <span style={{ color: "var(--accent)" }}>
              {addEdgeMode.fromId ? "Click target node…" : "Click source node…"}
            </span>
          )}
        </div>
      </div>
    </div>
  );
});

// ----------------------------------------------------------------------------
// GR_NodeBox — agent / graph / terminal nodes
// ----------------------------------------------------------------------------

function GR_NodeBox({ node, selected, entry, edgePicking, edgePickStage, onClick, onDoubleClick, onMouseDown }) {
  const isTerminal = node.kind === "terminal";
  const isGraph = node.kind === "graph";

  const baseStyle = {
    position: "absolute",
    left: node.x || 0,
    top: node.y || 0,
    cursor: edgePicking ? "crosshair" : "grab",
  };

  if (isTerminal) {
    return (
      <div onMouseDown={onMouseDown} onClick={onClick} onDoubleClick={onDoubleClick} style={baseStyle}>
        <div style={{
          width: 22,
          height: 22,
          borderRadius: "50%",
          background: selected ? "var(--accent)" : "var(--text)",
          border: edgePickStage === "from"
            ? "2px dashed var(--accent)"
            : selected
              ? "2px solid var(--accent)"
              : "2px solid var(--border-strong)",
          boxShadow: selected ? "0 0 0 4px var(--accent-dim)" : undefined,
        }} />
        <div className="mono text-sm" style={{
          color: selected ? "var(--accent)" : "var(--text-2)",
          whiteSpace: "nowrap",
          marginTop: 2,
          fontSize: 11,
        }}>
          {entry ? "▶ " : ""}{node.id}
        </div>
      </div>
    );
  }

  return (
    <div
      onMouseDown={onMouseDown}
      onClick={onClick}
      onDoubleClick={onDoubleClick}
      style={{
        ...baseStyle,
        width: GR_NODE_SIZE[node.kind].w,
        height: GR_NODE_SIZE[node.kind].h,
        borderRadius: 8,
        background: isGraph ? "transparent" : "var(--bg-1)",
        border: edgePickStage === "from"
          ? "2px dashed var(--accent)"
          : selected
            ? "2px solid var(--accent)"
            : `${isGraph ? "1.5px dashed" : "1.5px solid"} var(--border-strong)`,
        boxShadow: selected ? "0 0 0 4px var(--accent-dim)" : "0 1px 0 rgba(0, 0, 0, 0.2)",
        padding: "6px 10px",
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11 }}>
        <Icon
          name={isGraph ? "graph" : "agent"}
          size={11}
          style={{ color: isGraph ? "var(--violet)" : "var(--accent)" }}
        />
        <span className="mono" style={{ fontSize: 11, fontWeight: 500 }}>
          {entry ? "▶ " : ""}{node.id}
        </span>
        <span className="muted" style={{ fontSize: 9.5, marginLeft: "auto" }}>
          {node.kind}
        </span>
      </div>
      <div className="mono muted text-sm" style={{
        fontSize: 10.5,
        marginTop: 3,
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
      }}>
        {isGraph
          ? (node.graph_id || <span style={{ color: "var(--red)" }}>(graph_id not set)</span>)
          : (node.agent_id || <span style={{ color: "var(--red)" }}>(agent_id not set)</span>)}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------------
// GR_EdgePath — per-edge SVG router (static / conditional)
// ----------------------------------------------------------------------------

function GR_EdgePath({ edge, nodes }) {
  // For conditional edges with a json_path router, draw one curve per
  // branch + an optional default-to curve. For static edges and
  // callable-router conditionals, draw one curve to the single known
  // target (callable routers expose no static target).
  const from = nodes.find((n) => n.id === edge.from_node);
  if (!from) return null;
  const fromSize = GR_NODE_SIZE[from.kind] || GR_NODE_SIZE.agent;
  const fx = (from.x || 0) + fromSize.w;
  const fy = (from.y || 0) + fromSize.h / 2;

  if (edge.kind === "static") {
    const to = nodes.find((n) => n.id === edge.to_node);
    if (!to) return null;
    return <GR_SingleEdge fx={fx} fy={fy} to={to} dashed={false} label={null} />;
  }

  // conditional
  const router = edge.router || {};
  if (router.kind === "json_path") {
    const out = [];
    for (let i = 0; i < (router.branches || []).length; i += 1) {
      const br = router.branches[i];
      const to = nodes.find((n) => n.id === br.to_node);
      if (!to) continue;
      const label = Object.entries(br.when || {}).map(([k, v]) => `${k}=${v}`).join(" ∧ ");
      out.push(<GR_SingleEdge key={"b" + i} fx={fx} fy={fy} to={to} dashed label={label} />);
    }
    if (router.default_to) {
      const to = nodes.find((n) => n.id === router.default_to);
      if (to) {
        out.push(<GR_SingleEdge key="def" fx={fx} fy={fy} to={to} dashed label="(default)" />);
      }
    }
    return <>{out}</>;
  }

  if (router.kind === "callable") {
    // Callable routers have no static target — draw a stub arrow.
    return (
      <text x={fx + 12} y={fy} fontSize="10" fontFamily="IBM Plex Mono" fill="var(--accent)">
        callable:{router.callable_id || "?"}
      </text>
    );
  }
  return null;
}

function GR_SingleEdge({ fx, fy, to, dashed, label }) {
  const toSize = GR_NODE_SIZE[to.kind] || GR_NODE_SIZE.agent;
  const tx = to.x || 0;
  const ty = (to.y || 0) + toSize.h / 2;
  const mx = (fx + tx) / 2;
  const path = `M ${fx} ${fy} C ${mx} ${fy}, ${mx} ${ty}, ${tx} ${ty}`;
  const stroke = dashed ? "var(--accent)" : "var(--text-3)";
  const marker = dashed ? "url(#arrow-cond)" : "url(#arrow-static)";
  return (
    <g>
      <path
        d={path}
        stroke={stroke}
        strokeWidth="1.6"
        fill="none"
        strokeDasharray={dashed ? "5 4" : "0"}
        markerEnd={marker}
      />
      {label && (
        <text
          x={mx}
          y={(fy + ty) / 2 - 6}
          textAnchor="middle"
          fontSize="10"
          fontFamily="IBM Plex Mono"
          fill={stroke}
        >
          {label.length > 30 ? label.slice(0, 28) + "…" : label}
        </text>
      )}
    </g>
  );
}

// ----------------------------------------------------------------------------
// Side panel
// ----------------------------------------------------------------------------

function GR_SidePanel({ draft, selected, onUpdateNode, onDeleteNode, onSetEntry, onDeleteEdgeAt, onNavigateSubgraph }) {
  return (
    <div style={{ borderLeft: "1px solid var(--border)", padding: 14, fontSize: 12.5, overflow: "auto", maxHeight: 500 }}>
      {selected ? (
        <GR_SelectedNodeForm
          node={selected}
          isEntry={draft.entry_node_id === selected.id}
          edges={draft.edges || []}
          onUpdateNode={onUpdateNode}
          onDeleteNode={onDeleteNode}
          onSetEntry={onSetEntry}
          onDeleteEdgeAt={onDeleteEdgeAt}
          onNavigateSubgraph={onNavigateSubgraph}
        />
      ) : (
        <GR_GraphStatsBlock draft={draft} />
      )}
    </div>
  );
}

function GR_GraphStatsBlock({ draft }) {
  const nodeIds = new Set((draft.nodes || []).map((n) => n.id));
  const dangling = [];
  for (const e of (draft.edges || [])) {
    if (!nodeIds.has(e.from_node)) dangling.push(`edge.from_node = ${e.from_node}`);
    if (e.kind === "static" && !nodeIds.has(e.to_node)) {
      dangling.push(`edge.to_node = ${e.to_node}`);
    }
    if (e.kind === "conditional") {
      const r = e.router || {};
      if (r.kind === "json_path") {
        for (const br of (r.branches || [])) {
          if (!nodeIds.has(br.to_node)) {
            dangling.push(`branch.to_node = ${br.to_node}`);
          }
        }
        if (r.default_to && !nodeIds.has(r.default_to)) {
          dangling.push(`default_to = ${r.default_to}`);
        }
      }
    }
  }
  const entryOk = draft.entry_node_id && nodeIds.has(draft.entry_node_id);
  return (
    <div className="col" style={{ gap: 8 }}>
      <div className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>
        graph
      </div>
      <div className="mono" style={{ fontWeight: 600, fontSize: 14 }}>{draft.id}</div>
      <div className="muted text-sm">{draft.description}</div>
      <div className="mt-3 muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>
        stats
      </div>
      <dl className="kv" style={{ gridTemplateColumns: "110px 1fr", gap: "4px 12px" }}>
        <dt>nodes</dt><dd>{(draft.nodes || []).length}</dd>
        <dt>edges</dt><dd>{(draft.edges || []).length}</dd>
        <dt>entry</dt>
        <dd className="mono">
          {entryOk
            ? draft.entry_node_id
            : <span style={{ color: "var(--red)" }}>{draft.entry_node_id || "(unset)"}</span>}
        </dd>
        <dt>terminals</dt><dd>{(draft.nodes || []).filter((n) => n.kind === "terminal").length}</dd>
        <dt>subgraphs</dt><dd>{(draft.nodes || []).filter((n) => n.kind === "graph").length}</dd>
        {draft.max_iterations != null && (
          <><dt>max_iterations</dt><dd>{draft.max_iterations}</dd></>
        )}
      </dl>
      {dangling.length > 0 && (
        <div className="banner banner-warning mt-3" style={{ padding: "8px 10px", fontSize: 11.5 }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>
            {dangling.length} dangling reference{dangling.length === 1 ? "" : "s"}
          </div>
          {dangling.map((d, i) => (
            <div key={i} className="mono">{d}</div>
          ))}
        </div>
      )}
      <div className="muted text-sm mt-3">
        Click a node to inspect or edit. Drag to reposition (8px grid).
        Double-click a subgraph node to jump in.
      </div>
    </div>
  );
}

function GR_SelectedNodeForm({ node, isEntry, edges, onUpdateNode, onDeleteNode, onSetEntry, onDeleteEdgeAt, onNavigateSubgraph }) {
  const edgesIn = edges.map((e, i) => ({ e, i })).filter(({ e }) => {
    if (e.kind === "static") return e.to_node === node.id;
    if (e.kind === "conditional") {
      const r = e.router || {};
      if (r.kind === "json_path") {
        return (r.branches || []).some((b) => b.to_node === node.id) || r.default_to === node.id;
      }
    }
    return false;
  });
  const edgesOut = edges.map((e, i) => ({ e, i })).filter(({ e }) => e.from_node === node.id);

  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>
        {node.kind} node
      </div>
      <div className="field">
        <label className="field-label">id</label>
        <input
          className="input"
          value={node.id}
          onChange={(e) => onUpdateNode({ id: e.target.value })}
          style={{ width: "100%" }}
        />
      </div>
      {node.kind === "agent" && (
        <div className="field">
          <label className="field-label">agent_id</label>
          <input
            className="input"
            value={node.agent_id || ""}
            onChange={(e) => onUpdateNode({ agent_id: e.target.value })}
            placeholder="(none)"
            style={{ width: "100%" }}
          />
        </div>
      )}
      {node.kind === "graph" && (
        <div className="field">
          <label className="field-label">
            graph_id <span className="hint">double-click node to navigate</span>
          </label>
          <div style={{ display: "flex", gap: 4 }}>
            <input
              className="input"
              value={node.graph_id || ""}
              onChange={(e) => onUpdateNode({ graph_id: e.target.value })}
              placeholder="(none)"
              style={{ flex: 1 }}
            />
            {node.graph_id && (
              <Btn size="sm" icon="chevron-right" kind="ghost" onClick={() => onNavigateSubgraph(node.graph_id)}>
                Open
              </Btn>
            )}
          </div>
        </div>
      )}
      <div className="muted text-sm">x: {Math.round(node.x || 0)} · y: {Math.round(node.y || 0)}</div>
      <div className="mt-2 muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>
        edges in ({edgesIn.length})
      </div>
      {edgesIn.length === 0 && <div className="muted text-sm">— none —</div>}
      {edgesIn.map(({ e, i }) => (
        <div key={"in-" + i} className="mono text-sm" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ flex: 1 }}>
            {e.from_node} → {node.id} <span className="muted">({e.kind})</span>
          </span>
          <a onClick={() => onDeleteEdgeAt(i)} style={{ cursor: "pointer", color: "var(--red)" }} title="Delete edge">×</a>
        </div>
      ))}
      <div className="mt-2 muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>
        edges out ({edgesOut.length})
      </div>
      {edgesOut.length === 0 && <div className="muted text-sm">— none —</div>}
      {edgesOut.map(({ e, i }) => (
        <GR_EdgeOutRow key={"out-" + i} edge={e} idx={i} onDelete={() => onDeleteEdgeAt(i)} />
      ))}
      <div className="mt-3" style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {!isEntry && <Btn size="sm" kind="ghost" icon="play" onClick={onSetEntry}>Set as entry</Btn>}
        {isEntry && <span className="muted text-sm">(entry node)</span>}
        <Btn size="sm" kind="danger" icon="trash" onClick={onDeleteNode}>Delete node</Btn>
      </div>
      <div className="muted text-sm mt-2">
        Edits stage locally; click Save to PUT-replace the whole graph.
      </div>
    </div>
  );
}

function GR_EdgeOutRow({ edge, idx, onDelete }) {
  if (edge.kind === "static") {
    return (
      <div className="mono text-sm" style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{ flex: 1 }}>
          → {edge.to_node} <span className="muted">(static)</span>
        </span>
        <a onClick={onDelete} style={{ cursor: "pointer", color: "var(--red)" }} title="Delete edge">×</a>
      </div>
    );
  }
  const r = edge.router || {};
  return (
    <div className="mono text-sm" style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{ flex: 1 }}>
          → <span className="muted">conditional · {r.kind || "?"}</span>
        </span>
        <a onClick={onDelete} style={{ cursor: "pointer", color: "var(--red)" }} title="Delete edge">×</a>
      </div>
      {r.kind === "json_path" && (r.branches || []).map((br, i) => (
        <div key={i} className="muted text-sm" style={{ paddingLeft: 12 }}>
          → {br.to_node} when {Object.entries(br.when || {}).map(([k, v]) => `${k}=${v}`).join(" ∧ ")}
        </div>
      ))}
      {r.kind === "json_path" && r.default_to && (
        <div className="muted text-sm" style={{ paddingLeft: 12 }}>
          → {r.default_to} (default)
        </div>
      )}
      {r.kind === "callable" && (
        <div className="muted text-sm" style={{ paddingLeft: 12 }}>
          callable: {r.callable_id}
        </div>
      )}
    </div>
  );
}

// Helper: strip UI-only x/y before PUTting back to the server.
function GR_stripCoords(node) {
  const { x, y, ...rest } = node;
  return rest;
}

// Export to global scope. Designer's app.jsx looks up GraphsPage +
// GraphDetail off window.
Object.assign(window, { GraphsPage, GraphDetail, GR_NewGraphModal });
