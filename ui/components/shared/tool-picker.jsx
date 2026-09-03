/* global React, Icon, Btn, Banner */

// ToolPicker - the shared y/w/r/n tool picker (uiv2 Wave 2 synthesis:
// "build the shared tool picker component here first" - agent bindings
// today, graph tool nodes / approval policies / service grants / the MCP
// allowlist are the named future adopters per the mockup's own footnote,
// not migrated in this pass). Search, 6-per-page pager (mockup default;
// overridable), toolset-grouped bulk-select header, a "Selected" filter
// toggle, CapabilityBadges per row, and an expandable per-row disclosure
// showing the tool's input schema (uiv2 Wave 2 ruling a-8: the old
// separate read-view Tools tab superseded by disclosure on picker rows,
// not a standalone tab).
//
// ONE fetch serves everything: GET /tools already carries input_schema
// plus the y/w/r/n flags per tool (primer/api/routers/providers.py's
// list_all_tools/_catalogue_tools, tool_catalogue_flags()) - the old
// agent overlay's separate read-view Tools tab issued a SECOND fetch per
// toolset (GET /toolsets/{id}/tools) purely to get that same schema data
// under a different field name (`schema` there vs `input_schema` here).
// Folding the disclosure into this picker removes that redundant fetch
// entirely, not just relocates the UI.
//
// Controlled component: `selected` is a Set<scoped_id>, `onChange(next)`
// receives the new Set on every toggle - the caller owns persistence
// (agent.tools, a graph node's tool list, ...).
//
// `mode="single"` (uiv2 Wave 3, approved for the approval-policy Tool
// row - a policy gates exactly one tool): rows become radio-styled and
// picking one REPLACES the selection instead of adding to it, the
// toolset group header's bulk-select checkbox and the "selected · N"
// filter chip are hidden (neither makes sense with at most one pick).
// Default (no `mode`, or "multi") is byte-identical to before this was
// added - every existing multi-select consumer is unaffected.

