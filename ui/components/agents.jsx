/* global React, Icon, StatusPill, Btn, Modal, Banner, CardList, Card, Fab, relativeTime */

// Agents page + detail wired to the real API. The Designer's mock-data
// scaffold was replaced in Phase 2 — every fetch goes through
// window.primerApi.{apiFetch, useResource, useMutation}. Cache-key convention
// follows other components: "agents:list", "agent-detail:${aid}",
// "agent-status:${aid}", "agent-sessions:${aid}", "toolset-tools:${tid}".
//
// Babel-standalone shares the global scope across <script> tags so every
// top-level binding in this file is prefixed with AG_ to avoid name clashes
// with providers.jsx (PROVIDER_FIELDS) and workspaces.jsx (WS_TERMINAL).

const AG_TABS = [
  { id: "config",   label: "Config",   icon: "settings" },
  { id: "tools",    label: "Tools",    icon: "tools" },
  { id: "sessions", label: "Sessions", icon: "zap" },
  { id: "metadata", label: "Metadata", icon: "doc" },
];

const AG_PROVIDER_COLORS = {
  openai: "var(--green)",
  anthropic: "var(--accent)",
  voyageai: "var(--blue)",
  cohere: "var(--violet)",
  ollama: "var(--amber)",
  google: "var(--blue)",
  gemini: "var(--blue)",
  huggingface: "var(--amber)",
  openresponses: "var(--green)",
};

function _agToastErr(pushToast, fallbackTitle) {
  return (err) => {
    if (typeof pushToast !== "function") return;
    pushToast({
      kind: "error",
      title: err?.title || fallbackTitle,
      detail: err?.detail || err?.message,
      requestId: err?.requestId,
    });
  };
}

// ============================================================================
// Agents list page
// ============================================================================

