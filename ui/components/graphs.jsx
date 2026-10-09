/* global React, Icon, Btn, Banner, Modal, CardList, Card, Fab, relativeTime */
// Task 7 (UI reconciliation Phase 2):
// Wires GraphsPage + GraphDetail to the real API. The detail body is
// the graph builder (graph-builder/*.jsx); this file keeps the list, the
// create modal, the status panel and the few pieces the builder borrows
// (GR_JsonField, GR_ImportSpecModal, GR_parseBranchValue, GR_stripCoords).
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
//     (list row, breadcrumb-back) go through
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
  const [creatingAgent, setCreatingAgent] = React.useState(false);

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
    // The seed agent is OPTIONAL. When one is chosen, seed a minimal
    // runnable skeleton (Begin → agent → End wired by static edges) so
    // the new graph runs out of the box. When none is chosen, create an
    // EMPTY graph — it persists fine, but isn't runnable until it has a
    // Begin → … → End (runnability is enforced at session-start, not at
    // creation).
    const skeleton = seedAgentId
      ? {
          nodes: [
            { kind: "begin", id: "begin" },
            { kind: "agent", id: "start", agent_id: seedAgentId },
            { kind: "end", id: "end", output_template: "" },
          ],
          edges: [
            { kind: "static", from_node: "begin", to_node: "start" },
            { kind: "static", from_node: "start", to_node: "end" },
          ],
        }
      : { nodes: [], edges: [] };
    const body = {
      ...(id ? { id } : {}),
      description: description.trim(),
      ...skeleton,
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
            disabled={create.loading}
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
            optional · with one, the graph starts as Begin → agent → End;
            without, it's created empty (not runnable until it has a
            Begin → … → End)
          </span>
        </label>
        <div style={{ display: "flex", gap: 6 }}>
          <select
            className="select"
            value={seedAgentId}
            onChange={(e) => setSeedAgentId(e.target.value)}
            style={{ flex: 1, minWidth: 0 }}
          >
            <option value="">-- pick an agent --</option>
            {(agents.data?.items ?? []).map((a) => (
              <option key={a.id} value={a.id}>{a.id}</option>
            ))}
          </select>
          {typeof window.AG_NewAgentModal === "function" && (
            <Btn
              size="sm"
              kind="ghost"
              icon="plus"
              onClick={() => setCreatingAgent(true)}
              title="Create a new agent without leaving this dialog"
            >
              New
            </Btn>
          )}
        </div>
        {(agents.data?.items ?? []).length === 0 && !agents.loading && (
          <div className="field-help" style={{ color: "var(--amber)" }}>
            No agents configured.
            {typeof window.AG_NewAgentModal === "function" ? (
              <>
                {" "}
                <a
                  onClick={() => setCreatingAgent(true)}
                  style={{ color: "var(--accent)", cursor: "pointer", textDecoration: "underline" }}
                >
                  Create one inline
                </a>{" "}
                — no need to leave this dialog.
              </>
            ) : (
              <> Create one at <span className="mono">/agents</span> first.</>
            )}
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

      {creatingAgent && typeof window.AG_NewAgentModal === "function" && (
        <window.AG_NewAgentModal
          onClose={() => setCreatingAgent(false)}
          pushToast={pushToast}
          onCreate={(row) => {
            setCreatingAgent(false);
            agents.refetch();
            setSeedAgentId(row.id);
            if (typeof pushToast === "function") {
              pushToast({
                kind: "success",
                title: "Agent created",
                detail: `${row.id} — selected as the seed for this graph`,
              });
            }
          }}
        />
      )}
    </Modal>
  );
}

// ============================================================================
// GraphsPage — list, wired to /graphs?limit=200
// ============================================================================

function GraphsPage({ onOpen, pushToast, startCreate }) {
  const { useViewport, usePagedList, Pager } = window.primerApi;
  const apiFetch = window.primerApi.apiFetch;
  const { isMobile } = useViewport();
  const [textFilter, setTextFilter] = React.useState("");
  // Server-side offset pagination (bug #19); filter narrows the current page.
  const list = usePagedList({
    key: "graphs:list",
    path: "/graphs",
    pageSize: 50,
    resetKey: textFilter,
  });

  const items = list.items;
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
  // "New graph" from the platform page opens the form DIRECTLY.
  React.useEffect(() => {
    if (startCreate) setCreateOpen(true);
  }, [startCreate]);

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            aria-label="Filter graphs"
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
                  subtitle={g.description || "No description"}
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

      <Pager pager={list} label="graphs" />

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
// GraphDetail - Designer shell; body is the graph builder + status panel
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
      <window.GB_Builder
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
          {status.error && (
            <div className="muted text-sm">
              <span style={{ color: "var(--red)" }}>{status.error.title || status.error.message}</span>
            </div>
          )}
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

// GR_ImportSpecModal - raw graph-spec paste/import escape hatch (GAP-6).
// Pre-fills the textarea with the current draft (coords stripped, same
// body shape onSave PUTs) so it doubles as an export/edit surface, and
// hands the parsed object to onApply, which validates the shape and
// replaces the editor draft. Parse + shape errors render inline rather
// than crashing the editor.
function GR_ImportSpecModal({ currentDraft, onClose, onApply }) {
  const _seedText = React.useMemo(() => {
    if (!currentDraft) return "";
    const body = {
      id: currentDraft.id,
      description: currentDraft.description,
      nodes: (currentDraft.nodes || []).map(GR_stripCoords),
      edges: (currentDraft.edges || []).map((e) => ({ ...e })),
      ...(currentDraft.max_iterations != null
        ? { max_iterations: currentDraft.max_iterations }
        : {}),
    };
    return JSON.stringify(body, null, 2);
  }, [currentDraft]);

  const [text, setText] = React.useState(_seedText);
  const [error, setError] = React.useState(null);

  const apply = () => {
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch (e) {
      setError("JSON parse: " + String(e.message || e));
      return;
    }
    try {
      onApply(parsed);  // throws a string on a shape problem
    } catch (e) {
      setError(typeof e === "string" ? e : String(e.message || e));
    }
  };

  return (
    <Modal
      title="Import graph spec"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="check" onClick={apply}>Load into editor</Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">
          Graph spec JSON <span className="hint">same shape as PUT /graphs/{"{id}"}</span>
        </label>
        <textarea
          className="textarea mono"
          rows={18}
          value={text}
          onChange={(e) => { setText(e.target.value); if (error) setError(null); }}
          placeholder={'{\n  "nodes": [ ... ],\n  "edges": [ ... ]\n}'}
          style={{ width: "100%", fontFamily: "IBM Plex Mono", fontSize: 12 }}
          data-testid="graph-import-spec"
        />
        <div className="field-help muted">
          Loads the pasted spec into the visual editor (the graph id stays the editor's;
          a pasted id is ignored so Save can't retarget another graph). Nothing is persisted
          until you press Save. Coordinates are auto-laid-out on load.
        </div>
        {error && (
          <div className="field-help" style={{ color: "var(--red)" }}>{error}</div>
        )}
      </div>
    </Modal>
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

// A branch condition's typed text as the value the router compares with: a
// list for in / not_in (JSON array, else comma-separated), else JSON, else the
// text itself. Used by the builder's branch editor (graph-builder/gb-branches.jsx).
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

// Helper: strip UI-only x/y before PUTting back to the server.
function GR_stripCoords(node) {
  const { x, y, ...rest } = node;
  return rest;
}

// Export to global scope. Designer's app.jsx looks up GraphsPage +
// GraphDetail off window.
Object.assign(window, { GraphsPage, GraphDetail, GR_NewGraphModal });
