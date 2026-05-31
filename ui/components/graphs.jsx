/* global React, Icon, Btn, Banner, Modal, CardList, Card, Fab, relativeTime */
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
// GR_NewGraphModal — seeds a minimal begin→agent→end skeleton
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
    // Seed a minimal valid graph: Begin → agent → End, wired by
    // static edges. The Graph model validator requires exactly one
    // Begin node and at least one End node reachable from Begin.
    const body = {
      ...(id ? { id } : {}),
      description: description || "(no description)",
      nodes: [
        { kind: "begin", id: "begin" },
        { kind: "agent", id: "start", agent_id: seedAgentId },
        { kind: "end", id: "end", output_template: "" },
      ],
      edges: [
        { kind: "static", from_node: "begin", to_node: "start" },
        { kind: "static", from_node: "start", to_node: "end" },
      ],
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
            required · the new graph starts with Begin → agent → End
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
  const { apiFetch, useResource, useViewport } = window.primerApi;
  const { isMobile } = useViewport();
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

      {isMobile ? (
        list.loading && items.length === 0 ? (
          <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</div>
        ) : list.error && items.length === 0 ? (
          <Banner
            kind="error"
            title={list.error.title || "Couldn't load graphs"}
            detail={list.error.detail || list.error.message}
            actions={<Btn size="sm" icon="refresh" onClick={() => list.refetch()}>Retry</Btn>}
          />
        ) : (
          <CardList
            items={filtered}
            empty={items.length === 0 ? "No graphs yet." : "No graphs match."}
            renderCard={(g) => {
              const status = perRowStatus[g.id];
              const nodeCount = (g.nodes || []).length;
              const edgeCount = (g.edges || []).length;
              const statusPill = status == null
                ? null
                : status.ok === true
                  ? <span className="pill pill-ended"><span className="dot"></span>ok</span>
                  : status.ok === false
                    ? <span className="pill pill-failed"><span className="dot"></span>{(status.issues || []).length} issue{(status.issues || []).length === 1 ? "" : "s"}</span>
                    : <span className="muted" title={status.error}>err</span>;
              const metaParts = [
                `${nodeCount} node${nodeCount === 1 ? "" : "s"}`,
                `${edgeCount} edge${edgeCount === 1 ? "" : "s"}`,
              ];
              if (g.entry_node_id) metaParts.push(`entry: ${g.entry_node_id}`);
              return (
                <Card
                  title={g.id}
                  subtitle={g.description || null}
                  pill={statusPill}
                  meta={metaParts.join(" · ")}
                  onClick={() => onOpen(g.id)}
                />
              );
            }}
          />
        )
      ) : (
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
      )}

      {isMobile && (
        <Fab icon="plus" label="New graph" onClick={() => setCreateOpen(true)} />
      )}

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
  const { apiFetch, useMutation, useResource, useRouter } = window.primerApi;
  const { navigate } = useRouter();

  // Track JSON parse errors from per-node JsonField helpers; Save is
  // disabled when any error key is non-null.
  const [jsonErrors, setJsonErrors] = React.useState({});
  const reportJsonError = React.useCallback((key, msg) => {
    setJsonErrors((prev) => {
      if (!msg) {
        if (!(key in prev)) return prev;
        const next = { ...prev };
        delete next[key];
        return next;
      }
      if (prev[key] === msg) return prev;
      return { ...prev, [key]: msg };
    });
  }, []);

  // Agents + graphs lists for dropdowns on agent/graph node forms.
  const agents = useResource(
    "graphs-editor:agents",
    (s) => apiFetch("GET", "/agents?limit=200", null, { signal: s }),
    {},
  );
  const allGraphs = useResource(
    "graphs-editor:graphs",
    (s) => apiFetch("GET", "/graphs?limit=200", null, { signal: s }),
    {},
  );

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
  const [selectedEdgeId, setSelectedEdgeId] = React.useState(null);  // index into draft.edges
  const [addEdgeMode, setAddEdgeMode] = React.useState(null);  // null | {fromId?}
  const [edgeMode, setEdgeMode] = React.useState("static");  // "static" | "conditional"
  const [dragging, setDragging] = React.useState(null);
  const [showAddMenu, setShowAddMenu] = React.useState(false);
  const canvasRef = React.useRef(null);

  // Sync draft when server payload changes.
  React.useEffect(() => {
    setDraft(seed);
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
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
    setSelectedEdgeId(null);
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
    // For begin/end use plain kind as the base id; for everything else use
    // a `${kind}_<n>` slug (with `fan_out`/`fan_in`/`tool_call` collapsed
    // to their short forms so the seed id stays readable).
    const slug = kind === "fan_out"
      ? "fanout"
      : kind === "fan_in"
        ? "fanin"
        : kind === "tool_call"
          ? "tool"
          : kind;
    const seed = kind === "begin" || kind === "end" ? kind : `${slug}_1`;
    let newId = seed;
    let i = 1;
    while (existingIds.has(newId)) {
      i += 1;
      newId = kind === "begin" || kind === "end" ? `${kind}_${i}` : `${slug}_${i}`;
    }
    let newNode;
    if (kind === "agent") newNode = { kind: "agent", id: newId, agent_id: "" };
    else if (kind === "graph") newNode = { kind: "graph", id: newId, graph_id: "" };
    else if (kind === "begin") newNode = { kind: "begin", id: newId };
    else if (kind === "end") newNode = { kind: "end", id: newId, output_template: "" };
    else if (kind === "fan_out") {
      // Seed with a single broadcast spec so the node is "shaped" enough
      // for the side-panel form to render meaningfully. Target stays
      // blank — operator picks it from the dropdown.
      newNode = {
        kind: "fan_out",
        id: newId,
        specs: [
          {
            kind: "broadcast",
            target_node_id: "",
            count: 1,
            on_failure: "fail_fast",
          },
        ],
      };
    } else if (kind === "fan_in") {
      newNode = { kind: "fan_in", id: newId, aggregate_template: "" };
    } else if (kind === "tool_call") {
      newNode = {
        kind: "tool_call",
        id: newId,
        tool_id: "",
        arguments: {},
      };
    } else throw new Error("unknown node kind: " + kind);
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
        const newEdge = edgeMode === "static"
          ? { kind: "static", from_node: addEdgeMode.fromId, to_node: nodeId }
          : {
              kind: "conditional",
              from_node: addEdgeMode.fromId,
              router: {
                kind: "json_path",
                branches: [{ conditions: [], to_node: nodeId }],
                default_to: nodeId,
              },
            };
        setDraft((d) => ({ ...d, edges: [...(d.edges || []), newEdge] }));
        setAddEdgeMode(null);
      }
      return;
    }
    setSelectedNodeId(nodeId);
    setSelectedEdgeId(null);
  };

  const onEdgeClick = (edgeIdx) => {
    setSelectedEdgeId(edgeIdx);
    setSelectedNodeId(null);
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
  const hasBegin = (draft.nodes || []).some((n) => n.kind === "begin");
  const violations = GR_localViolations(draft);
  const hardViolation = violations.some((v) => v.kind === "hard");

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
              <a
                className="dd-item"
                onClick={() => { if (!hasBegin) onAddNode("begin"); }}
                style={{
                  ...GR_DD_ITEM_STYLE,
                  opacity: hasBegin ? 0.4 : 1,
                  cursor: hasBegin ? "not-allowed" : "pointer",
                }}
                aria-disabled={hasBegin}
              >
                Begin{hasBegin ? " (exists)" : ""}
              </a>
              <a className="dd-item" onClick={() => onAddNode("agent")} style={GR_DD_ITEM_STYLE}>Agent</a>
              <a className="dd-item" onClick={() => onAddNode("graph")} style={GR_DD_ITEM_STYLE}>Subgraph</a>
              <a className="dd-item" onClick={() => onAddNode("fan_out")} style={GR_DD_ITEM_STYLE}>Fan-out</a>
              <a className="dd-item" onClick={() => onAddNode("fan_in")} style={GR_DD_ITEM_STYLE}>Fan-in</a>
              <a className="dd-item" onClick={() => onAddNode("tool_call")} style={GR_DD_ITEM_STYLE}>Tool call</a>
              <a className="dd-item" onClick={() => onAddNode("end")} style={GR_DD_ITEM_STYLE}>End</a>
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
        <div
          className="seg"
          role="group"
          aria-label="Edge mode"
          style={{
            display: "inline-flex",
            border: "1px solid var(--border)",
            borderRadius: 6,
            overflow: "hidden",
          }}
        >
          <button
            type="button"
            onClick={() => setEdgeMode("static")}
            className={edgeMode === "static" ? "active" : ""}
            style={{
              padding: "4px 10px",
              fontSize: 12,
              border: "none",
              background: edgeMode === "static" ? "var(--accent)" : "transparent",
              color: edgeMode === "static" ? "var(--bg)" : "var(--text-2)",
              cursor: "pointer",
            }}
          >
            Static
          </button>
          <button
            type="button"
            onClick={() => setEdgeMode("conditional")}
            className={edgeMode === "conditional" ? "active" : ""}
            style={{
              padding: "4px 10px",
              fontSize: 12,
              border: "none",
              background: edgeMode === "conditional" ? "var(--accent)" : "transparent",
              color: edgeMode === "conditional" ? "var(--bg)" : "var(--text-2)",
              cursor: "pointer",
            }}
          >
            Conditional
          </button>
        </div>
        <Btn size="sm" kind="ghost" icon="refresh" onClick={onAutoLayout}>Auto-layout</Btn>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6 }}>
          {diffCount > 0 && <span className="muted text-sm tabular">· unsaved changes</span>}
          <Btn size="sm" kind="ghost" onClick={onDiscard} disabled={diffCount === 0}>Discard</Btn>
          <Btn
            size="sm"
            kind="primary"
            icon="check"
            disabled={
              diffCount === 0
              || save.loading
              || Object.keys(jsonErrors).length > 0
              || hardViolation
            }
            onClick={onSave}
          >
            {save.loading ? "Saving…" : "Save"}
          </Btn>
        </div>
      </div>

      {/* Topology violations banner (non-blocking; hard violations gate Save) */}
      {violations.length > 0 && (
        <GR_ViolationsBanner violations={violations} />
      )}

      {/* Editor + side panel */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 260px" }}>
        <GR_Canvas
          ref={canvasRef}
          draft={draft}
          selectedNodeId={selectedNodeId}
          selectedEdgeId={selectedEdgeId}
          addEdgeMode={addEdgeMode}
          onNodeClick={onNodeClick}
          onEdgeClick={onEdgeClick}
          onNodeDoubleClick={onNodeDoubleClick}
          onNodeMouseDown={onNodeMouseDown}
          onBackgroundClick={() => {
            setSelectedNodeId(null);
            setSelectedEdgeId(null);
            if (addEdgeMode) setAddEdgeMode(null);
          }}
        />
        <GR_SidePanel
          draft={draft}
          selected={selected}
          selectedEdgeIdx={selectedEdgeId}
          agentsList={agents.data?.items}
          graphsList={allGraphs.data?.items}
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
            setSelectedEdgeId(null);
          }}
          onUpdateEdge={(idx, nextEdge) => {
            setDraft((d) => ({
              ...d,
              edges: d.edges.map((e, i) => i === idx ? nextEdge : e),
            }));
          }}
          onNavigateSubgraph={(gid) => navigate("/graphs/" + gid)}
          onSetGraph={(patch) => setDraft((d) => ({ ...d, ...patch }))}
          onReportJsonError={reportJsonError}
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