function AgentsPage({ onOpen, pushToast }) {
  const { useResource, useRouter, useViewport, apiFetch, usePagedList, Pager } = window.primerApi;
  const { navigate } = useRouter();
  const { isMobile } = useViewport();

  const [createOpen, setCreateOpen] = React.useState(false);
  const [textFilter, setTextFilter] = React.useState("");
  const filterFocused = React.useRef(false);

  // Server-side offset pagination (bug #19). The text filter is applied
  // client-side over the current page, so typing snaps back to page 0.
  const list = usePagedList({
    key: "agents:list",
    path: "/agents",
    pageSize: 50,
    resetKey: textFilter,
  });
  const providers = useResource(
    "agents:llm-providers",
    (signal) => apiFetch("GET", "/llm_providers?limit=200", null, { signal }),
    { pollMs: null }
  );

  const items = list.items;
  const filtered = React.useMemo(() => {
    if (!textFilter) return items;
    const q = textFilter.toLowerCase();
    return items.filter((a) =>
      (a.id || "").toLowerCase().includes(q) ||
      (a.description || "").toLowerCase().includes(q)
    );
  }, [items, textFilter]);

  // Per-row status — fetch /agents/{id}/status once per visible row.
  const [perRowStatus, setPerRowStatus] = React.useState({});
  React.useEffect(() => {
    if (items.length === 0) {
      setPerRowStatus({});
      return undefined;
    }
    const ctrl = new AbortController();
    Promise.all(
      items.map((a) =>
        apiFetch("GET", `/agents/${encodeURIComponent(a.id)}/status`, null, { signal: ctrl.signal })
          .then((r) => [a.id, r])
          .catch((e) => [a.id, { ok: null, error: e?.title || e?.message }])
      )
    ).then((entries) => setPerRowStatus(Object.fromEntries(entries)));
    return () => ctrl.abort();
  }, [list.data]);

  // Per-row session count, best-effort.
  const [perRowSessions, setPerRowSessions] = React.useState({});
  React.useEffect(() => {
    if (items.length === 0) {
      setPerRowSessions({});
      return undefined;
    }
    const ctrl = new AbortController();
    Promise.all(
      items.map((a) =>
        apiFetch("GET", `/sessions?agent_id=${encodeURIComponent(a.id)}&limit=1`, null, { signal: ctrl.signal })
          .then((r) => [a.id, r.total ?? (r.items?.length ?? 0)])
          .catch(() => [a.id, null])
      )
    ).then((entries) => setPerRowSessions(Object.fromEntries(entries)));
    return () => ctrl.abort();
  }, [list.data]);

  const openRow = (aid) => {
    if (typeof onOpen === "function") onOpen(aid);
    else navigate("/agents/" + aid);
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter agents…"
            value={textFilter}
            onChange={(e) => setTextFilter(e.target.value)}
            onFocus={() => { filterFocused.current = true; }}
            onBlur={() => { filterFocused.current = false; }}
          />
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New agent</Btn>
        </div>
      </div>

      {isMobile ? (
        list.loading && items.length === 0 ? (
          <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</div>
        ) : list.error && items.length === 0 ? (
          <Banner
            kind="error"
            title={list.error.title || "Couldn't load agents"}
            detail={list.error.detail || list.error.message}
            actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
          />
        ) : (
          <CardList
            items={filtered}
            empty={items.length === 0 ? "No agents yet." : "No agents match."}
            renderCard={(a) => {
              const providerId = a.model?.provider_id;
              const modelName = a.model?.model_name;
              const provider = (providers.data?.items ?? []).find((p) => p.id === providerId);
              const vendorColor = AG_PROVIDER_COLORS[provider?.provider] || "var(--text-3)";
              const status = perRowStatus[a.id];
              const sessionCount = perRowSessions[a.id];
              const statusPill = status == null
                ? null
                : status.ok === true
                  ? <span className="pill pill-ended"><span className="dot"></span>ok</span>
                  : status.ok === false
                    ? <span className="pill pill-failed"><span className="dot"></span>{(status.issues || []).length} issue{(status.issues || []).length === 1 ? "" : "s"}</span>
                    : <span className="muted" title={status.error}>err</span>;
              const metaParts = [];
              metaParts.push(`${(a.tools ?? []).length} tool${(a.tools ?? []).length === 1 ? "" : "s"}`);
              if (sessionCount != null) metaParts.push(`${sessionCount} session${sessionCount === 1 ? "" : "s"}`);
              return (
                <Card
                  title={a.id}
                  subtitle={providerId
                    ? <span className="mono">
                        <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: vendorColor, marginRight: 6 }}></span>
                        {providerId}{modelName ? <span className="muted"> · {modelName}</span> : null}
                      </span>
                    : <span className="muted">(unconfigured)</span>}
                  pill={statusPill}
                  meta={`${metaParts.join(" · ")}${a.description ? " · " + a.description : ""}`}
                  onClick={() => openRow(a.id)}
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
              <th>Provider · model</th>
              <th>Tools</th>
              <th style={{ textAlign: "right" }}>Sessions</th>
              <th style={{ width: 100 }}>Status</th>
            </tr>
          </thead>
          <tbody>
            {list.loading && items.length === 0 ? (
              <tr><td colSpan={6} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</td></tr>
            ) : list.error && items.length === 0 ? (
              <tr><td colSpan={6} style={{ padding: 20, textAlign: "center" }}>
                <span style={{ color: "var(--red)" }}>{list.error.title || list.error.message}</span>
                {" · "}<a onClick={list.refetch} style={{ cursor: "pointer" }}>Retry</a>
              </td></tr>
            ) : filtered.length === 0 ? (
              items.length === 0 ? (
                <tr><td colSpan={6}>
                  <div className="empty" style={{ padding: "40px 20px" }}>
                    <div className="ico-wrap"><Icon name="agent" size={22} /></div>
                    <div className="head">No agents yet</div>
                    <div className="sub">Agents pair an LLM provider with a system prompt and a list of toolsets, then run inside a session.</div>
                    <div className="actions"><Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New agent</Btn></div>
                  </div>
                </td></tr>
              ) : (
                <tr><td colSpan={6} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>No agents match.</td></tr>
              )
            ) : filtered.map((a) => {
              const providerId = a.model?.provider_id;
              const modelName = a.model?.model_name;
              const provider = (providers.data?.items ?? []).find((p) => p.id === providerId);
              const vendorColor = AG_PROVIDER_COLORS[provider?.provider] || "var(--text-3)";
              const status = perRowStatus[a.id];
              const sessionCount = perRowSessions[a.id];
              return (
                <tr key={a.id} onClick={() => openRow(a.id)} style={{ cursor: "pointer" }}>
                  <td className="mono">{a.id}</td>
                  <td className="muted text-sm" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {a.description || <span style={{ color: "var(--text-4)" }}>—</span>}
                  </td>
                  <td className="mono text-sm">
                    {providerId
                      ? <>
                          <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: vendorColor, marginRight: 6 }}></span>
                          {providerId}{modelName ? <span className="muted"> · {modelName}</span> : null}
                        </>
                      : <span className="muted">(unconfigured)</span>}
                  </td>
                  <td className="mono muted text-sm">{(a.tools ?? []).length}</td>
                  <td className="mono num tabular">
                    {sessionCount == null
                      ? <span className="muted">…</span>
                      : sessionCount > 0
                        ? <span style={{ color: "var(--blue)" }}>{sessionCount}</span>
                        : <span className="muted">0</span>}
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
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      )}

      <Pager pager={list} label="agents" />

      {isMobile && (
        <Fab icon="plus" label="New agent" onClick={() => setCreateOpen(true)} />
      )}

      {createOpen && (
        <AG_NewAgentModal
          onClose={() => setCreateOpen(false)}
          pushToast={pushToast}
          onCreate={(row) => {
            setCreateOpen(false);
            if (typeof pushToast === "function") {
              pushToast({ kind: "success", title: "Agent created", detail: row.id });
            }
            list.refetch();
            navigate("/agents/" + row.id);
          }}
        />
      )}
    </div>
  );
}

// ============================================================================
// New agent modal
// ============================================================================

function AG_NewAgentModal({ onClose, onCreate, pushToast, existing }) {
  // Same modal serves both create (existing == null) and edit
  // (existing == agent row). In edit mode the id field is locked,
  // submit PUT-replaces, and the success callback is just close().
  const isEdit = !!existing;
  const { useResource, useMutation, apiFetch } = window.primerApi;
  const providers = useResource(
    "agents:llm-providers",
    (signal) => apiFetch("GET", "/llm_providers?limit=200", null, { signal }),
    { pollMs: null }
  );
  // /v1/tools returns the merged catalogue across user-defined + the
  // five built-in toolsets, with each tool's scoped id (toolset__tool)
  // already computed server-side. Failures per-toolset are surfaced
  // via available=false on the toolset entry so the picker can render
  // them dimmed instead of breaking entirely.
  const toolsCatalogue = useResource(
    "agents:tools-catalogue",
    (signal) => apiFetch("GET", "/tools", null, { signal }),
    { pollMs: null }
  );

  // Initial values come from the existing agent in edit mode, else
  // blanks. system_prompt and compaction_prompt are stored as arrays
  // server-side; the form only handles a single line, so we collapse
  // ["a", "b"] → "a\n\nb" on read and emit one entry on save.
  const _joinPrompt = (p) => Array.isArray(p) ? p.join("\n\n") : (p || "");
  const _initialTools = () => {
    const t = existing?.tools;
    return new Set(Array.isArray(t) ? t : []);
  };

  const [id, setId] = React.useState(existing?.id || "");
  const [description, setDescription] = React.useState(existing?.description || "");
  const [providerId, setProviderId] = React.useState(existing?.model?.provider_id || "");
  const [modelName, setModelName] = React.useState(existing?.model?.model_name || "");
  const [systemPrompt, setSystemPrompt] = React.useState(_joinPrompt(existing?.system_prompt));
  const [compactionPrompt, setCompactionPrompt] = React.useState(_joinPrompt(existing?.compaction_prompt));
  // selectedScopedIds is a Set so toggles are O(1); persisted as a
  // sorted list at submit time for stable JSON.
  const [selectedScopedIds, setSelectedScopedIds] = React.useState(_initialTools);
  const [temperature, setTemperature] = React.useState(
    existing?.temperature != null ? String(existing.temperature) : ""
  );
  // response_format is a structured-output JSON Schema object stored on
  // the agent. Held as raw text in the form (pretty-printed from the
  // saved value on edit) + a parse-error flag; parsed to an object at
  // submit time. Empty text == no structured output (omitted from body).
  // Mirrors the graph editor's per-node response_format JSON field.
  const [responseFormat, setResponseFormat] = React.useState(
    existing?.response_format != null
      ? JSON.stringify(existing.response_format, null, 2)
      : ""
  );
  const [responseFormatError, setResponseFormatError] = React.useState(null);
  const [fieldErrors, setFieldErrors] = React.useState({});
  const [activeTab, setActiveTab] = React.useState("basic");
  const [toolFilter, setToolFilter] = React.useState("");
  // "Selected" filter chip (studio-ux fix 4): the Tools tab's search had no
  // way to see WHICH tools are ticked across all toolsets/pages — this ANDs
  // with the text filter, reusing the same selectedScopedIds set the
  // "N of 172 selected" counter already tracks.
  const [showSelectedOnly, setShowSelectedOnly] = React.useState(false);
  const [toolPage, setToolPage] = React.useState(1);

  React.useEffect(() => {
    if (!providerId && providers.data?.items?.length) {
      setProviderId(providers.data.items[0].id);
    }
  }, [providers.data, providerId]);

  const selectedProvider = (providers.data?.items ?? []).find((p) => p.id === providerId);
  const modelOptions = selectedProvider?.models ?? [];

  React.useEffect(() => {
    if (modelOptions.length > 0 && !modelOptions.some((m) => m.name === modelName)) {
      setModelName(modelOptions[0].name);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelOptions]);

  const toolsetEntries = toolsCatalogue.data?.items ?? [];

  const filteredToolsetEntries = React.useMemo(() => {
    const q = toolFilter.trim().toLowerCase();
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
    // "Selected" filter chip: restrict to tools currently in
    // selectedScopedIds, across every toolset/page (not just the visible
    // slice), so it composes cleanly with the text filter above.
    if (showSelectedOnly) {
      entries = entries
        .map((ts) => ({ ...ts, tools: ts.tools.filter((t) => selectedScopedIds.has(t.scoped_id)) }))
        .filter((ts) => ts.tools.length > 0);
    }
    return entries;
  }, [toolsetEntries, toolFilter, showSelectedOnly, selectedScopedIds]);

  const totalAvailable = React.useMemo(
    () => toolsetEntries.reduce((acc, ts) => acc + ts.tools.length, 0),
    [toolsetEntries],
  );

  // Built-in toolsets alone can ship 100+ tools (the `system` toolset
  // currently exposes ~102), so the picker has to paginate or the
  // modal becomes a 3000-px-tall scroll. The list is flattened across
  // available toolsets so paging counts whole tools, not whole groups
  // (toolsets that span pages get their header re-rendered at the top
  // of each page so the operator never loses scope context). Unavailable
  // toolsets are stripped here and rendered as a compact summary above
  // the paginated list so they don't waste page slots.
  const AGENT_TOOL_PAGE_SIZE = 25;
  const flatTools = React.useMemo(() => {
    const out = [];
    for (const ts of filteredToolsetEntries) {
      if (!ts.available) continue;
      for (const tool of ts.tools) {
        out.push({ ...tool, _toolset: ts });
      }
    }
    return out;
  }, [filteredToolsetEntries]);
  const unavailableToolsets = React.useMemo(
    () => filteredToolsetEntries.filter((ts) => !ts.available),
    [filteredToolsetEntries],
  );
  const toolTotalPages = Math.max(1, Math.ceil(flatTools.length / AGENT_TOOL_PAGE_SIZE));
  // Snap to first page whenever the filter narrows the result set,
  // and clamp the current page if the page count shrinks below it.
  React.useEffect(() => { setToolPage(1); }, [toolFilter, showSelectedOnly]);
  React.useEffect(() => {
    if (toolPage > toolTotalPages) setToolPage(toolTotalPages);
  }, [toolPage, toolTotalPages]);
  const toolPageStart = (toolPage - 1) * AGENT_TOOL_PAGE_SIZE;
  const toolPageEnd = Math.min(toolPageStart + AGENT_TOOL_PAGE_SIZE, flatTools.length);
  const pageTools = flatTools.slice(toolPageStart, toolPageEnd);

  const toggleScopedId = (scopedId) => {
    setSelectedScopedIds((prev) => {
      const next = new Set(prev);
      if (next.has(scopedId)) next.delete(scopedId);
      else next.add(scopedId);
      return next;
    });
  };

  const toggleToolsetGroup = (entry, allSelected) => {
    setSelectedScopedIds((prev) => {
      const next = new Set(prev);
      for (const t of entry.tools) {
        if (allSelected) next.delete(t.scoped_id);
        else next.add(t.scoped_id);
      }
      return next;
    });
  };

  const create = useMutation(
    (body) => isEdit
      ? apiFetch("PUT", "/agents/" + encodeURIComponent(existing.id), body)
      : apiFetch("POST", "/agents", body),
    {
      invalidates: isEdit
        ? ["agents:list", "agent-detail:" + (existing?.id || ""), "agent-status:" + (existing?.id || "")]
        : ["agents:list"],
      onSuccess: (row) => onCreate(row),
      onError: (err) => {
        if (err.status === 422 && Array.isArray(err.fieldErrors)) {
          const map = {};
          for (const fe of err.fieldErrors) map[(fe.loc || []).join(".")] = fe.msg;
          setFieldErrors(map);
        } else if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err.title || (isEdit ? "Save failed" : "Create failed"),
            detail: err.detail || err.message,
            requestId: err.requestId,
          });
        }
      },
    }
  );

  const submit = async () => {
    setFieldErrors({});
    setResponseFormatError(null);
    // response_format: parse the textarea once here so a malformed
    // schema is caught client-side (jump to Advanced + show the error)
    // before the request goes out. Empty text == no structured output.
    let responseFormatValue = null;
    if (responseFormat.trim() !== "") {
      try {
        responseFormatValue = JSON.parse(responseFormat);
      } catch (e) {
        setResponseFormatError(String(e.message || e));
        setActiveTab("advanced");
        return;
      }
    }
    // Agent.tools is the list of scoped tool ids — no separate
    // allowlist field; an empty list means no tools registered.
    const tools = [...selectedScopedIds].sort();
    const body = {
      // On edit the id is locked but still sent (PUT-replace contract).
      ...(isEdit ? { id: existing.id } : (id ? { id } : {})),
      description: description || "(no description)",
      model: { provider_id: providerId, model_name: modelName },
      tools,
      system_prompt: systemPrompt ? [systemPrompt] : [],
      compaction_prompt: compactionPrompt ? [compactionPrompt] : [],
    };
    if (temperature !== "" && !Number.isNaN(+temperature)) {
      body.temperature = Number(temperature);
    }
    // PUT is a full replace, so always send response_format on edit
    // (null clears a previously-set schema); on create only include it
    // when set, matching the model default.
    if (responseFormatValue !== null || isEdit) {
      body.response_format = responseFormatValue;
    }
    try { await create.mutate(body); } catch (_e) { /* surfaced via onError */ }
  };

  const selectedCount = selectedScopedIds.size;

  return (
    <Modal
      title={isEdit ? `Edit agent · ${existing.id}` : "New agent"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn
            kind="primary"
            icon={isEdit ? "check" : "plus"}
            onClick={submit}
            disabled={!providerId || !modelName || create.loading}
          >
            {create.loading ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save changes" : "Create")}
          </Btn>
        </>
      }
    >
      <div className="tabs" style={{ display: "flex", gap: 4, borderBottom: "1px solid var(--border)", marginBottom: 14 }}>
        {[
          { key: "basic", label: "Basic" },
          { key: "tools", label: `Tools${selectedCount > 0 ? ` (${selectedCount})` : ""}` },
          { key: "advanced", label: "Advanced" },
        ].map((t) => (
          <button
            key={t.key}
            type="button"
            data-testid={`agent-tab-${t.key}`}
            onClick={() => setActiveTab(t.key)}
            style={{
              background: "none", border: "none",
              borderBottom: activeTab === t.key ? "2px solid var(--accent)" : "2px solid transparent",
              padding: "6px 12px", marginBottom: -1, cursor: "pointer",
              color: activeTab === t.key ? "var(--text)" : "var(--text-2)",
              fontSize: 12.5, fontWeight: activeTab === t.key ? 600 : 400,
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {activeTab === "basic" && (
        <>
          <div className="field">
            <label className="field-label" htmlFor="na-id">
              ID {isEdit
                ? <span className="hint">locked — id cannot change after create</span>
                : <span className="hint">optional — backend assigns if blank</span>}
            </label>
            <input
              id="na-id"
              className="input"
              value={id}
              onChange={(e) => setId(e.target.value)}
              placeholder="auto-generated"
              disabled={isEdit}
              style={{ width: "100%" }}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="na-description">Description</label>
            <input
              id="na-description"
              className="input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              style={{ width: "100%" }}
            />
            {fieldErrors["body.description"] && (
              <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.description"]}</div>
            )}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="na-llm-provider">LLM provider</label>
            <select
              id="na-llm-provider"
              className="select"
              value={providerId}
              onChange={(e) => setProviderId(e.target.value)}
              style={{ width: "100%" }}
            >
              <option value="">-- pick a provider --</option>
              {(providers.data?.items ?? []).map((p) => (
                <option key={p.id} value={p.id}>{p.id}</option>
              ))}
            </select>
            {(providers.data?.items ?? []).length === 0 && !providers.loading && (
              <div className="field-help" style={{ color: "var(--amber)" }}>
                No LLM providers configured. Create one at <span className="mono">/providers/llm</span> first.
              </div>
            )}
            {fieldErrors["body.model.provider_id"] && (
              <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.model.provider_id"]}</div>
            )}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="na-model">Model</label>
            <select
              id="na-model"
              className="select"
              value={modelName}
              onChange={(e) => setModelName(e.target.value)}
              style={{ width: "100%" }}
            >
              <option value="">-- pick a model --</option>
              {modelOptions.map((m) => (
                <option key={m.name} value={m.name}>{m.name}</option>
              ))}
            </select>
            <div className="field-help">Model list comes from the provider row, not a live introspection (T0025).</div>
            {fieldErrors["body.model.model_name"] && (
              <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.model.model_name"]}</div>
            )}
          </div>
        </>
      )}

      {activeTab === "tools" && (
        <div>
          <div style={{ marginBottom: 10, display: "flex", alignItems: "center", gap: 8 }}>
            <div className="input-icon" style={{ flex: 1 }}>
              <Icon name="search" size={13} className="icon" />
              <input
                className="input"
                placeholder="Filter by tool name, description, or toolset…"
                value={toolFilter}
                onChange={(e) => setToolFilter(e.target.value)}
                data-testid="agent-tool-filter"
                style={{ width: "100%" }}
              />
            </div>
            <button
              type="button"
              data-testid="agent-tool-filter-selected"
              className={"chip" + (showSelectedOnly ? " active" : "")}
              aria-pressed={showSelectedOnly}
              onClick={() => setShowSelectedOnly((v) => !v)}
              title={showSelectedOnly ? "Show all tools" : "Show only selected tools"}
              style={{ whiteSpace: "nowrap" }}
            >
              Selected
            </button>
            <span className="muted text-sm" style={{ whiteSpace: "nowrap" }}>
              {selectedCount} of {totalAvailable} selected
            </span>
          </div>
          {toolsCatalogue.loading && toolsetEntries.length === 0 && (
            <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }}>
              Loading tool catalogue…
            </div>
          )}
          {!toolsCatalogue.loading && filteredToolsetEntries.length === 0 && (
            <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }} data-testid="agent-tool-empty">
              {showSelectedOnly
                ? `No selected tools${toolFilter ? " match the filter." : "."}`
                : toolFilter ? "No tools match the filter." : "No toolsets available."}
            </div>
          )}
          {/* Unavailable toolsets stay visible outside the paginated
              body so operators can see which providers exist but aren't
              currently usable — they don't consume page slots. */}
          {unavailableToolsets.length > 0 && (
            <div style={{ marginBottom: 8, display: "flex", flexWrap: "wrap", gap: 4 }}>
              {unavailableToolsets.map((entry) => (
                <span
                  key={entry.id}
                  className="muted text-sm"
                  style={{
                    fontSize: 11, padding: "2px 8px",
                    border: "1px dashed var(--border)", borderRadius: 4,
                    color: "var(--amber)", opacity: 0.85,
                  }}
                  title={entry.unavailable_reason || "unavailable"}
                >
                  <span className="mono">{entry.id}</span> · unavailable
                </span>
              ))}
            </div>
          )}
          {flatTools.length > 0 && (
            <div style={{ maxHeight: 360, overflowY: "auto", border: "1px solid var(--border)", borderRadius: 6 }}>
              {(() => {
                // Render the paginated slice with a toolset header
                // every time the parent toolset changes. The header
                // tristate operates on the FULL toolset (across pages),
                // not just the visible slice, so bulk select stays
                // meaningful regardless of paging.
                const rows = [];
                let lastToolsetId = null;
                for (const t of pageTools) {
                  if (t._toolset.id !== lastToolsetId) {
                    const entry = t._toolset;
                    const allSelected = entry.tools.length > 0 && entry.tools.every((x) => selectedScopedIds.has(x.scoped_id));
                    const someSelected = entry.tools.some((x) => selectedScopedIds.has(x.scoped_id));
                    rows.push(
                      <div
                        key={`h-${entry.id}-p${toolPage}`}
                        style={{
                          display: "flex", alignItems: "center", gap: 8,
                          padding: "8px 10px", background: "var(--bg-2)",
                          borderTop: lastToolsetId === null ? "none" : "1px solid var(--border)",
                          borderBottom: "1px solid var(--border)",
                          position: "sticky", top: 0, zIndex: 1,
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={allSelected}
                          ref={(el) => { if (el) el.indeterminate = !allSelected && someSelected; }}
                          onChange={() => toggleToolsetGroup(entry, allSelected)}
                          disabled={entry.tools.length === 0}
                          data-testid={`agent-toolset-group-${entry.id}`}
                        />
                        <span className="mono" style={{ fontSize: 12.5, fontWeight: 600 }}>{entry.id}</span>
                        {entry.builtin && <span className="muted text-sm" style={{ fontSize: 10.5 }}>· built-in</span>}
                        {entry.tagline && (
                          <span className="muted text-sm" style={{ fontSize: 11, marginLeft: 4 }}>{entry.tagline}</span>
                        )}
                        <span className="muted text-sm" style={{ marginLeft: "auto" }}>
                          {entry.tools.length} tool{entry.tools.length === 1 ? "" : "s"}
                        </span>
                      </div>
                    );
                    lastToolsetId = entry.id;
                  }
                  const checked = selectedScopedIds.has(t.scoped_id);
                  rows.push(
                    <label
                      key={t.scoped_id}
                      style={{
                        display: "flex", alignItems: "flex-start", gap: 8,
                        padding: "6px 10px 6px 28px", cursor: "pointer",
                        borderTop: "1px solid var(--bg-1)",
                        background: checked ? "var(--bg-2)" : "transparent",
                      }}
                      data-testid={`agent-tool-${t.scoped_id}`}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleScopedId(t.scoped_id)}
                        style={{ marginTop: 3 }}
                      />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div className="mono" style={{ fontSize: 12 }}>{t.id}</div>
                        {t.description && (
                          <div className="muted text-sm" style={{ fontSize: 11, marginTop: 2, lineHeight: 1.4 }}>
                            {t.description}
                          </div>
                        )}
                      </div>
                    </label>
                  );
                }
                return rows;
              })()}
            </div>
          )}
          {flatTools.length > 0 && (
            <div
              style={{
                display: "flex", alignItems: "center", justifyContent: "space-between",
                marginTop: 8, fontSize: 11.5, color: "var(--text-3)",
              }}
            >
              <span className="tabular">
                Showing <strong style={{ color: "var(--text)" }}>{flatTools.length === 0 ? 0 : toolPageStart + 1}</strong>–
                <strong style={{ color: "var(--text)" }}>{toolPageEnd}</strong> of{" "}
                <strong style={{ color: "var(--text)" }}>{flatTools.length}</strong>
              </span>
              <div className="pager" style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <Btn
                  size="sm"
                  kind="ghost"
                  icon="chevron-left"
                  disabled={toolPage === 1}
                  onClick={() => setToolPage((p) => Math.max(1, p - 1))}
                  data-testid="agent-tool-page-prev"
                >Previous</Btn>
                <span className="muted text-sm tabular" style={{ padding: "0 6px" }}>
                  Page {toolPage} of {toolTotalPages}
                </span>
                <Btn
                  size="sm"
                  kind="ghost"
                  iconRight="chevron-right"
                  disabled={toolPage === toolTotalPages}
                  onClick={() => setToolPage((p) => Math.min(toolTotalPages, p + 1))}
                  data-testid="agent-tool-page-next"
                >Next</Btn>
              </div>
            </div>
          )}
          <div className="field-help" style={{ marginTop: 8 }}>
            Tools are referenced as <span className="mono">toolset_id__tool_name</span>. The agent has
            access to <strong>only</strong> the tools picked here — never a whole toolset. Bulk-select via the
            toolset header ticks every tool in that toolset (across pages); the toolset itself is not
            implicitly registered.
          </div>
        </div>
      )}

      {activeTab === "advanced" && (
        <>
          <div className="field">
            <label className="field-label" htmlFor="na-system-prompt">
              System prompt <span className="hint">optional · stored as a single-segment list</span>
            </label>
            <textarea
              id="na-system-prompt"
              className="textarea"
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              rows={4}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="na-compaction-prompt">
              Compaction prompt <span className="hint">optional · used when the conversation outgrows the LLM context window</span>
            </label>
            <textarea
              id="na-compaction-prompt"
              className="textarea"
              value={compactionPrompt}
              onChange={(e) => setCompactionPrompt(e.target.value)}
              rows={4}
              placeholder="Instructions the runtime uses to summarise older turns when context is tight. Empty = use the framework default."
            />
            <div className="field-help">
              Agent-specific because <em>what to keep</em> depends on the agent's purpose — a researcher may
              want to preserve cited sources, a coder the current file under edit.
            </div>
            <p className="help-text">
              Leave blank to use the default prompt (recommended unless your agent has a domain-specific compaction need).
              The default is designed to preserve system context, recent turns, and pending tool calls.
            </p>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="na-temperature">
              Temperature <span className="hint">optional · default is provider-decided</span>
            </label>
            <input
              id="na-temperature"
              className="input"
              type="number"
              step="0.05"
              min="0"
              value={temperature}
              onChange={(e) => setTemperature(e.target.value)}
              style={{ width: 100 }}
            />
            {fieldErrors["body.temperature"] && (
              <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.temperature"]}</div>
            )}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="na-response-format">
              Response format <span className="hint">optional · structured-output JSON Schema</span>
            </label>
            <textarea
              id="na-response-format"
              className="textarea mono"
              value={responseFormat}
              onChange={(e) => setResponseFormat(e.target.value)}
              onBlur={() => {
                // Validate-on-blur like the graph editor's GR_JsonField:
                // empty == no schema (cleared), otherwise must parse.
                if (responseFormat.trim() === "") {
                  setResponseFormatError(null);
                  return;
                }
                try {
                  JSON.parse(responseFormat);
                  setResponseFormatError(null);
                } catch (e) {
                  setResponseFormatError(String(e.message || e));
                }
              }}
              rows={6}
              placeholder={'{\n  "type": "object",\n  "properties": { "verdict": { "type": "string" } },\n  "required": ["verdict"]\n}'}
              style={{ width: "100%", fontFamily: "IBM Plex Mono", fontSize: 12 }}
              data-testid="agent-response-format"
            />
            <div className="field-help">
              When set, the LLM is constrained to emit JSON matching this schema (same shape
              as a graph agent-node's <span className="mono">response_format</span>). Leave blank
              to run the agent unconstrained. Validated as a JSON Schema on save.
            </div>
            {responseFormatError && (
              <div className="field-help" style={{ color: "var(--red)" }}>JSON parse: {responseFormatError}</div>
            )}
            {fieldErrors["body.response_format"] && (
              <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.response_format"]}</div>
            )}
          </div>
        </>
      )}
    </Modal>
  );
}

// ============================================================================
// Agent detail page
// ============================================================================

function AgentDetail({ agentId, pushToast }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.primerApi;
  const { params, query, navigate } = useRouter();
  const id = agentId || params.id;
  const tab = AG_TABS.some((t) => t.id === query.tab) ? query.tab : "config";
  const setTab = (t) => navigate("/agents/" + id + "?tab=" + t);

  const detail = useResource(
    "agent-detail:" + id,
    (signal) => apiFetch("GET", "/agents/" + encodeURIComponent(id), null, { signal }),
    { pollMs: null, deps: [id] }
  );
  const status = useResource(
    "agent-status:" + id,
    (signal) => apiFetch("GET", "/agents/" + encodeURIComponent(id) + "/status", null, { signal }),
    { pollMs: 30000, deps: [id] }
  );

  const delMut = useMutation(
    () => apiFetch("DELETE", "/agents/" + encodeURIComponent(id)),
    {
      invalidates: ["agents:list"],
      onSuccess: () => {
        if (typeof pushToast === "function") {
          pushToast({ kind: "warning", title: "Agent deleted", detail: id });
        }
        navigate("/agents");
      },
      onError: (err) => {
        if (err.status === 409) {
          setDeleteError(err.detail || err.title || "Cannot delete — referenced by other entities");
        } else if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err.title || "Delete failed",
            detail: err.detail || err.message,
            requestId: err.requestId,
          });
        }
      },
    }
  );
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState(null);

  // New "Chat" button: skip the workspace-session ceremony and just
  // open an interactive chat with this agent — POST /chats then
  // navigate to /chats/{id}. The chat detail page handles initial
  // message + streaming.
  const startChatMut = useMutation(
    () => apiFetch("POST", "/chats", { agent_id: id }),
    {
      invalidates: ["chats:list"],
      onSuccess: (row) => navigate("/chats/" + row.id),
      onError: (err) => {
        if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err?.title || "Couldn't start chat",
            detail: err?.detail || err?.message,
            requestId: err?.requestId,
          });
        }
      },
    }
  );
  const startChat = () => { if (!startChatMut.loading) startChatMut.mutate(); };

  if (detail.loading && !detail.data) {
    return (
      <div className="col" style={{ gap: 14 }}>
        <AG_DetailActions onChat={startChat} chatLoading={startChatMut.loading} onDelete={() => { setDeleteError(null); setConfirmDelete(true); }} onBack={() => navigate("/agents")} />
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
      </div>
    );
  }
  if (detail.error && !detail.data) {
    return (
      <div className="col" style={{ gap: 14 }}>
        <AG_DetailActions onChat={startChat} chatLoading={startChatMut.loading} onDelete={() => { setDeleteError(null); setConfirmDelete(true); }} onBack={() => navigate("/agents")} />
        <Banner
          kind="error"
          title={detail.error.title || "Couldn't load agent"}
          detail={detail.error.detail || detail.error.message}
          actions={<Btn size="sm" icon="chevron-left" onClick={() => navigate("/agents")}>Back to list</Btn>}
        />
      </div>
    );
  }

  const a = detail.data;

  return (
    <div className="col" style={{ gap: 14 }}>
      <AG_DetailActions
        onChat={startChat} chatLoading={startChatMut.loading}
        onDelete={() => { setDeleteError(null); setConfirmDelete(true); }}
        onBack={() => navigate("/agents")}
      />

      <AG_StatusPanel id={id} status={status} />

      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {AG_TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={tab === t.id}
              onClick={() => setTab(t.id)}
              className={tab === t.id ? "active" : ""}
              style={{
                background: "none",
                border: "none",
                padding: "10px 14px",
                cursor: "pointer",
                color: tab === t.id ? "var(--text)" : "var(--text-3)",
                fontSize: 12.5,
                fontWeight: tab === t.id ? 600 : 400,
                borderBottom: tab === t.id ? "2px solid var(--accent)" : "2px solid transparent",
                marginBottom: -1,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              <Icon name={t.icon} size={13} />
              {t.label}
            </button>
          ))}
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          {tab === "config" && <AG_ConfigTab agent={a} pushToast={pushToast} />}
          {tab === "tools" && <AG_ToolsTab agent={a} />}
          {tab === "sessions" && <AG_SessionsTab agentId={id} />}
          {tab === "metadata" && <AG_MetadataTab agent={a} />}
        </div>
      </div>

      {confirmDelete && (
        <Modal
          title={`Delete ${id}?`}
          danger
          onClose={() => setConfirmDelete(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setConfirmDelete(false)}>Cancel</Btn>
              <Btn
                kind="danger"
                icon="trash"
                disabled={delMut.loading}
                onClick={async () => {
                  try { await delMut.mutate(); } catch (_e) { /* surfaced via onError */ }
                }}
              >Delete</Btn>
            </>
          }
        >
          {deleteError && (
            <Banner
              kind="error"
              title="Delete blocked"
              detail={deleteError}
            />
          )}
          <ul>
            <li>Removes the agent row from storage.</li>
            <li>Any session bound to this agent that is still running will fail on the next turn-claim.</li>
            <li>DELETE is NOT idempotent — a second DELETE returns 404 (app spec §5).</li>
          </ul>
        </Modal>
      )}

    </div>
  );
}

