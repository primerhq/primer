/* global React, Btn, Banner, GB_reducer, GB_stripAll, GB_validate, GB_api, GB_Canvas,
   GB_Outline, GB_Inspector, GB_AddStepPalette, GB_ReadinessChip, GB_ReadinessPopover,
   GB_DryRunDrawer, GB_Starters, GB_makeNode, GR_ImportSpecModal, GR_stripCoords */
// GB_Builder - the graph builder shell. Owns the draft (same staged-local +
// PUT-replace model as the old editor), composes the outline / canvas /
// inspector, and hosts the palette, readiness popover and dry-run drawer.
// WIRING.md §3, §4, §12, §13.

function GB_Builder(props) {
  const { graphId, loaded, onSaved, onRefresh, pushToast, runId } = props;
  const { useState, useMemo, useReducer, useRef, useEffect, useCallback } = React;
  const { useResource, useMutation } = window.primerApi;

  const readOnly = !!(loaded && loaded.harness_id);

  const seed = useMemo(() => ({
    ...(loaded || {}),
    nodes: ((loaded || {}).nodes || []).map((n) => ({ ...n })),
    edges: ((loaded || {}).edges || []).map((e) => ({ ...e })),
  }), [loaded]);

  const [draft, rawDispatch] = useReducer(GB_reducer, seed);
  const [selectedId, setSelectedId] = useState(null);
  const [selectedEdge, setSelectedEdge] = useState(null);
  const [paletteAfter, setPaletteAfter] = useState(undefined); // undefined = closed
  const [readyOpen, setReadyOpen] = useState(false);
  const [dryOpen, setDryOpen] = useState(false);
  const [dryResult, setDryResult] = useState(null);
  const [dryLoading, setDryLoading] = useState(false);
  const [sampleInput, setSampleInput] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [showBands, setShowBands] = useState(true);
  const [addEdgeMode, setAddEdgeMode] = useState(null);
  const [layoutNonce, setLayoutNonce] = useState(0);
  const [jsonErrors, setJsonErrors] = useState({});

  // Undo stack - cheap because the reducer is pure and drafts are small.
  const undoRef = useRef([]);
  const redoRef = useRef([]);
  // The live draft, mirrored into a ref so the undo stack and the keyboard
  // handlers can read the current state without re-creating callbacks.
  const draftRef = useRef(draft);
  useEffect(() => { draftRef.current = draft; }, [draft]);

  // NOTE: useReducer's dispatch takes an ACTION, not an updater function
  // (that is useState). Passing a function here would hand GB_reducer a
  // function as its action, whose `.type` is undefined, so every edit would
  // fall through to the reducer's default branch and silently do nothing.
  // Undo bookkeeping therefore happens here, around a plain action dispatch.
  const dispatch = useCallback((action) => {
    if (readOnly) return;
    const prev = draftRef.current;
    if (action && action.type !== "SET_DRAFT" && GB_reducer(prev, action) !== prev) {
      undoRef.current = [...undoRef.current.slice(-49), prev];
      redoRef.current = [];
    }
    rawDispatch(action);
  }, [readOnly]);

  useEffect(() => { rawDispatch({ type: "SET_DRAFT", draft: seed }); undoRef.current = []; redoRef.current = []; }, [seed]);

  useEffect(() => {
    const onKey = (e) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); setPaletteAfter(selectedId || null); }
      if (mod && e.key.toLowerCase() === "z" && !e.shiftKey && undoRef.current.length) {
        e.preventDefault();
        const prev = undoRef.current[undoRef.current.length - 1];
        undoRef.current = undoRef.current.slice(0, -1);
        redoRef.current = [...redoRef.current, draftRef.current];
        rawDispatch({ type: "SET_DRAFT", draft: prev });
      }
      if (mod && e.key.toLowerCase() === "z" && e.shiftKey && redoRef.current.length) {
        e.preventDefault();
        const next = redoRef.current[redoRef.current.length - 1];
        redoRef.current = redoRef.current.slice(0, -1);
        undoRef.current = [...undoRef.current, draftRef.current];
        rawDispatch({ type: "SET_DRAFT", draft: next });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId]);

  const dirty = useMemo(
    () => JSON.stringify(GB_stripAll(draft)) !== JSON.stringify(GB_stripAll(seed)),
    [draft, seed],
  );

  const toolsRes = useResource("tools:catalogue", (signal) => GB_api.toolCatalogue(signal), { pollMs: 0 });
  const tools = (toolsRes.data && toolsRes.data.items) || [];
  const statusRes = useResource(
    `graph:status:${graphId}`,
    (signal) => GB_api.graphStatus(graphId, signal),
    { pollMs: 0 },
  );
  const serverIssues = (statusRes.data && statusRes.data.issues) || [];

  const runStates = useResource(
    runId ? `graph:runstates:${graphId}:${runId}` : null,
    (signal) => GB_api.nodeStates(graphId, runId, signal),
    { pollMs: runId ? 1500 : 0 },
  );
  const statusTint = useMemo(() => {
    const items = (runStates.data && runStates.data.items) || [];
    const out = {};
    for (const it of items) out[it.node_id] = it.status;
    return Object.keys(out).length ? out : null;
  }, [runStates.data]);

  const validation = useMemo(
    () => GB_validate(draft, { knownToolIds: tools.map((t) => t.id) }),
    [draft, tools],
  );
  const problemsByNode = useMemo(() => {
    const out = {};
    for (const r of [...validation.blocking, ...validation.runnable]) {
      if (!r.nodeId) continue;
      (out[r.nodeId] = out[r.nodeId] || []).push(r);
    }
    return out;
  }, [validation]);

  const hasJsonError = Object.keys(jsonErrors).some((k) => jsonErrors[k]);
  const canSave = dirty && !validation.blocking.length && !hasJsonError && !readOnly;
  const canRun = !validation.blocking.length && !validation.runnable.length;

  const save = useMutation(
    () => GB_api.putGraph(graphId, { ...draft, nodes: (draft.nodes || []).map(GR_stripCoords) }),
    {
      invalidates: ["graphs:list", `graph:${graphId}`],
      // Same toast the console uses everywhere else, including which graph it
      // was - a revamp is no reason to invent different copy for a save.
      onSuccess: () => {
        if (onSaved) onSaved();
        if (pushToast) pushToast({ kind: "success", title: "Graph saved", detail: graphId });
      },
    },
  );

  const selectedNode = (draft.nodes || []).find((n) => n.id === selectedId) || null;

  const applyFix = (row) => {
    if (row.fix === "add_response_format" && row.nodeId) {
      const paths = [];
      for (const e of draft.edges || []) {
        if (e.from_node !== row.nodeId || !e.router) continue;
        for (const b of e.router.branches || []) for (const c of b.conditions || []) {
          const head = String(c.path || "").split(".")[0];
          if (head && paths.indexOf(head) === -1) paths.push(head);
        }
      }
      const props2 = {};
      (paths.length ? paths : ["approved"]).forEach((p, i) => { props2[p] = { type: i === 0 ? "boolean" : "string" }; });
      dispatch({
        type: "UPDATE_NODE", id: row.nodeId,
        patch: { response_format: { type: "object", properties: props2, required: Object.keys(props2).slice(0, 1), additionalProperties: false } },
      });
      setSelectedId(row.nodeId);
    } else if (row.fix === "set_max_iterations") {
      dispatch({ type: "SET_GRAPH", patch: { max_iterations: draft.max_iterations || 3 } });
    } else if (row.fix === "add_catch_all" && row.edgeIdx != null) {
      const e = (draft.edges || [])[row.edgeIdx];
      const fallback = (draft.nodes || []).find((n) => n.kind === "end");
      if (e && fallback) {
        dispatch({ type: "UPDATE_EDGE", idx: row.edgeIdx, patch: { router: { ...e.router, default_to: fallback.id } } });
      }
    } else if (row.fix === "add_end") {
      const node = GB_makeNode({ kind: "end", label: "Finish", takenIds: (draft.nodes || []).map((n) => n.id) });
      dispatch({ type: "ADD_NODE", node });
      setSelectedId(node.id);
    } else if (row.fix === "add_begin") {
      const node = GB_makeNode({ kind: "begin", label: "Start", takenIds: (draft.nodes || []).map((n) => n.id) });
      dispatch({ type: "ADD_NODE", node });
      setSelectedId(node.id);
    } else if (row.nodeId) {
      setSelectedId(row.nodeId);
    }
    setReadyOpen(false);
  };

  const runDryRun = async () => {
    setDryLoading(true);
    try {
      let parsed = sampleInput;
      try { parsed = JSON.parse(sampleInput); } catch (_e) { /* plain string is fine */ }
      const res = await GB_api.dryRun(graphId, draft, parsed);
      // Label rows with the human step name.
      const byId = {};
      for (const n of draft.nodes || []) byId[n.id] = n;
      res.nodes = (res.nodes || []).map((r) => ({ ...r, label: (byId[r.node_id] || {}).description || r.node_id }));
      setDryResult(res);
    } catch (err) {
      if (pushToast) pushToast({ title: "Dry run failed", detail: err.detail || err.message, kind: "error" });
    } finally {
      setDryLoading(false);
    }
  };

  const openDryRun = () => { setDryOpen(true); if (!dryResult) runDryRun(); };

  const onCreateFromPalette = ({ nodes, edges, connectFrom, connectTo }) => {
    const allEdges = [...(edges || [])];
    if (connectFrom) {
      allEdges.push({ kind: "static", from_node: connectFrom, to_node: connectTo || nodes[0].id });
    }
    dispatch({ type: "ADD_NODES", nodes, edges: allEdges });
    setSelectedId(nodes[0].id);
    setLayoutNonce((n) => n + 1);
  };

  // An empty graph offers the six shapes instead of a blank canvas.
  const isEmpty = !(draft.nodes || []).length;

  return (
    <div className="col" data-testid="gb-builder" style={{ gap: 0, border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden", background: "var(--bg-1)" }}>
      {/* Top bar */}
      <div
        data-testid="gb-topbar"
        className="row"
        style={{ height: 52, flex: "0 0 auto", gap: 12, alignItems: "center", padding: "0 14px", borderBottom: "1px solid var(--border)", background: "var(--bg-elev)", position: "relative" }}
      >
        <div className="row mono" style={{ gap: 8, alignItems: "center", fontSize: "var(--fs-12)", color: "var(--text-3)", minWidth: 0 }}>
          <span>graphs</span>
          <span style={{ color: "var(--border-strong)" }}>/</span>
          <span style={{ color: "var(--text)", fontFamily: "inherit", fontSize: "var(--fs-13)", fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {draft.description || graphId}
          </span>
          {dirty ? <span data-testid="gb-dirty" title="unsaved changes" style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--amber)", flex: "0 0 auto" }} /> : null}
        </div>

        <div className="row" style={{ marginLeft: "auto", gap: 8, alignItems: "center" }}>
          {!readOnly ? (
            <GB_ReadinessChip
              validation={validation}
              serverIssues={serverIssues}
              open={readyOpen}
              onToggle={() => setReadyOpen(!readyOpen)}
            />
          ) : null}
          <Btn size="sm" kind="ghost" onClick={openDryRun}>Dry run</Btn>
          <Btn size="sm" kind="ghost" data-testid="gb-json-tab" onClick={() => setImportOpen(true)}>JSON</Btn>
          {!readOnly ? (
            <Btn
              size="sm"
              kind="ghost"
              data-testid="gb-discard"
              disabled={!dirty || save.loading}
              onClick={() => {
                // Throw away every staged edit and go back to the last saved
                // state. Uses rawDispatch + clears the undo/redo stacks (the
                // same reset the seed effect performs) so discarded work can't
                // be half-restored with Cmd-Z.
                rawDispatch({ type: "SET_DRAFT", draft: seed });
                undoRef.current = [];
                redoRef.current = [];
                setSelectedId(null);
                setSelectedEdge(null);
                setLayoutNonce((n) => n + 1);
              }}
            >
              Discard
            </Btn>
          ) : null}
          {!readOnly ? (
            <Btn
              size="sm"
              data-testid="gb-save"
              disabled={!canSave || save.loading}
              onClick={() => save.mutate()}
            >
              {save.loading ? "Saving…" : "Save draft"}
            </Btn>
          ) : null}
        </div>

        {readyOpen ? (
          <GB_ReadinessPopover
            validation={validation}
            serverIssues={serverIssues}
            draft={draft}
            onFix={applyFix}
            onSelectNode={(id) => { setSelectedId(id); setReadyOpen(false); }}
            onClose={() => setReadyOpen(false)}
          />
        ) : null}
      </div>

      {readOnly ? (
        <Banner kind="warning">
          managed by harness <span className="mono">{loaded.harness_id}</span> - direct edits are blocked; update the harness instead.
        </Banner>
      ) : null}

      {isEmpty && !readOnly ? (
        <GB_Starters
          tools={tools}
          onBlank={() => {
            const begin = GB_makeNode({ kind: "begin", label: "Start", takenIds: [] });
            const end = GB_makeNode({ kind: "end", label: "Finish", takenIds: [begin.id] });
            dispatch({ type: "ADD_NODES", nodes: [begin, end], edges: [] });
          }}
          onApply={(spec) => { dispatch({ type: "APPLY_TEMPLATE", spec }); setLayoutNonce((n) => n + 1); }}
        />
      ) : (
        <div className="row" style={{ flex: 1, minHeight: 520, height: 620 }}>
          {/* Left rail */}
          <div className="col" style={{ width: 252, flex: "0 0 auto", borderRight: "1px solid var(--border)", background: "var(--bg-1)", minHeight: 0 }}>
            <div className="row" style={{ gap: 8, alignItems: "center", padding: "12px 14px 8px" }}>
              <div style={{ fontSize: 10.5, fontWeight: 600, letterSpacing: ".08em", color: "var(--text-3)", textTransform: "uppercase" }}>Steps</div>
              <span
                onClick={() => setShowBands(!showBands)}
                title="Show the order steps run in"
                style={{ marginLeft: "auto", fontSize: 10.5, color: showBands ? "var(--accent)" : "var(--text-4)", cursor: "pointer" }}
              >
                bands
              </span>
            </div>
            <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
              <GB_Outline
                draft={draft}
                selectedId={selectedId}
                onSelect={(id) => { setSelectedId(id); setSelectedEdge(null); }}
                problemsByNode={problemsByNode}
                runStates={statusTint}
              />
            </div>
            {!readOnly ? (
              <div className="col" style={{ gap: 6, marginTop: "auto", padding: 10, borderTop: "1px solid var(--border)" }}>
                <button
                  type="button"
                  data-testid="gb-outline-add"
                  onClick={() => setPaletteAfter(selectedId || null)}
                  style={{
                    display: "flex", alignItems: "center", justifyContent: "center", gap: 7, padding: 9,
                    borderRadius: 9, background: "var(--bg-2)", border: "1px dashed var(--border-strong)",
                    color: "var(--text-2)", fontSize: "var(--fs-12)", cursor: "pointer", width: "100%",
                  }}
                >
                  + Add a step <span className="mono" style={{ fontSize: 10, color: "var(--text-3)" }}>⌘K</span>
                </button>
                <div className="muted" style={{ fontSize: 10.5, padding: "0 2px 2px" }}>
                  Every step is created finished - pick what it should do first, and it gets a real name.
                </div>
              </div>
            ) : null}
          </div>

          {/* Canvas */}
          <div style={{ flex: 1, minWidth: 0, position: "relative", background: "var(--bg-2)" }}>
            <GB_Canvas
              draft={draft}
              layout="lr"
              showBands={showBands}
              selectedNodeId={selectedId}
              selectedEdgeId={selectedEdge}
              statusTint={statusTint}
              layoutNonce={layoutNonce}
              addEdgeMode={addEdgeMode}
              onNodeClick={(id) => { setSelectedId(id); setSelectedEdge(null); }}
              onEdgeClick={(idx) => { if (idx != null && idx >= 0) { setSelectedEdge(idx); setSelectedId(null); } }}
              onBackgroundClick={() => { setSelectedId(null); setSelectedEdge(null); }}
              onMoveNode={(id, x, y) => dispatch({ type: "MOVE_NODE", id, x, y })}
              onConnect={(from, to) => dispatch({ type: "ADD_EDGE", edge: { kind: "static", from_node: from, to_node: to } })}
              onIllegalEdge={(reason, nodeId) => {
                if (pushToast) pushToast({ title: "Can't connect that way", detail: reason });
                if (nodeId) setSelectedId(nodeId);
              }}
            />
            {!readOnly ? (
              <div className="row" style={{ position: "absolute", left: 12, bottom: 12, gap: 6 }}>
                <Btn
                  size="sm"
                  kind={addEdgeMode ? "primary" : "ghost"}
                  onClick={() => setAddEdgeMode(addEdgeMode ? null : {})}
                >
                  {addEdgeMode ? "Connecting… (click a step, then another)" : "Connect steps"}
                </Btn>
                <Btn size="sm" kind="ghost" data-testid="gb-tidy" onClick={() => { dispatch({ type: "AUTO_LAYOUT" }); setLayoutNonce((n) => n + 1); }}>Tidy up</Btn>
              </div>
            ) : null}
          </div>

          {/* Inspector */}
          <div className="col" style={{ width: 400, flex: "0 0 auto", borderLeft: "1px solid var(--border)", background: "var(--bg-1)", minHeight: 0, overflow: "hidden" }}>
            <GB_Inspector
              draft={draft}
              node={selectedNode}
              edgeIdx={selectedEdge}
              dispatch={dispatch}
              tools={tools}
              readOnly={readOnly}
              problems={selectedId ? problemsByNode[selectedId] : null}
              onSelectNode={setSelectedId}
              onJsonError={(key, err) => setJsonErrors((s) => ({ ...s, [key]: err }))}
            />
          </div>
        </div>
      )}

      {dryOpen ? (
        <GB_DryRunDrawer
          result={dryResult}
          loading={dryLoading}
          sampleInput={sampleInput}
          onSampleInput={setSampleInput}
          onRecheck={runDryRun}
          onClose={() => setDryOpen(false)}
          onFix={applyFix}
          onSelectNode={setSelectedId}
          canRun={canRun}
          onRun={async () => {
            try {
              let parsed = sampleInput;
              try { parsed = JSON.parse(sampleInput); } catch (_e) { /* string */ }
              const s = await GB_api.startRun({ graphId, graphInput: sampleInput ? parsed : undefined });
              if (pushToast) pushToast({ title: "Run started", detail: s.id });
              if (onRefresh) onRefresh();
            } catch (err) {
              if (pushToast) pushToast({ title: "Couldn't start the run", detail: err.detail || err.message, kind: "error" });
            }
          }}
        />
      ) : null}

      {paletteAfter !== undefined ? (
        <GB_AddStepPalette
          draft={draft}
          afterNodeId={paletteAfter}
          tools={tools}
          onClose={() => setPaletteAfter(undefined)}
          onCreate={onCreateFromPalette}
          onAddBranch={(fromId) => {
            const from = fromId || (draft.nodes || [])[0];
            const fid = typeof from === "string" ? from : (from || {}).id;
            if (!fid) return;
            const target = (draft.nodes || []).find((n) => n.kind === "end");
            dispatch({
              type: "ADD_EDGE",
              edge: {
                kind: "conditional", from_node: fid,
                router: {
                  kind: "json_path",
                  branches: [{ conditions: [{ path: "", op: "eq", value: "" }], to_node: target ? target.id : "" }],
                  default_to: target ? target.id : null,
                },
              },
            });
            setSelectedEdge((draft.edges || []).length);
            setSelectedId(null);
          }}
        />
      ) : null}

      {importOpen && typeof GR_ImportSpecModal === "function" ? (
        <GR_ImportSpecModal
          currentDraft={draft}
          onClose={() => setImportOpen(false)}
          onApply={(spec) => { dispatch({ type: "IMPORT_SPEC", spec }); setImportOpen(false); setLayoutNonce((n) => n + 1); }}
        />
      ) : null}
    </div>
  );
}

window.GB_Builder = GB_Builder;