function TP_toggleSet(set, id) {
  const next = new Set(set);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

function TP_Row({ tool, checked, onToggle, open, onToggleOpen, single }) {
  const hl = window.primerVendor?.highlightJson;
  return (
    <div style={{ borderTop: "1px solid var(--bg-1)" }}>
      <label
        style={{
          display: "flex", alignItems: "flex-start", gap: 8,
          padding: "6px 10px 6px 28px", cursor: "pointer",
          background: checked ? "var(--bg-2)" : "transparent",
        }}
        data-testid={`tool-picker-row-${tool.scoped_id}`}
      >
        <input
          type={single ? "radio" : "checkbox"}
          checked={checked}
          onChange={() => onToggle(tool.scoped_id)}
          style={{ marginTop: 3 }}
        />
        <button
          type="button"
          onClick={(e) => { e.preventDefault(); onToggleOpen(tool.scoped_id); }}
          data-testid={`tool-picker-expand-${tool.scoped_id}`}
          style={{ background: "none", border: "none", padding: 0, cursor: "pointer", marginTop: 2, color: "var(--text-3)" }}
          title={open ? "Hide schema" : "Show schema"}
        >
          <Icon name={open ? "chevron-down" : "chevron-right"} size={11} />
        </button>
        <div style={{ flex: 1, minWidth: 0, display: "flex", alignItems: "flex-start", gap: 8 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="mono" style={{ fontSize: 12 }}>{tool.id}</div>
            {tool.description && (
              <div className="muted text-sm" style={{ fontSize: 11, marginTop: 2, lineHeight: 1.4 }}>
                {tool.description}
              </div>
            )}
          </div>
          <window.primerApi.CapabilityBadges tool={tool} testid={`tool-picker-badges-${tool.scoped_id}`} />
        </div>
      </label>
      {open && (
        <div style={{ padding: "4px 14px 12px 46px" }}>
          {tool.input_schema && Object.keys(tool.input_schema).length ? (
            hl
              ? <div className="code-block" dangerouslySetInnerHTML={{ __html: hl(JSON.stringify(tool.input_schema, null, 2)) }} />
              : <pre className="code-block">{JSON.stringify(tool.input_schema, null, 2)}</pre>
          ) : (
            <div className="muted text-sm">No input parameters.</div>
          )}
        </div>
      )}
    </div>
  );
}

function ToolPicker({ selected, onChange, pageSize, mode }) {
  const { useResource, apiFetch } = window.primerApi;
  const single = mode === "single";
  const size = pageSize || 6;
  const catalogue = useResource(
    "tool-picker:catalogue",
    (signal) => apiFetch("GET", "/tools", null, { signal }),
    { pollMs: null }
  );
  const [filter, setFilter] = React.useState("");
  const [selectedOnly, setSelectedOnly] = React.useState(false);
  const [page, setPage] = React.useState(1);
  const [openId, setOpenId] = React.useState(null);

  const toolsetEntries = catalogue.data?.items ?? [];

  const allScopedIds = React.useMemo(() => {
    const s = new Set();
    for (const ts of toolsetEntries) {
      for (const t of ts.tools) s.add(t.scoped_id);
    }
    return s;
  }, [toolsetEntries]);
  // A selected id the catalogue no longer serves (toolset dropped the
  // tool, or went unavailable) - surfaced so the operator can clean it
  // up rather than silently persisting a dead reference on save.
  const staleIds = React.useMemo(
    () => [...selected].filter((id) => !allScopedIds.has(id)),
    [selected, allScopedIds],
  );

  const filteredToolsetEntries = React.useMemo(() => {
    const q = filter.trim().toLowerCase();
    let entries = toolsetEntries;
    if (q) {
      entries = entries
        .map((ts) => ({
          ...ts,
          tools: ts.tools.filter(
            (t) =>
              t.id.toLowerCase().includes(q) ||
              t.scoped_id.toLowerCase().includes(q) ||
              (t.description || "").toLowerCase().includes(q) ||
              ts.id.toLowerCase().includes(q),
          ),
        }))
        .filter((ts) => ts.tools.length > 0 || ts.id.toLowerCase().includes(q));
    }
    if (selectedOnly) {
      entries = entries
        .map((ts) => ({ ...ts, tools: ts.tools.filter((t) => selected.has(t.scoped_id)) }))
        .filter((ts) => ts.tools.length > 0);
    }
    return entries;
  }, [toolsetEntries, filter, selectedOnly, selected]);

  const totalAvailable = React.useMemo(
    () => toolsetEntries.reduce((acc, ts) => acc + ts.tools.length, 0),
    [toolsetEntries],
  );

  const flatTools = React.useMemo(() => {
    const out = [];
    for (const ts of filteredToolsetEntries) {
      if (!ts.available) continue;
      for (const tool of ts.tools) out.push({ ...tool, _toolset: ts });
    }
    return out;
  }, [filteredToolsetEntries]);
  const unavailableToolsets = React.useMemo(
    () => filteredToolsetEntries.filter((ts) => !ts.available),
    [filteredToolsetEntries],
  );

  const totalPages = Math.max(1, Math.ceil(flatTools.length / size));
  React.useEffect(() => { setPage(1); }, [filter, selectedOnly]);
  React.useEffect(() => { if (page > totalPages) setPage(totalPages); }, [page, totalPages]);
  const pageStart = (page - 1) * size;
  const pageEnd = Math.min(pageStart + size, flatTools.length);
  const pageTools = flatTools.slice(pageStart, pageEnd);

  const toggleId = (scopedId) =>
    onChange(single ? new Set([scopedId]) : TP_toggleSet(selected, scopedId));
  const toggleGroup = (entry, allSelected) => {
    let next = new Set(selected);
    for (const t of entry.tools) {
      if (allSelected) next.delete(t.scoped_id);
      else next.add(t.scoped_id);
    }
    onChange(next);
  };

  const selectedCount = selected.size;

  return (
    <div data-testid="tool-picker">
      <div style={{ marginBottom: 8, display: "flex", alignItems: "center", gap: 6 }}>
        <div className="input-icon" style={{ flex: 1 }}>
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Search the catalog…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            data-testid="tool-picker-filter"
            style={{ width: "100%" }}
          />
        </div>
        {!single && (
          <button
            type="button"
            data-testid="tool-picker-filter-selected"
            className={"chip" + (selectedOnly ? " active" : "")}
            aria-pressed={selectedOnly}
            onClick={() => setSelectedOnly((v) => !v)}
            title={selectedOnly ? "Show all tools" : "Show only selected tools"}
            style={{ whiteSpace: "nowrap" }}
          >
            selected · {selectedCount}
          </button>
        )}
      </div>
      {staleIds.length > 0 && (
        <div className="field-help" style={{ color: "var(--amber)", marginBottom: 6 }} data-testid="tool-picker-stale">
          {staleIds.length} selected tool{staleIds.length === 1 ? "" : "s"} no longer exposed by the catalogue: {staleIds.join(", ")}
        </div>
      )}
      {catalogue.error && (
        <Banner
          kind="error"
          title="Couldn't load the tool catalogue"
          detail={catalogue.error.detail || catalogue.error.title || catalogue.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={catalogue.refetch}>Retry</Btn>}
        />
      )}
      {catalogue.loading && toolsetEntries.length === 0 && (
        <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }}>Loading catalog…</div>
      )}
      {!catalogue.loading && !catalogue.error && filteredToolsetEntries.length === 0 && (
        <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }} data-testid="tool-picker-empty">
          {selectedOnly
            ? `No selected tools${filter ? " match the filter." : "."}`
            : filter ? "No tools match the filter." : "No toolsets available."}
        </div>
      )}
      {unavailableToolsets.length > 0 && (
        <div style={{ marginBottom: 8, display: "flex", flexWrap: "wrap", gap: 4 }}>
          {unavailableToolsets.map((entry) => (
            <span key={entry.id} className="muted text-sm"
              style={{ fontSize: 11, padding: "2px 8px", border: "1px dashed var(--border)", borderRadius: 4, color: "var(--amber)", opacity: 0.85 }}
              title={entry.unavailable_reason || "unavailable"}>
              <span className="mono">{entry.id}</span> · unavailable
            </span>
          ))}
        </div>
      )}
      {flatTools.length > 0 && (
        <div style={{ maxHeight: 320, overflowY: "auto", border: "1px solid var(--border)", borderRadius: 6 }}>
          {(() => {
            const rows = [];
            let lastToolsetId = null;
            for (const t of pageTools) {
              if (t._toolset.id !== lastToolsetId) {
                const entry = t._toolset;
                const allSelected = entry.tools.length > 0 && entry.tools.every((x) => selected.has(x.scoped_id));
                const someSelected = entry.tools.some((x) => selected.has(x.scoped_id));
                rows.push(
                  <div key={`h-${entry.id}-p${page}`}
                    style={{
                      display: "flex", alignItems: "center", gap: 8, padding: "8px 10px",
                      background: "var(--bg-2)",
                      borderTop: lastToolsetId === null ? "none" : "1px solid var(--border)",
                      borderBottom: "1px solid var(--border)", position: "sticky", top: 0, zIndex: 1,
                    }}>
                    {!single && (
                      <input type="checkbox" checked={allSelected}
                        ref={(el) => { if (el) el.indeterminate = !allSelected && someSelected; }}
                        onChange={() => toggleGroup(entry, allSelected)}
                        disabled={entry.tools.length === 0}
                        data-testid={`tool-picker-group-${entry.id}`} />
                    )}
                    <span className="mono" style={{ fontSize: 12.5, fontWeight: 600 }}>{entry.id}</span>
                    {entry.builtin && <span className="muted text-sm" style={{ fontSize: 10.5 }}>· built-in</span>}
                    <span className="muted text-sm" style={{ marginLeft: "auto" }}>
                      {entry.tools.length} tool{entry.tools.length === 1 ? "" : "s"}
                    </span>
                  </div>
                );
                lastToolsetId = entry.id;
              }
              rows.push(
                <TP_Row key={t.scoped_id} tool={t}
                  checked={selected.has(t.scoped_id)}
                  onToggle={toggleId}
                  open={openId === t.scoped_id}
                  onToggleOpen={(id) => setOpenId((cur) => (cur === id ? null : id))}
                  single={single} />
              );
            }
            return rows;
          })()}
        </div>
      )}
      {flatTools.length > 0 && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 8, fontSize: 11.5, color: "var(--text-3)" }}>
          <span className="tabular">
            {flatTools.length === 0 ? 0 : pageStart + 1}–{pageEnd} of {flatTools.length}
          </span>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <Btn size="sm" kind="ghost" icon="chevron-left" disabled={page === 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))} data-testid="tool-picker-page-prev">‹</Btn>
            <span className="muted text-sm tabular" style={{ padding: "0 6px" }}>{page} of {totalPages}</span>
            <Btn size="sm" kind="ghost" iconRight="chevron-right" disabled={page === totalPages}
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))} data-testid="tool-picker-page-next">›</Btn>
          </div>
        </div>
      )}
      <div className="field-help" data-testid="tool-picker-footnote" style={{ marginTop: 8 }}>
        One picker everywhere: agent bindings, graph tool nodes, approval policies, service grants, the MCP
        allowlist. Flags: y yields · w workspace · r role · n notifying.
      </div>
    </div>
  );
}

window.ToolPicker = ToolPicker;