function GR_ViolationsBanner({ violations }) {
  const hard = violations.filter((v) => v.kind === "hard");
  const soft = violations.filter((v) => v.kind === "soft");
  const hasHard = hard.length > 0;
  const bg = hasHard ? "var(--red-dim, rgba(220, 38, 38, 0.08))" : "var(--amber-dim, rgba(217, 119, 6, 0.08))";
  const borderColor = hasHard ? "var(--red)" : "var(--amber)";
  const titleColor = hasHard ? "var(--red)" : "var(--amber)";
  return (
    <div
      className="violations-banner"
      style={{
        margin: "6px 12px 0 12px",
        padding: "8px 10px",
        background: bg,
        border: `1px solid ${borderColor}`,
        borderRadius: 6,
        fontSize: 11.5,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
        <Icon name={hasHard ? "alert-triangle" : "info"} size={12} style={{ color: titleColor }} />
        <span style={{ fontWeight: 600, color: titleColor }}>
          {hasHard
            ? `${hard.length} hard violation${hard.length === 1 ? "" : "s"}`
            : `${soft.length} warning${soft.length === 1 ? "" : "s"}`}
          {hasHard && soft.length > 0
            ? ` · ${soft.length} warning${soft.length === 1 ? "" : "s"}`
            : ""}
        </span>
        {hasHard && (
          <span className="muted" style={{ marginLeft: "auto", fontSize: 10.5 }}>
            Save disabled until fixed
          </span>
        )}
      </div>
      <ul style={{ margin: 0, paddingLeft: 16 }}>
        {hard.map((v, i) => (
          <li key={`h${i}`} style={{ color: "var(--red)" }}>{v.text}</li>
        ))}
        {soft.map((v, i) => (
          <li key={`s${i}`} className="muted">{v.text}</li>
        ))}
      </ul>
    </div>
  );
}

// ----------------------------------------------------------------------------
// GR_localViolations — client-side topology checks. Hard violations disable
// Save; soft violations are surfaced as warnings.
//
// Hard rules:
//   - exactly one Begin node
//   - at least one End node
//   - all End nodes reachable from Begin (forward-reachable via edges)
//   - no duplicate node ids
//   - every edge endpoint references a known node id
// Soft rules:
//   - orphan nodes (in-degree == 0 && kind != "begin")
//   - Begin without a description
// ----------------------------------------------------------------------------

function GR_localViolations(g) {
  const out = [];
  if (!g || !Array.isArray(g.nodes)) return out;

  const nodes = g.nodes;
  const edges = Array.isArray(g.edges) ? g.edges : [];

  // Duplicate ids.
  const seen = new Map();
  for (const n of nodes) {
    seen.set(n.id, (seen.get(n.id) || 0) + 1);
  }
  for (const [id, count] of seen.entries()) {
    if (count > 1) {
      out.push({ kind: "hard", text: `Duplicate node id: ${id} (${count}×)` });
    }
  }

  // Begin / End counts.
  const begins = nodes.filter((n) => n.kind === "begin");
  const ends = nodes.filter((n) => n.kind === "end");
  if (begins.length === 0) {
    out.push({ kind: "hard", text: "Exactly one Begin node required (got 0)" });
  } else if (begins.length > 1) {
    out.push({ kind: "hard", text: `Exactly one Begin node required (got ${begins.length})` });
  }
  if (ends.length === 0) {
    out.push({ kind: "hard", text: "At least one End node required" });
  }

  // Edge endpoints.
  const nodeIds = new Set(nodes.map((n) => n.id));
  const edgeTargets = (e) => {
    if (e.kind === "static") return [e.to_node];
    if (e.kind === "conditional") {
      const r = e.router || {};
      if (r.kind === "json_path") {
        const ts = (r.branches || []).map((b) => b.to_node);
        if (r.default_to) ts.push(r.default_to);
        return ts;
      }
    }
    return [];
  };
  for (let i = 0; i < edges.length; i += 1) {
    const e = edges[i];
    if (!nodeIds.has(e.from_node)) {
      out.push({ kind: "hard", text: `Edge ${i}: unknown from_node "${e.from_node}"` });
    }
    for (const t of edgeTargets(e)) {
      if (!nodeIds.has(t)) {
        out.push({ kind: "hard", text: `Edge ${i}: unknown target "${t}"` });
      }
    }
  }

  // Forward reachability from Begin.
  if (begins.length === 1 && ends.length > 0) {
    const adj = new Map(nodes.map((n) => [n.id, []]));
    for (const e of edges) {
      if (!adj.has(e.from_node)) continue;
      for (const t of edgeTargets(e)) {
        if (adj.has(t)) adj.get(e.from_node).push(t);
      }
    }
    const reachable = new Set();
    const stack = [begins[0].id];
    while (stack.length) {
      const cur = stack.pop();
      if (reachable.has(cur)) continue;
      reachable.add(cur);
      for (const nx of (adj.get(cur) || [])) stack.push(nx);
    }
    for (const ed of ends) {
      if (!reachable.has(ed.id)) {
        out.push({ kind: "hard", text: `End "${ed.id}" is not reachable from Begin` });
      }
    }
  }

  // Soft: orphan nodes (no incoming edge, not the Begin).
  const incoming = new Map(nodes.map((n) => [n.id, 0]));
  for (const e of edges) {
    for (const t of edgeTargets(e)) {
      if (incoming.has(t)) incoming.set(t, incoming.get(t) + 1);
    }
  }
  for (const n of nodes) {
    if (n.kind === "begin") continue;
    if ((incoming.get(n.id) || 0) === 0) {
      out.push({ kind: "soft", text: `Node "${n.id}" has no incoming edges` });
    }
  }

  // Soft: Begin without a description.
  if (begins.length === 1 && !begins[0].description) {
    out.push({ kind: "soft", text: "Begin node has no description" });
  }

  return out;
}

// ----------------------------------------------------------------------------
// GR_Canvas (forwardRef so the editor can attach its ref)
// ----------------------------------------------------------------------------

const GR_NODE_SIZE = {
  agent: { w: 150, h: 56 },
  graph: { w: 150, h: 56 },
  begin: { w: 22, h: 22 },
  end: { w: 22, h: 22 },
  // Spec B node kinds:
  fan_out: { w: 170, h: 56 },
  fan_in: { w: 150, h: 56 },
  tool_call: { w: 170, h: 56 },
};

const GR_Canvas = React.forwardRef(function GR_Canvas(
  {
    draft,
    selectedNodeId,
    selectedEdgeId,
    addEdgeMode,
    onNodeClick,
    onEdgeClick,
    onNodeDoubleClick,
    onNodeMouseDown,
    onBackgroundClick,
  },
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
            <GR_EdgePath
              key={i}
              edge={e}
              nodes={draft.nodes}
              selected={selectedEdgeId === i}
              onClick={(ev) => { ev.stopPropagation(); if (onEdgeClick) onEdgeClick(i); }}
            />
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
// GR_NodeBox — begin / agent / graph / end nodes
// ----------------------------------------------------------------------------

function GR_NodeBox({ node, selected, entry, edgePicking, edgePickStage, onClick, onDoubleClick, onMouseDown }) {
  const isBegin = node.kind === "begin";
  const isEnd = node.kind === "end";
  const isGraph = node.kind === "graph";

  const baseStyle = {
    position: "absolute",
    left: node.x || 0,
    top: node.y || 0,
    cursor: edgePicking ? "crosshair" : "grab",
  };

  if (isBegin || isEnd) {
    // Begin: hollow ring (accent); End: filled disk (text color).
    const ringStyle = isBegin
      ? {
          background: selected ? "var(--accent-dim)" : "var(--bg-1)",
          border: edgePickStage === "from"
            ? "2px dashed var(--accent)"
            : selected
              ? "2px solid var(--accent)"
              : "2px solid var(--accent)",
        }
      : {
          background: selected ? "var(--accent)" : "var(--text)",
          border: edgePickStage === "from"
            ? "2px dashed var(--accent)"
            : selected
              ? "2px solid var(--accent)"
              : "2px solid var(--border-strong)",
        };
    return (
      <div onMouseDown={onMouseDown} onClick={onClick} onDoubleClick={onDoubleClick} style={baseStyle}>
        <div style={{
          width: 22,
          height: 22,
          borderRadius: "50%",
          ...ringStyle,
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

function GR_EdgePath({ edge, nodes, selected, onClick }) {
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
    return (
      <GR_SingleEdge
        fx={fx}
        fy={fy}
        to={to}
        dashed={false}
        label={null}
        selected={selected}
        onClick={onClick}
      />
    );
  }

  // conditional
  const router = edge.router || {};
  if (router.kind === "json_path") {
    const out = [];
    const branches = router.branches || [];
    for (let i = 0; i < branches.length; i += 1) {
      const br = branches[i];
      const to = nodes.find((n) => n.id === br.to_node);
      if (!to) continue;
      // Prefer the new `conditions` shape; fall back to the legacy `when`.
      const condBits = (br.conditions || []).map((c) => {
        if (c.op === "exists") return `${c.path}?`;
        return `${c.path} ${c.op} ${JSON.stringify(c.value)}`;
      });
      const whenBits = Object.entries(br.when || {}).map(([k, v]) => `${k}=${v}`);
      const label = (condBits.length ? condBits : whenBits).join(" ∧ ");
      out.push(
        <GR_SingleEdge
          key={"b" + i}
          fx={fx}
          fy={fy}
          to={to}
          dashed
          label={label}
          selected={selected}
          onClick={onClick}
        />,
      );
    }
    if (router.default_to) {
      const to = nodes.find((n) => n.id === router.default_to);
      if (to) {
        out.push(
          <GR_SingleEdge
            key="def"
            fx={fx}
            fy={fy}
            to={to}
            dashed
            label="(default)"
            selected={selected}
            onClick={onClick}
          />,
        );
      }
    }
    // Branch-count badge near the source midpoint.
    const badgeText = `${branches.length} branch${branches.length === 1 ? "" : "es"}`;
    out.push(
      <text
        key="badge"
        x={fx + 8}
        y={fy - 8}
        fontSize="9.5"
        fontFamily="IBM Plex Mono"
        fill="var(--accent)"
        style={{ pointerEvents: "auto", cursor: "pointer" }}
        onClick={onClick}
      >
        {badgeText}
      </text>,
    );
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

function GR_SingleEdge({ fx, fy, to, dashed, label, selected, onClick }) {
  const toSize = GR_NODE_SIZE[to.kind] || GR_NODE_SIZE.agent;
  const tx = to.x || 0;
  const ty = (to.y || 0) + toSize.h / 2;
  const mx = (fx + tx) / 2;
  const path = `M ${fx} ${fy} C ${mx} ${fy}, ${mx} ${ty}, ${tx} ${ty}`;
  const stroke = selected ? "var(--accent)" : (dashed ? "var(--accent)" : "var(--text-3)");
  const marker = dashed ? "url(#arrow-cond)" : "url(#arrow-static)";
  const strokeWidth = selected ? "3.2" : "1.6";
  const interactive = typeof onClick === "function";
  return (
    <g style={interactive ? { pointerEvents: "auto", cursor: "pointer" } : undefined} onClick={onClick}>
      {/* Invisible wide hit target for easier clicking on thin paths. */}
      {interactive && (
        <path
          d={path}
          stroke="transparent"
          strokeWidth="12"
          fill="none"
        />
      )}
      <path
        d={path}
        stroke={stroke}
        strokeWidth={strokeWidth}
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
// Form-field helpers (text / textarea / number / JSON)
// ----------------------------------------------------------------------------

function GR_TextField({ label, value, onChange, placeholder, help }) {
  return (
    <div className="field">
      <label className="field-label">{label}</label>
      <input
        className="input"
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder || ""}
        style={{ width: "100%" }}
      />
      {help && <div className="field-help muted">{help}</div>}
    </div>
  );
}

function GR_TextAreaField({ label, value, onChange, rows, help, placeholder }) {
  return (
    <div className="field">
      <label className="field-label">{label}</label>
      <textarea
        className="textarea mono"
        rows={rows || 4}
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder || ""}
        style={{ width: "100%", fontFamily: "IBM Plex Mono", fontSize: 12 }}
      />
      {help && <div className="field-help muted">{help}</div>}
    </div>
  );
}

function GR_NumberField({ label, value, onChange, help, placeholder }) {
  return (
    <div className="field">
      <label className="field-label">{label}</label>
      <input
        className="input"
        type="number"
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder || ""}
        style={{ width: "100%" }}
      />
      {help && <div className="field-help muted">{help}</div>}
    </div>
  );
}

// JsonField parses the textarea on blur and reports parse errors up to the
// parent via `onError`. Empty input is treated as null. The parent can track
// outstanding errors and disable Save.
function GR_JsonField({ label, value, onChange, onError, help, errorKey }) {
  const [text, setText] = React.useState(
    value === undefined || value === null ? "" : JSON.stringify(value, null, 2),
  );
  const [err, setErr] = React.useState(null);
  // Sync local text when value changes externally (e.g. a different node selected).
  React.useEffect(() => {
    setText(value === undefined || value === null ? "" : JSON.stringify(value, null, 2));
    setErr(null);
    if (onError && errorKey) onError(errorKey, null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  function commit() {
    if (text.trim() === "") {
      onChange(null);
      setErr(null);
      if (onError && errorKey) onError(errorKey, null);
      return;
    }
    try {
      onChange(JSON.parse(text));
      setErr(null);
      if (onError && errorKey) onError(errorKey, null);
    } catch (e) {
      const msg = String(e.message || e);
      setErr(msg);
      if (onError && errorKey) onError(errorKey, msg);
    }
  }
  return (
    <div className="field">
      <label className="field-label">{label}</label>
      <textarea
        className="textarea mono"
        rows={6}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        style={{ width: "100%", fontFamily: "IBM Plex Mono", fontSize: 12 }}
      />
      {help && <div className="field-help muted">{help}</div>}
      {err && <div className="field-help" style={{ color: "var(--red)" }}>JSON parse: {err}</div>}
    </div>
  );
}

// ----------------------------------------------------------------------------
// Side panel
// ----------------------------------------------------------------------------

function GR_SidePanel({
  draft,
  selected,
  selectedEdgeIdx,
  onUpdateNode,
  onDeleteNode,
  onSetEntry,
  onDeleteEdgeAt,
  onUpdateEdge,
  onNavigateSubgraph,
  onSetGraph,
  onReportJsonError,
  agentsList,
  graphsList,
}) {
  const selectedEdge = selectedEdgeIdx != null ? (draft.edges || [])[selectedEdgeIdx] : null;
  return (
    <div style={{ borderLeft: "1px solid var(--border)", padding: 14, fontSize: 12.5, overflow: "auto", maxHeight: 500 }}>
      {selected ? (
        <GR_SelectedNodeForm
          node={selected}
          isEntry={draft.entry_node_id === selected.id}
          edges={draft.edges || []}
          allNodes={draft.nodes || []}
          onUpdateNode={onUpdateNode}
          onDeleteNode={onDeleteNode}
          onSetEntry={onSetEntry}
          onDeleteEdgeAt={onDeleteEdgeAt}
          onNavigateSubgraph={onNavigateSubgraph}
          onReportJsonError={onReportJsonError}
          agentsList={agentsList}
          graphsList={graphsList}
        />
      ) : selectedEdge ? (
        <GR_SelectedEdgeForm
          edge={selectedEdge}
          edgeIdx={selectedEdgeIdx}
          nodes={draft.nodes || []}
          onUpdateEdge={onUpdateEdge}
          onDeleteEdge={() => onDeleteEdgeAt(selectedEdgeIdx)}
        />
      ) : (
        <GR_GraphStatsBlock draft={draft} onSetGraph={onSetGraph} />
      )}
    </div>
  );
}

function GR_GraphStatsBlock({ draft, onSetGraph }) {
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
      {onSetGraph ? (
        <>
          <GR_TextField
            label="description"
            value={draft.description ?? ""}
            onChange={(v) => onSetGraph({ description: v })}
            placeholder="(no description)"
          />
          <GR_NumberField
            label="max_iterations"
            value={draft.max_iterations ?? ""}
            onChange={(v) =>
              onSetGraph({ max_iterations: v === "" ? null : Number(v) })
            }
            placeholder="unlimited"
            help="Cap on per-graph iterations (empty = unlimited)."
          />
        </>
      ) : (
        <div className="muted text-sm">{draft.description}</div>
      )}
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
        <dt>begin</dt><dd>{(draft.nodes || []).filter((n) => n.kind === "begin").length}</dd>
        <dt>ends</dt><dd>{(draft.nodes || []).filter((n) => n.kind === "end").length}</dd>
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

function GR_SelectedNodeForm({
  node,
  isEntry,
  edges,
  allNodes,
  onUpdateNode,
  onDeleteNode,
  onSetEntry,
  onDeleteEdgeAt,
  onNavigateSubgraph,
  onReportJsonError,
  agentsList,
  graphsList,
}) {
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

  const errBase = `node:${node.id}`;

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

      {/* description shared across all kinds */}
      <GR_TextField
        label="description"
        value={node.description ?? ""}
        onChange={(v) => onUpdateNode({ description: v || null })}
        placeholder="(optional)"
      />

      {/* Per-kind fields */}
      {node.kind === "begin" && (
        <GR_JsonField
          label="input_schema"
          value={node.input_schema}
          onChange={(v) => onUpdateNode({ input_schema: v })}
          onError={onReportJsonError}
          errorKey={`${errBase}:input_schema`}
          help="Optional JSON Schema (Draft 2020-12) for graph_input."
        />
      )}

      {node.kind === "end" && (
        <>
          <GR_TextAreaField
            label="output_template (Jinja2)"
            value={node.output_template || ""}
            onChange={(v) => onUpdateNode({ output_template: v })}
            rows={4}
            placeholder="{{ result }}"
          />
          <GR_JsonField
            label="output_schema"
            value={node.output_schema}
            onChange={(v) => onUpdateNode({ output_schema: v })}
            onError={onReportJsonError}
            errorKey={`${errBase}:output_schema`}
            help="Optional JSON Schema validated against End's parsed payload."
          />
        </>
      )}

      {node.kind === "agent" && (
        <>
          <div className="field">
            <label className="field-label">agent_id</label>
            {agentsList && Array.isArray(agentsList) ? (
              <select
                className="select"
                value={node.agent_id || ""}
                onChange={(e) => onUpdateNode({ agent_id: e.target.value })}
                style={{ width: "100%" }}
              >
                <option value="">— pick an agent —</option>
                {agentsList.map((a) => (
                  <option key={a.id} value={a.id}>{a.id}</option>
                ))}
              </select>
            ) : (
              <input
                className="input"
                value={node.agent_id || ""}
                onChange={(e) => onUpdateNode({ agent_id: e.target.value })}
                placeholder="(none)"
                style={{ width: "100%" }}
              />
            )}
          </div>
          <GR_TextAreaField
            label="input_template (Jinja2)"
            value={node.input_template || ""}
            onChange={(v) => onUpdateNode({ input_template: v || null })}
            rows={4}
            placeholder="{{ graph_input.question }}"
          />
          <GR_JsonField
            label="input_schema (designer metadata)"
            value={node.input_schema}
            onChange={(v) => onUpdateNode({ input_schema: v })}
            onError={onReportJsonError}
            errorKey={`${errBase}:input_schema`}
          />
          <GR_JsonField
            label="response_format"
            value={node.response_format}
            onChange={(v) => onUpdateNode({ response_format: v })}
            onError={onReportJsonError}
            errorKey={`${errBase}:response_format`}
            help="Optional structured-output schema for this agent."
          />
        </>
      )}

      {node.kind === "graph" && (
        <>
          <div className="field">
            <label className="field-label">
              graph_id <span className="hint">double-click node to navigate</span>
            </label>
            <div style={{ display: "flex", gap: 4 }}>
              {graphsList && Array.isArray(graphsList) ? (
                <select
                  className="select"
                  value={node.graph_id || ""}
                  onChange={(e) => onUpdateNode({ graph_id: e.target.value })}
                  style={{ flex: 1 }}
                >
                  <option value="">— pick a graph —</option>
                  {graphsList.map((g) => (
                    <option key={g.id} value={g.id}>{g.id}</option>
                  ))}
                </select>
              ) : (
                <input
                  className="input"
                  value={node.graph_id || ""}
                  onChange={(e) => onUpdateNode({ graph_id: e.target.value })}
                  placeholder="(none)"
                  style={{ flex: 1 }}
                />
              )}
              {node.graph_id && (
                <Btn size="sm" icon="chevron-right" kind="ghost" onClick={() => onNavigateSubgraph(node.graph_id)}>
                  Open
                </Btn>
              )}
            </div>
          </div>
          <GR_TextAreaField
            label="input_template (Jinja2)"
            value={node.input_template || ""}
            onChange={(v) => onUpdateNode({ input_template: v || null })}
            rows={4}
            placeholder="{{ graph_input }}"
          />
        </>
      )}

      {node.kind === "fan_out" && (
        <GR_FanOutSpecsEditor
          node={node}
          otherNodeIds={(allNodes || []).map((n) => n.id).filter((id) => id !== node.id)}
          onUpdateNode={onUpdateNode}
        />
      )}

      {node.kind === "fan_in" && (
        <>
          <GR_TextAreaField
            label="aggregate_template (Jinja2)"
            value={node.aggregate_template || ""}
            onChange={(v) => onUpdateNode({ aggregate_template: v })}
            rows={6}
            placeholder={"{\n  \"items\": {{ inputs | map(attribute='parsed') | list | tojson }}\n}"}
            help={
              "Aggregator scope: `inputs` is a list of upstream NodeOutputs "
              + "(each with `.parsed`, `.text`, `.error`). Template must "
              + "render to JSON."
            }
          />
          <GR_JsonField
            label="output_schema"
            value={node.output_schema}
            onChange={(v) => onUpdateNode({ output_schema: v })}
            onError={onReportJsonError}
            errorKey={`${errBase}:output_schema`}
            help="Optional JSON Schema validated against the rendered aggregate."
          />
        </>
      )}

      {node.kind === "tool_call" && (
        <GR_ToolCallForm
          node={node}
          onUpdateNode={onUpdateNode}
          onReportJsonError={onReportJsonError}
          errBase={errBase}
        />
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
        {!isEntry && node.kind !== "begin" && node.kind !== "end" && (
          <Btn size="sm" kind="ghost" icon="play" onClick={onSetEntry}>Set as entry</Btn>
        )}
        {isEntry && <span className="muted text-sm">(entry node)</span>}
        <Btn size="sm" kind="danger" icon="trash" onClick={onDeleteNode}>Delete node</Btn>
      </div>
      <div className="muted text-sm mt-2">
        Edits stage locally; click Save to PUT-replace the whole graph.
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------------
// GR_FanOutSpecsEditor — list of FanOutSpec rows (broadcast / tee / map).
// Spec B §1.1 — `_FanOutNode.specs: list[FanOutSpec]`; each spec is
// discriminated by `kind` with one of three shapes:
//   broadcast: target_node_id + count
//   tee      : target_node_ids
//   map      : target_node_id + source_node_id + source_path
// All three share `on_failure: fail_fast | drain_then_fail | collect`.
// State updates flow back into `draft.nodes[i].specs` via onUpdateNode.
// ----------------------------------------------------------------------------

const GR_ON_FAILURE_OPTS = ["fail_fast", "drain_then_fail", "collect"];

function GR_FanOutSpecsEditor({ node, otherNodeIds, onUpdateNode }) {
  const specs = Array.isArray(node.specs) ? node.specs : [];

  function setSpecs(next) {
    onUpdateNode({ specs: next });
  }
  function updateSpec(i, patch) {
    setSpecs(specs.map((s, j) => (i === j ? { ...s, ...patch } : s)));
  }
  function changeKind(i, kind) {
    // Reset disallowed fields when switching kinds — the Python
    // FanOutSpec validator rejects any cross-kind leftover.
    const base = { kind, on_failure: specs[i]?.on_failure || "fail_fast" };
    if (kind === "broadcast") {
      setSpecs(specs.map((s, j) => (i === j
        ? { ...base, target_node_id: "", count: 1 }
        : s)));
    } else if (kind === "tee") {
      setSpecs(specs.map((s, j) => (i === j
        ? { ...base, target_node_ids: [] }
        : s)));
    } else { // map
      setSpecs(specs.map((s, j) => (i === j
        ? { ...base, target_node_id: "", source_node_id: "", source_path: "" }
        : s)));
    }
  }
  function removeSpec(i) {
    setSpecs(specs.filter((_, j) => j !== i));
  }
  function addSpec() {
    setSpecs([
      ...specs,
      { kind: "broadcast", target_node_id: "", count: 1, on_failure: "fail_fast" },
    ]);
  }
  function toggleTeeTarget(i, id, checked) {
    const cur = specs[i]?.target_node_ids || [];
    const next = checked
      ? Array.from(new Set([...cur, id]))
      : cur.filter((x) => x !== id);
    updateSpec(i, { target_node_ids: next });
  }

  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="muted text-sm mono" style={{
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        fontSize: 10.5,
      }}>
        specs ({specs.length})
      </div>
      {specs.length === 0 && (
        <div className="muted text-sm">— no specs (at least one required) —</div>
      )}
      {specs.map((spec, i) => (
        <div
          key={i}
          className="spec-card"
          style={{
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: 8,
            background: "var(--bg-1)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
            <span className="muted text-sm mono">spec {i + 1}</span>
            <a
              onClick={() => removeSpec(i)}
              style={{ cursor: "pointer", color: "var(--red)", fontSize: 12 }}
              title="Delete spec"
            >
              × remove
            </a>
          </div>
          <div className="field">
            <label className="field-label">kind</label>
            <select
              className="select"
              value={spec.kind || "broadcast"}
              onChange={(e) => changeKind(i, e.target.value)}
              style={{ width: "100%" }}
            >
              <option value="broadcast">broadcast — N copies of one target</option>
              <option value="tee">tee — one copy each to N targets</option>
              <option value="map">map — one per item in upstream array</option>
            </select>
          </div>

          {spec.kind === "broadcast" && (
            <>
              <div className="field">
                <label className="field-label">target_node_id</label>
                <select
                  className="select"
                  value={spec.target_node_id || ""}
                  onChange={(e) => updateSpec(i, { target_node_id: e.target.value })}
                  style={{ width: "100%" }}
                >
                  <option value="">— pick a target —</option>
                  {(otherNodeIds || []).map((id) => (
                    <option key={id} value={id}>{id}</option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label className="field-label">count</label>
                <input
                  className="input"
                  type="number"
                  min={1}
                  value={spec.count ?? 1}
                  onChange={(e) => {
                    const n = Number(e.target.value);
                    updateSpec(i, { count: Number.isFinite(n) && n >= 1 ? n : 1 });
                  }}
                  style={{ width: "100%" }}
                />
              </div>
            </>
          )}

          {spec.kind === "tee" && (
            <div className="field">
              <label className="field-label">
                target_node_ids{" "}
                <span className="hint">tick each target</span>
              </label>
              <div style={{
                border: "1px solid var(--border)",
                borderRadius: 4,
                padding: 6,
                background: "var(--bg)",
                maxHeight: 120,
                overflowY: "auto",
              }}>
                {(otherNodeIds || []).length === 0 && (
                  <div className="muted text-sm">— no other nodes —</div>
                )}
                {(otherNodeIds || []).map((id) => {
                  const checked = (spec.target_node_ids || []).includes(id);
                  return (
                    <label key={id} className="mono text-sm" style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                      cursor: "pointer",
                      padding: "2px 0",
                    }}>
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(e) => toggleTeeTarget(i, id, e.target.checked)}
                      />
                      <span>{id}</span>
                    </label>
                  );
                })}
              </div>
            </div>
          )}

          {spec.kind === "map" && (
            <>
              <div className="field">
                <label className="field-label">target_node_id</label>
                <select
                  className="select"
                  value={spec.target_node_id || ""}
                  onChange={(e) => updateSpec(i, { target_node_id: e.target.value })}
                  style={{ width: "100%" }}
                >
                  <option value="">— pick a target —</option>
                  {(otherNodeIds || []).map((id) => (
                    <option key={id} value={id}>{id}</option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label className="field-label">source_node_id</label>
                <select
                  className="select"
                  value={spec.source_node_id || ""}
                  onChange={(e) => updateSpec(i, { source_node_id: e.target.value })}
                  style={{ width: "100%" }}
                >
                  <option value="">— pick a source —</option>
                  {(otherNodeIds || []).map((id) => (
                    <option key={id} value={id}>{id}</option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label className="field-label">source_path</label>
                <input
                  className="input mono"
                  value={spec.source_path || ""}
                  onChange={(e) => updateSpec(i, { source_path: e.target.value })}
                  placeholder=". or data.items[*]"
                  style={{ width: "100%", fontFamily: "IBM Plex Mono", fontSize: 12 }}
                />
              </div>
            </>
          )}

          <div className="field">
            <label className="field-label">on_failure</label>
            <select
              className="select"
              value={spec.on_failure || "fail_fast"}
              onChange={(e) => updateSpec(i, { on_failure: e.target.value })}
              style={{ width: "100%" }}
            >
              {GR_ON_FAILURE_OPTS.map((opt) => (
                <option key={opt} value={opt}>{opt}</option>
              ))}
            </select>
          </div>
        </div>
      ))}
      <div>
        <Btn size="sm" kind="ghost" icon="plus" onClick={addSpec}>
          Spec
        </Btn>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------------
// GR_ToolCallForm — ToolCall node side-panel form.
// Fetches the catalogue once via `useResource("graphs-editor:tools-catalogue")`
// — useResource is the editor's existing cache layer, so multiple ToolCall
// nodes selected in turn all share one fetch. Presents:
//   * a tool picker (dropdown of `<id> — <description>`),
//   * a key/value args editor (auto-seeded from the selected tool's
//     input_schema; type + required hints rendered next to each key),
//   * an "Advanced: use a single arguments_template" toggle (the
//     `arguments` map and `arguments_template` are mutually exclusive
//     server-side — Spec B §1.1 / _ToolCallNode), and
//   * an optional `output_schema` JSON field.
// On fetch failure (or empty catalogue) it falls back to raw text inputs.
// ----------------------------------------------------------------------------

function GR_ToolCallForm({ node, onUpdateNode, onReportJsonError, errBase }) {
  const { apiFetch, useResource } = window.primerApi;
  const catalogue = useResource(
    "graphs-editor:tools-catalogue",
    (s) => apiFetch("GET", "/tools/catalogue", null, { signal: s }),
    {},
  );
  const items = catalogue.data?.items || [];
  const itemById = React.useMemo(() => {
    const m = new Map();
    for (const it of items) m.set(it.id, it);
    return m;
  }, [items]);

  // `arguments` and `arguments_template` are mutually exclusive
  // server-side (Spec B §1.1). The toggle is driven off whether
  // arguments_template is currently a non-empty string.
  const usingTemplate = node.arguments_template != null && node.arguments_template !== "";
  const args = node.arguments && typeof node.arguments === "object" ? node.arguments : {};
  const argKeys = Object.keys(args);

  function pickTool(toolId) {
    const tool = itemById.get(toolId);
    const patch = { tool_id: toolId };
    if (tool && tool.input_schema && tool.input_schema.properties) {
      // Seed `arguments` with one empty entry per declared property,
      // preserving any existing values the operator already entered.
      const seeded = { ...args };
      for (const k of Object.keys(tool.input_schema.properties)) {
        if (!(k in seeded)) seeded[k] = "";
      }
      patch.arguments = seeded;
    }
    onUpdateNode(patch);
  }

  function updateArg(key, value) {
    onUpdateNode({ arguments: { ...args, [key]: value } });
  }
  function renameArg(oldKey, newKey) {
    if (newKey === oldKey) return;
    if (!newKey) return;
    const next = {};
    for (const k of argKeys) {
      next[k === oldKey ? newKey : k] = args[k];
    }
    onUpdateNode({ arguments: next });
  }
  function deleteArg(key) {
    const next = { ...args };
    delete next[key];
    onUpdateNode({ arguments: next });
  }
  function addArg() {
    let n = 1;
    let candidate = "arg";
    while (candidate in args) {
      n += 1;
      candidate = `arg${n}`;
    }
    onUpdateNode({ arguments: { ...args, [candidate]: "" } });
  }
  function toggleTemplate(on) {
    if (on) {
      // Switch to template mode: clear the literal args map, seed
      // arguments_template with the current literal args as JSON so
      // the operator has something to edit.
      const seed = argKeys.length ? JSON.stringify(args, null, 2) : "{}";
      onUpdateNode({ arguments: {}, arguments_template: seed });
    } else {
      onUpdateNode({ arguments_template: null });
    }
  }

  const selectedTool = node.tool_id ? itemById.get(node.tool_id) : null;
  const schemaProps = selectedTool?.input_schema?.properties || {};
  const requiredSet = new Set(selectedTool?.input_schema?.required || []);

  return (
    <div className="col" style={{ gap: 10 }}>
      {/* Tool picker */}
      <div className="field">
        <label className="field-label">tool_id</label>
        {catalogue.loading && !catalogue.data && (
          <div className="muted text-sm">Loading catalogue…</div>
        )}
        {catalogue.error && (
          <div className="field-help" style={{ color: "var(--amber)" }}>
            Catalogue unavailable ({catalogue.error.title || catalogue.error.message});
            type the scoped tool id manually below.
          </div>
        )}
        {!catalogue.error && items.length > 0 ? (
          <select
            className="select"
            value={node.tool_id || ""}
            onChange={(e) => pickTool(e.target.value)}
            style={{ width: "100%" }}
          >
            <option value="">— pick a tool —</option>
            {items.map((it) => (
              <option key={it.id} value={it.id}>
                {it.id}{it.description ? ` — ${it.description.slice(0, 60)}` : ""}
              </option>
            ))}
          </select>
        ) : (
          <input
            className="input mono"
            value={node.tool_id || ""}
            onChange={(e) => onUpdateNode({ tool_id: e.target.value })}
            placeholder="toolset__tool_name"
            style={{ width: "100%", fontFamily: "IBM Plex Mono", fontSize: 12 }}
          />
        )}
        {selectedTool && selectedTool.description && (
          <div className="field-help muted">{selectedTool.description}</div>
        )}
      </div>

      {/* Advanced toggle: literal args vs single template */}
      <label className="mono text-sm" style={{
        display: "flex", alignItems: "center", gap: 6, cursor: "pointer",
      }}>
        <input
          type="checkbox"
          checked={usingTemplate}
          onChange={(e) => toggleTemplate(e.target.checked)}
        />
        <span>Advanced: use a single arguments_template (Jinja → JSON)</span>
      </label>

      {!usingTemplate && (
        <div className="field">
          <label className="field-label">arguments</label>
          {argKeys.length === 0 && (
            <div className="muted text-sm">— no arguments —</div>
          )}
          {argKeys.map((k) => {
            const propType = schemaProps[k]?.type;
            const required = requiredSet.has(k);
            return (
              <div key={k} style={{
                border: "1px solid var(--border)",
                borderRadius: 4,
                padding: 6,
                marginBottom: 6,
                background: "var(--bg-1)",
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 4 }}>
                  <input
                    className="input mono"
                    value={k}
                    onChange={(e) => renameArg(k, e.target.value)}
                    style={{ flex: 1, fontFamily: "IBM Plex Mono", fontSize: 11.5 }}
                  />
                  {propType && (
                    <span className="muted text-sm" style={{ fontSize: 10.5 }}>
                      {propType}{required ? " *" : ""}
                    </span>
                  )}
                  <a
                    onClick={() => deleteArg(k)}
                    style={{ cursor: "pointer", color: "var(--red)", fontSize: 12 }}
                    title="Delete arg"
                  >
                    ×
                  </a>
                </div>
                <textarea
                  className="textarea mono"
                  rows={2}
                  value={typeof args[k] === "string" ? args[k] : JSON.stringify(args[k])}
                  onChange={(e) => updateArg(k, e.target.value)}
                  placeholder="value (Jinja-templated string)"
                  style={{ width: "100%", fontFamily: "IBM Plex Mono", fontSize: 11.5 }}
                />
              </div>
            );
          })}
          <Btn size="sm" kind="ghost" icon="plus" onClick={addArg}>Arg</Btn>
        </div>
      )}

      {usingTemplate && (
        <GR_TextAreaField
          label="arguments_template (Jinja2 → JSON)"
          value={node.arguments_template || ""}
          onChange={(v) => onUpdateNode({ arguments_template: v })}
          rows={6}
          placeholder={"{\n  \"query\": \"{{ graph_input.q }}\"\n}"}
          help="When set, shadows the literal arguments map. Must render to a JSON object."
        />
      )}

      <GR_JsonField
        label="output_schema"
        value={node.output_schema}
        onChange={(v) => onUpdateNode({ output_schema: v })}
        onError={onReportJsonError}
        errorKey={`${errBase}:output_schema`}
        help="Optional JSON Schema validated against the tool result."
      />
    </div>
  );
}

// Edge form — static edges show a target picker; conditional edges
// render the full BranchEditor (operator-based predicates + default_to).
function GR_SelectedEdgeForm({ edge, edgeIdx, nodes, onUpdateEdge, onDeleteEdge }) {
  const nodeIds = nodes.map((n) => n.id);
  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>
        {edge.kind} edge
      </div>
      <div className="mono text-sm">
        <span className="muted">from</span> {edge.from_node}
      </div>

      {edge.kind === "static" && (
        <div className="field">
          <label className="field-label">to_node</label>
          <select
            className="select"
            value={edge.to_node || ""}
            onChange={(e) => onUpdateEdge(edgeIdx, { ...edge, to_node: e.target.value })}
            style={{ width: "100%" }}
          >
            <option value="">— pick a target —</option>
            {nodeIds.map((id) => (
              <option key={id} value={id}>{id}</option>
            ))}
          </select>
        </div>
      )}

      {edge.kind === "conditional" && (edge.router?.kind === "json_path") && (
        <GR_BranchEditor
          edge={edge}
          nodeIds={nodeIds}
          onChange={(nextEdge) => onUpdateEdge(edgeIdx, nextEdge)}
        />
      )}

      <Btn size="sm" kind="danger" icon="trash" onClick={onDeleteEdge}>Delete edge</Btn>
    </div>
  );
}

// ----------------------------------------------------------------------------
// GR_BranchEditor — conditional-edge json_path router editor.
// Each branch: a conditions sub-table (path / op / value) + target dropdown.
// Operators (BranchCondition.op): eq, ne, gt, gte, lt, lte, in, not_in, exists.
// ----------------------------------------------------------------------------

const GR_BRANCH_OPS = ["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "exists"];

function GR_parseBranchValue(text, op) {
  if (op === "in" || op === "not_in") {
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) return parsed;
      return [parsed];
    } catch {
      return text.split(",").map((s) => s.trim()).filter((s) => s.length > 0);
    }
  }
  try { return JSON.parse(text); } catch { return text; }
}

function GR_formatBranchValue(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value) || typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function GR_BranchEditor({ edge, nodeIds, onChange }) {
  const router = edge.router || { kind: "json_path", branches: [], default_to: null };
  const branches = router.branches || [];

  function setBranches(next) {
    onChange({ ...edge, router: { ...router, branches: next } });
  }
  function setDefaultTo(v) {
    onChange({ ...edge, router: { ...router, default_to: v || null } });
  }
  function updateCondition(branchIdx, condIdx, patch) {
    const next = branches.map((b, i) => {
      if (i !== branchIdx) return b;
      const conds = (b.conditions || []).map((c, j) => j === condIdx ? { ...c, ...patch } : c);
      return { ...b, conditions: conds };
    });
    setBranches(next);
  }
  function deleteCondition(branchIdx, condIdx) {
    const next = branches.map((b, i) => {
      if (i !== branchIdx) return b;
      return { ...b, conditions: (b.conditions || []).filter((_, j) => j !== condIdx) };
    });
    setBranches(next);
  }
  function addCondition(branchIdx) {
    const next = branches.map((b, i) => {
      if (i !== branchIdx) return b;
      return { ...b, conditions: [...(b.conditions || []), { path: "", op: "eq", value: "" }] };
    });
    setBranches(next);
  }
  function setBranchTarget(branchIdx, to_node) {
    setBranches(branches.map((b, i) => i === branchIdx ? { ...b, to_node } : b));
  }
  function deleteBranch(branchIdx) {
    setBranches(branches.filter((_, i) => i !== branchIdx));
  }
  function addBranch() {
    setBranches([...branches, { conditions: [], to_node: "" }]);
  }

  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>
        branches ({branches.length})
      </div>
      {branches.length === 0 && <div className="muted text-sm">— no branches —</div>}
      {branches.map((b, i) => (
        <div
          key={i}
          className="branch-card"
          style={{
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: 8,
            background: "var(--bg-1)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
            <span className="muted text-sm mono">branch {i + 1}</span>
            <a
              onClick={() => deleteBranch(i)}
              style={{ cursor: "pointer", color: "var(--red)", fontSize: 12 }}
              title="Delete branch"
            >
              × delete
            </a>
          </div>
          <table className="conds" style={{ width: "100%", borderCollapse: "collapse", fontSize: 11.5 }}>
            <tbody>
              {(b.conditions || []).map((c, j) => (
                <tr key={j}>
                  <td style={{ paddingRight: 4, paddingBottom: 4 }}>
                    <input
                      className="input"
                      value={c.path || ""}
                      placeholder="$.path"
                      onChange={(e) => updateCondition(i, j, { path: e.target.value })}
                      style={{ width: "100%", fontFamily: "IBM Plex Mono", fontSize: 11.5 }}
                    />
                  </td>
                  <td style={{ paddingRight: 4, paddingBottom: 4, width: 80 }}>
                    <select
                      className="select"
                      value={c.op || "eq"}
                      onChange={(e) => {
                        const nextOp = e.target.value;
                        const patch = { op: nextOp };
                        if (nextOp === "exists") patch.value = null;
                        updateCondition(i, j, patch);
                      }}
                      style={{ width: "100%", fontSize: 11.5 }}
                    >
                      {GR_BRANCH_OPS.map((op) => (
                        <option key={op} value={op}>{op}</option>
                      ))}
                    </select>
                  </td>
                  {c.op !== "exists" ? (
                    <td style={{ paddingRight: 4, paddingBottom: 4 }}>
                      <input
                        className="input"
                        value={GR_formatBranchValue(c.value)}
                        placeholder={c.op === "in" || c.op === "not_in" ? "a,b,c or [\"a\",\"b\"]" : "value"}
                        onChange={(e) => updateCondition(i, j, { value: GR_parseBranchValue(e.target.value, c.op) })}
                        style={{ width: "100%", fontFamily: "IBM Plex Mono", fontSize: 11.5 }}
                      />
                    </td>
                  ) : (
                    <td style={{ paddingRight: 4, paddingBottom: 4 }}>
                      <span className="muted text-sm">(presence only)</span>
                    </td>
                  )}
                  <td style={{ paddingBottom: 4, width: 20 }}>
                    <a
                      onClick={() => deleteCondition(i, j)}
                      style={{ cursor: "pointer", color: "var(--red)" }}
                      title="Delete condition"
                    >
                      ×
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
            <Btn size="sm" kind="ghost" icon="plus" onClick={() => addCondition(i)}>
              Condition
            </Btn>
          </div>
          <div className="field" style={{ marginTop: 8 }}>
            <label className="field-label">to_node</label>
            <select
              className="select"
              value={b.to_node || ""}
              onChange={(e) => setBranchTarget(i, e.target.value)}
              style={{ width: "100%" }}
            >
              <option value="">— pick a target —</option>
              {nodeIds.map((id) => (
                <option key={id} value={id}>{id}</option>
              ))}
            </select>
          </div>
        </div>
      ))}
      <div>
        <Btn size="sm" kind="ghost" icon="plus" onClick={addBranch}>
          Branch
        </Btn>
      </div>
      <div className="field">
        <label className="field-label">default_to (when no branch matches)</label>
        <select
          className="select"
          value={router.default_to || ""}
          onChange={(e) => setDefaultTo(e.target.value)}
          style={{ width: "100%" }}
        >
          <option value="">— none —</option>
          {nodeIds.map((id) => (
            <option key={id} value={id}>{id}</option>
          ))}
        </select>
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