// Internal action bar — rendered INSIDE the page body so the
// .page-header .page-actions selector resolves to the buttons even
// though app.jsx renders its own outer page-header.
function AG_DetailActions({ onChat, chatLoading, onDelete, onBack }) {
  return (
    <div className="page-header" style={{ marginBottom: 0, justifyContent: "flex-end" }}>
      <div className="page-actions">
        <Btn icon="send" kind="primary" onClick={onChat} disabled={chatLoading}>
          {chatLoading ? "Opening chat…" : "Chat"}
        </Btn>
        <Btn icon="trash" kind="danger" onClick={onDelete}>Delete</Btn>
        <Btn icon="chevron-left" kind="ghost" onClick={onBack}>Back</Btn>
      </div>
    </div>
  );
}

function AG_StatusPanel({ id, status }) {
  const ok = status.data?.ok === true;
  const issues = status.data?.issues || [];
  const colour = status.data == null ? "var(--text-3)" : ok ? "var(--green)" : "var(--red)";
  return (
    <div
      className="panel"
      style={{
        background: ok
          ? "linear-gradient(90deg, var(--green-dim) 0%, var(--bg-1) 50%)"
          : status.data == null
            ? undefined
            : "linear-gradient(90deg, var(--red-dim) 0%, var(--bg-1) 50%)",
        borderColor: ok
          ? "oklch(0.75 0.15 145 / 0.3)"
          : status.data == null
            ? undefined
            : "oklch(0.7 0.2 25 / 0.3)",
      }}
    >
      <div className="panel-body" style={{ display: "flex", alignItems: "flex-start", gap: 14, padding: "14px 18px" }}>
        <Icon
          name={ok ? "check-circle" : status.data == null ? "info" : "x-circle"}
          size={28}
          style={{ color: colour, flexShrink: 0 }}
        />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 600 }}>
            {status.loading && status.data == null
              ? "Checking references…"
              : status.error
                ? "Status check failed"
                : ok
                  ? "All references resolve"
                  : `${issues.length} issue${issues.length === 1 ? "" : "s"} blocking new sessions`}
          </div>
          <div className="muted text-sm">
            <span className="mono">GET /v1/agents/{id}/status</span> · last checked just now · polled every 30s
            {status.error && (
              <> · <span style={{ color: "var(--red)" }}>{status.error.title || status.error.message}</span></>
            )}
          </div>
          {issues.length > 0 && (
            <div className="mt-2">
              {issues.map((iss, i) => (
                <div key={i} className="ref-row" style={{ borderColor: "var(--red-dim)" }}>
                  <Icon name="alert" size={12} className="ico" style={{ color: "var(--red)" }} />
                  <span className="label" style={{ color: "var(--red)" }}>{iss.kind || "issue"}</span>
                  <span className="val">{iss.detail || iss.message || JSON.stringify(iss)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// Config tab — read-only JSON + References cross-check
// ============================================================================

function AG_ConfigTab({ agent, pushToast }) {
  const hl = window.primerVendor?.highlightJson;
  const isManaged = !!agent.harness_id;

  const [editing, setEditing] = React.useState(false);

  const pretty = React.useMemo(() => JSON.stringify(agent, null, 2), [agent]);

  return (
    <div style={{ padding: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 10, gap: 10 }}>
        <div className="muted text-sm">
          {isManaged ? (
            <>This agent is managed by harness <span className="mono">{agent.harness_id}</span>. Direct edits are blocked — update the harness instead.</>
          ) : (
            <>PUT-replace edit via the form. References panel below cross-checks the bound provider + toolsets after save.</>
          )}
        </div>
        <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
          {!isManaged && (
            <Btn size="sm" icon="edit" kind="secondary" onClick={() => setEditing(true)}>Edit</Btn>
          )}
        </div>
      </div>
      {editing && (
        <AG_NewAgentModal
          existing={agent}
          pushToast={pushToast}
          onClose={() => setEditing(false)}
          onCreate={() => {
            setEditing(false);
            if (typeof pushToast === "function") {
              pushToast({ kind: "info", title: "Agent updated", detail: agent.id });
            }
          }}
        />
      )}
      {false ? (
        <textarea
          readOnly
          value={pretty}
          style={{
            // unreachable — kept so the closing `: hl ? ... : <pre>`
            // branches below stay valid JSX without a deeper rewrite
            // of this tab; the actual rendered output is the highlighted
            // / pre block.
            display: "none",
          }}
        />
      ) : hl
        ? <div className="code-block" dangerouslySetInnerHTML={{ __html: hl(pretty) }} />
        : <pre className="code-block">{pretty}</pre>}
      <AG_ReferencesPanel agent={agent} />
    </div>
  );
}

function AG_ReferencesPanel({ agent }) {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const providerId = agent.model?.provider_id;
  const provider = useResource(
    providerId ? `llm-provider:${providerId}` : "llm-provider:none",
    (signal) =>
      providerId
        ? apiFetch("GET", "/llm_providers/" + encodeURIComponent(providerId), null, { signal })
        : Promise.resolve(null),
    { pollMs: null, deps: [providerId] }
  );

  return (
    <div className="mt-3 panel">
      <div className="panel-h">
        <Icon name="fork" size={13} />
        <span>References</span>
      </div>
      <div className="panel-body" style={{ padding: "4px 14px" }}>
        <div className="ref-row">
          <Icon name="llm" size={13} className="ico" />
          <span className="label">LLM provider</span>
          <span className="val">
            <a
              onClick={() => providerId && navigate("/providers/llm/" + providerId)}
              style={{ cursor: providerId ? "pointer" : "default" }}
            >{providerId || "—"}</a>
          </span>
          {provider.loading ? (
            <span className="muted text-sm">checking…</span>
          ) : provider.error?.status === 404 ? (
            <span className="pill pill-failed"><span className="dot"></span>missing</span>
          ) : provider.data ? (
            <span className="pill pill-ended"><span className="dot"></span>ok</span>
          ) : null}
        </div>
        {(() => {
          // agent.tools is a flat list of scoped ids; group by toolset
          // prefix so the overview shows one ref row per source toolset
          // with the count of tools the agent registered from it.
          const groups = new Map();
          for (const sid of agent.tools || []) {
            if (typeof sid !== "string" || !sid.includes("__")) continue;
            const [prefix] = sid.split("__", 1);
            groups.set(prefix, (groups.get(prefix) || 0) + 1);
          }
          if (groups.size === 0) {
            return (
              <div className="ref-row">
                <Icon name="tools" size={13} className="ico" />
                <span className="label">Tools</span>
                <span className="val muted">none registered</span>
              </div>
            );
          }
          return [...groups.entries()]
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([tsId, count]) => (
              <AG_ToolsetRefRow
                key={tsId}
                tsId={tsId}
                registeredCount={count}
                navigate={navigate}
              />
            ));
        })()}
      </div>
    </div>
  );
}

function AG_ToolsetRefRow({ tsId, registeredCount, navigate }) {
  const { useResource, apiFetch } = window.primerApi;
  const tools = useResource(
    `toolset-tools:${tsId}`,
    (signal) => apiFetch("GET", "/toolsets/" + encodeURIComponent(tsId) + "/tools", null, { signal }),
    { pollMs: null, deps: [tsId] }
  );
  const exposedCount = tools.data?.tools?.length;
  const t711 = tools.error?.status === 500;
  return (
    <div className="ref-row">
      <Icon name="tools" size={13} className="ico" />
      <span className="label">Toolset</span>
      <span className="val">
        <a
          onClick={() => !tsId.startsWith("_") && tsId !== "web" && navigate("/toolsets/" + tsId)}
          style={{ cursor: "pointer" }}
        >{tsId}</a>
        <span className="muted text-sm">
          {" · "}{registeredCount} tool{registeredCount === 1 ? "" : "s"} registered
          {exposedCount != null && exposedCount !== registeredCount && (
            <> · {exposedCount} exposed by toolset</>
          )}
        </span>
      </span>
      {tools.loading ? (
        <span className="muted text-sm">…</span>
      ) : t711 ? (
        <span className="pill pill-failed" title="T0711 — MCP-HTTP 500 leak"><span className="dot"></span>T0711</span>
      ) : tools.error ? (
        <span className="pill pill-failed"><span className="dot"></span>err</span>
      ) : (
        <span className="pill pill-ended"><span className="dot"></span>ok</span>
      )}
    </div>
  );
}

// ============================================================================
// Tools tab — per-toolset isolation (U0009 / T0711 contract)
// ============================================================================

function AG_ToolsTab({ agent }) {
  const scopedIds = agent.tools || [];
  // Group the agent's scoped tool ids by their toolset prefix so the
  // page renders one panel per source toolset, each listing only the
  // tools the agent actually registered (never the toolset's full
  // catalogue).
  const grouped = React.useMemo(() => {
    const m = new Map();
    for (const sid of scopedIds) {
      if (typeof sid !== "string" || !sid.includes("__")) continue;
      const [prefix, ...rest] = sid.split("__");
      const bare = rest.join("__");
      if (!m.has(prefix)) m.set(prefix, []);
      m.get(prefix).push({ scoped_id: sid, bare });
    }
    return [...m.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [scopedIds]);

  if (scopedIds.length === 0) {
    return (
      <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>
        No tools registered with this agent.
      </div>
    );
  }
  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">
        {scopedIds.length} tool{scopedIds.length === 1 ? "" : "s"} registered, grouped by source
        toolset. Each card lists the canonical tool <span className="mono">id</span> (T0140/T0141
        — not <span className="mono">name</span>). The toolset itself is NOT implicitly attached;
        only the tools listed below are exposed to the LLM.
      </div>
      {grouped.map(([tsId, entries]) => (
        <AG_ToolsetSection
          key={tsId}
          tsId={tsId}
          registeredBareIds={entries.map((e) => e.bare)}
        />
      ))}
    </div>
  );
}

function AG_ToolsetSection({ tsId, registeredBareIds }) {
  const { useResource, apiFetch } = window.primerApi;
  const tools = useResource(
    `toolset-tools:${tsId}`,
    (signal) => apiFetch("GET", "/toolsets/" + encodeURIComponent(tsId) + "/tools", null, { signal }),
    { pollMs: null, deps: [tsId] }
  );
  // Filter the toolset's full catalogue to just the bare tool ids the
  // agent actually registered — the operator picks per-tool, so the
  // detail view must match that scope, not surface the entire toolset.
  const registeredSet = React.useMemo(
    () => new Set(registeredBareIds || []),
    [registeredBareIds],
  );

  // T0711 MCP-HTTP leak — for any toolset returning 500, surface the
  // anomaly block instead of crashing the rest of the page (U0009).
  if (tools.error?.status === 500) {
    return (
      <div className="panel" style={{ marginBottom: 14 }}>
        <div className="panel-h">
          <Icon name="tools" size={12} className="muted" />
          <span className="mono">{tsId}</span>
          <span className="pill pill-failed" style={{ marginLeft: 6 }}><span className="dot"></span>T0711</span>
        </div>
        <div className="panel-body">
          <Banner
            kind="error"
            title="Tools list unavailable"
            detail="The documented bug pinned by T0711 — MCP-HTTP transport leaks 500/errors/internal when the remote server is unreachable. Visit the toolset detail to Invalidate the cached provider and retry."
            actions={<Btn size="sm" icon="refresh" onClick={tools.refetch}>Retry</Btn>}
          />
        </div>
      </div>
    );
  }
  if (tools.error) {
    return (
      <div className="panel" style={{ marginBottom: 14 }}>
        <div className="panel-h">
          <Icon name="tools" size={12} className="muted" />
          <span className="mono">{tsId}</span>
          <span className="pill pill-failed" style={{ marginLeft: 6 }}><span className="dot"></span>error</span>
        </div>
        <div className="panel-body">
          <Banner
            kind="error"
            title={tools.error.title || "Couldn't load tools"}
            detail={tools.error.detail || tools.error.message}
            actions={<Btn size="sm" icon="refresh" onClick={tools.refetch}>Retry</Btn>}
          />
        </div>
      </div>
    );
  }
  if (tools.loading && !tools.data) {
    return (
      <div className="panel" style={{ marginBottom: 14 }}>
        <div className="panel-h">
          <Icon name="tools" size={12} className="muted" />
          <span className="mono">{tsId}</span>
        </div>
        <div className="panel-body">
          <div className="muted text-sm" style={{ textAlign: "center" }}>Loading…</div>
        </div>
      </div>
    );
  }
  const allItems = tools.data?.tools || [];
  // Show only tools the agent actually registered. If the toolset
  // metadata is reachable, intersect with registeredBareIds; if the
  // toolset returned them in a different order than the agent's
  // ``tools`` field, that's fine — we still want the agent's surface.
  const items = registeredSet.size > 0
    ? allItems.filter((t) => registeredSet.has(t.id))
    : allItems;
  // Detect bare ids the agent registered that the toolset no longer
  // exposes (e.g. the MCP server dropped a tool between agent create
  // and now). Surface them as stale rows so the operator can see what
  // needs cleanup.
  const reachableBareIds = new Set(allItems.map((t) => t.id));
  const staleBareIds = [...registeredSet].filter((b) => !reachableBareIds.has(b));
  return (
    <div className="panel" style={{ marginBottom: 14 }}>
      <div className="panel-h">
        <Icon name="tools" size={12} className="muted" />
        <span className="mono">{tsId}</span>
        <span className="sub">· {items.length + staleBareIds.length} registered</span>
      </div>
      <div className="panel-body" style={{ padding: 0 }}>
        {items.length === 0 && staleBareIds.length === 0 ? (
          <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }}>No tools.</div>
        ) : (
          <>
            {items.map((tool, i) => <AG_ToolEntry key={tool.id || i} tool={tool} />)}
            {staleBareIds.map((bare) => (
              <div key={`stale-${bare}`} style={{ borderTop: "1px solid var(--border)", padding: "8px 14px", display: "flex", alignItems: "center", gap: 8 }}>
                <Icon name="alert" size={11} style={{ color: "var(--amber)" }} />
                <span className="mono" style={{ flex: 1 }}>{bare}</span>
                <span className="pill pill-paused"><span className="dot"></span>not currently exposed</span>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}

function AG_ToolEntry({ tool }) {
  const [open, setOpen] = React.useState(false);
  const hl = window.primerVendor?.highlightJson;
  return (
    <div style={{ borderBottom: "1px solid var(--border)" }}>
      <div
        style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 14px", cursor: "pointer" }}
        onClick={() => setOpen(!open)}
      >
        <Icon name={open ? "chevron-down" : "chevron-right"} size={11} className="muted" />
        <span className="mono" style={{ flex: 1, minWidth: 0 }}>{tool.id}</span>
        {tool.description && (
          <span className="muted text-sm" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {tool.description}
          </span>
        )}
        <Btn
          size="sm"
          kind="ghost"
          disabled
          title="Tool invocation endpoint not yet implemented (planned — backend-additions §2.2)"
        >Test call</Btn>
      </div>
      {open && tool.schema && (
        <div style={{ padding: "8px 14px 12px" }}>
          {hl
            ? <div className="code-block" dangerouslySetInnerHTML={{ __html: hl(JSON.stringify(tool.schema, null, 2)) }} />
            : <pre className="code-block">{JSON.stringify(tool.schema, null, 2)}</pre>}
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Sessions tab — server-filtered by agent_id
// ============================================================================

function AG_SessionsTab({ agentId }) {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const sessions = useResource(
    `agent-sessions:${agentId}`,
    (signal) => apiFetch("GET", "/sessions?agent_id=" + encodeURIComponent(agentId) + "&limit=200", null, { signal }),
    { pollMs: 5000, deps: [agentId] }
  );
  const items = sessions.data?.items ?? [];
  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">
        Sessions bound to <span className="mono">{agentId}</span>, server-filtered.
      </div>
      {sessions.loading && items.length === 0 ? (
        <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }}>Loading…</div>
      ) : items.length === 0 ? (
        <div className="empty" style={{ padding: "30px 20px" }}>
          <div className="ico-wrap"><Icon name="zap" size={18} /></div>
          <div className="head">No sessions</div>
          <div className="sub">Use the Test agent button above to start one.</div>
        </div>
      ) : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr><th>Status</th><th>Session</th><th>Workspace</th><th>Turns</th><th>Created</th></tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr
                  key={s.id || s.session_id}
                  onClick={() => navigate("/sessions/" + (s.id || s.session_id))}
                  style={{ cursor: "pointer" }}
                >
                  <td><StatusPill status={s.status} /></td>
                  <td className="mono">{s.id || s.session_id}</td>
                  <td className="mono muted">
                    {(s.workspace_id || "").slice(0, 18)}
                    {s.workspace_id && s.workspace_id.length > 18 ? "…" : ""}
                  </td>
                  <td className="mono num tabular">{s.turn_count ?? 0}</td>
                  <td className="mono muted">
                    {s.created_at
                      ? relativeTime((Date.now() - new Date(s.created_at).getTime()) / 1000)
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Metadata tab
// ============================================================================

function AG_MetadataTab({ agent }) {
  const meta = agent.metadata || {};
  const keys = Object.keys(meta);
  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">
        Free-form key/value bag on the agent row. {keys.length} key{keys.length === 1 ? "" : "s"}.
      </div>
      {keys.length === 0 ? (
        <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }}>No metadata.</div>
      ) : (
        <dl className="kv" style={{ gridTemplateColumns: "200px 1fr" }}>
          {keys.map((k) => (
            <React.Fragment key={k}>
              <dt>{k}</dt>
              <dd className="mono">{typeof meta[k] === "object" ? JSON.stringify(meta[k]) : String(meta[k])}</dd>
            </React.Fragment>
          ))}
        </dl>
      )}
    </div>
  );
}

// ============================================================================
// Test agent → cross-page NewSessionModal
// ============================================================================
//
// U0082 contract: opens with title="New session", Workspace select is
// .nth(0), Agent select is .nth(1), Agent pre-bound to defaultAgentId,
// workspace options populated from /workspaces?limit=200.

function AG_NewSessionModal({ onClose, defaultAgentId, pushToast }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();

  const workspaces = useResource(
    "test-agent:workspaces",
    (signal) => apiFetch("GET", "/workspaces?limit=200", null, { signal }),
    { pollMs: null }
  );
  const agents = useResource(
    "test-agent:agents",
    (signal) => apiFetch("GET", "/agents?limit=200", null, { signal }),
    { pollMs: null }
  );

  const wsItems = workspaces.data?.items ?? [];
  const agentItems = agents.data?.items ?? [];

  const [workspaceId, setWorkspaceId] = React.useState("");
  const [agentId, setAgentId] = React.useState(defaultAgentId || "");
  const [instructions, setInstructions] = React.useState("");
  const [autoStart, setAutoStart] = React.useState(true);

  // Auto-pick first workspace once the list loads (only if none picked).
  React.useEffect(() => {
    if (!workspaceId && wsItems.length > 0) setWorkspaceId(wsItems[0].id);
  }, [wsItems, workspaceId]);

  // Defensive: keep defaultAgentId sticky even if user opens / changes
  // selection then re-opens.
  React.useEffect(() => {
    if (defaultAgentId) setAgentId(defaultAgentId);
  }, [defaultAgentId]);

  const create = useMutation(
    ({ wid, body }) => apiFetch("POST", `/workspaces/${encodeURIComponent(wid)}/sessions`, body),
    {
      invalidates: ["sessions:list"],
      onSuccess: (row) => {
        onClose();
        if (typeof pushToast === "function") {
          pushToast({ kind: "success", title: "Session created", detail: row?.id });
        }
        if (row?.id) navigate("/sessions/" + row.id);
      },
      onError: _agToastErr(pushToast, "Create session failed"),
    }
  );

  const submit = async () => {
    if (!workspaceId || !agentId) return;
    const body = {
      binding: { kind: "agent", agent_id: agentId },
      auto_start: autoStart,
    };
    if (instructions.trim()) body.initial_instructions = instructions.trim();
    try { await create.mutate({ wid: workspaceId, body }); } catch (_e) { /* surfaced via onError */ }
  };

  return (
    <Modal
      title="New session"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn
            kind="primary"
            icon="plus"
            onClick={submit}
            disabled={!workspaceId || !agentId || create.loading}
          >
            {create.loading ? "Creating…" : "Create"}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label" htmlFor="ns-workspace">Workspace</label>
        <select
          id="ns-workspace"
          className="select"
          value={workspaceId}
          onChange={(e) => setWorkspaceId(e.target.value)}
          style={{ width: "100%" }}
        >
          {wsItems.length === 0 && <option value="">-- no workspaces --</option>}
          {wsItems.map((w) => (
            <option key={w.id} value={w.id}>{w.id}</option>
          ))}
        </select>
        {wsItems.length === 0 && !workspaces.loading && (
          <div className="field-help" style={{ color: "var(--amber)" }}>
            No workspaces. Create one at <span className="mono">/workspaces</span> first.
          </div>
        )}
      </div>
      <div className="field">
        <label className="field-label" htmlFor="ns-agent">Agent</label>
        <select
          id="ns-agent"
          className="select"
          value={agentId}
          onChange={(e) => setAgentId(e.target.value)}
          style={{ width: "100%" }}
        >
          {/* Keep defaultAgentId as an option even if not yet in the loaded
              list — guarantees U0082's preselect-by-prop holds while the
              agents.list refetch is in flight. */}
          {defaultAgentId && !agentItems.some((a) => a.id === defaultAgentId) && (
            <option value={defaultAgentId}>{defaultAgentId}</option>
          )}
          {agentItems.map((a) => (
            <option key={a.id} value={a.id}>{a.id}</option>
          ))}
        </select>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="ns-instructions">
          Initial instructions <span className="hint">optional</span>
        </label>
        <textarea
          id="ns-instructions"
          className="textarea"
          rows={4}
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          placeholder="Tell the agent what to do…"
        />
      </div>
      <div className="field">
        <label className="field-label" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <input
            type="checkbox"
            checked={autoStart}
            onChange={(e) => setAutoStart(e.target.checked)}
          />
          <span>auto_start</span>
        </label>
        <div className="field-help">If unchecked, the session is created but not handed to a worker — useful for staging.</div>
      </div>
    </Modal>
  );
}

window.AgentsPage = AgentsPage;
window.AgentDetail = AgentDetail;
// Exposed so other components (graphs.jsx) can offer inline
// agent creation without juggling pages.
window.AG_NewAgentModal = AG_NewAgentModal;
