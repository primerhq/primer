/* global React, Icon, StatusPill, Btn, Modal, Banner, CardList, Card, Fab, relativeTime */

// Agents page + detail wired to the real API. The Designer's mock-data
// scaffold was replaced in Phase 2 — every fetch goes through
// window.primerApi.{apiFetch, useResource, useMutation}. Cache-key convention
// follows other components: "agents:list", "agent-detail:${aid}",
// "agent-status:${aid}", "agent-sessions:${aid}". The tool catalogue
// itself is owned by the shared ToolPicker (ui/components/shared/
// tool-picker.jsx, cache key "tool-picker:catalogue") since uiv2 Wave 2 -
// this file no longer fetches /tools directly.
//
// Babel-standalone shares the global scope across <script> tags so every
// top-level binding in this file is prefixed with AG_ to avoid name clashes
// with providers.jsx (PROVIDER_FIELDS) and workspaces.jsx (WS_TERMINAL).

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

function AgentsPage({ onOpen, pushToast, startCreate }) {
  const { useResource, useRouter, useViewport, apiFetch, usePagedList, Pager } = window.primerApi;
  const { navigate } = useRouter();
  const { isMobile } = useViewport();

  const [createOpen, setCreateOpen] = React.useState(false);
  const [textFilter, setTextFilter] = React.useState("");
  const filterFocused = React.useRef(false);
  // "New agent" from the platform page opens the form DIRECTLY -
  // landing on this list with a second button was a two-step detour.
  React.useEffect(() => {
    if (startCreate) setCreateOpen(true);
  }, [startCreate]);

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
  // An agent names a model PROFILE, not a provider+model pair. The profile
  // is what carries the provider, the wire model name, and the API-level
  // config, so the list resolves it to show the vendor dot and the
  // underlying model -- two agents on different profiles may share a model.
  const caps = window.primerApi.useCapabilities();
  const voices = window.primerApi.useResource(
    "agent-voices",
    (signal) => window.primerApi.apiFetch("GET", "/audio/voices", null, { signal }),
    { pollMs: 0 },
  );
  const profiles = useResource(
    "agents:model-profiles",
    (signal) => apiFetch("GET", "/model_profiles?limit=200", null, { signal }),
    { pollMs: null }
  );
  const profileById = React.useMemo(() => {
    const m = {};
    (profiles.data?.items ?? []).forEach((pr) => { m[pr.id] = pr; });
    return m;
  }, [profiles.data]);

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
              const profileId = a.model?.profile_id;
              const profile = profileById[profileId];
              const providerId = profile?.provider_id;
              const modelName = profile?.model_name;
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
                  subtitle={profileId
                    ? <span className="mono">
                        <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: vendorColor, marginRight: 6 }}></span>
                        {profileId}{providerId ? <span className="muted"> · {providerId}/{modelName}</span> : <span className="muted"> · (missing profile)</span>}
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
              <th>Model profile</th>
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
              const profileId = a.model?.profile_id;
              const profile = profileById[profileId];
              const providerId = profile?.provider_id;
              const modelName = profile?.model_name;
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
                    {profileId
                      ? <>
                          <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: vendorColor, marginRight: 6 }}></span>
                          {profileId}{providerId ? <span className="muted"> · {providerId}/{modelName}</span> : <span className="muted"> · (missing profile)</span>}
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
// AG_Toggle — sliding switch, mirrors CH_Toggle (channels.jsx) / SSO_Toggle.
// ============================================================================

function AG_Toggle({ checked, onChange, label, help, disabled, testid }) {
  return (
    <label
      style={{
        display: "flex", alignItems: "flex-start", gap: 10,
        cursor: disabled ? "default" : "pointer", opacity: disabled ? 0.5 : 1,
      }}
    >
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        data-testid={testid}
        onClick={() => !disabled && onChange(!checked)}
        style={{
          flex: "0 0 auto", width: 34, height: 20, borderRadius: 999,
          border: "1px solid var(--border)", padding: 0, marginTop: 1,
          background: checked ? "var(--accent)" : "var(--bg-2)",
          position: "relative", cursor: disabled ? "default" : "pointer",
          transition: "background 0.12s ease",
        }}
      >
        <span
          style={{
            position: "absolute", top: 1, left: checked ? 15 : 1,
            width: 16, height: 16, borderRadius: "50%",
            background: checked ? "var(--accent-fg)" : "var(--text-3)",
            transition: "left 0.12s ease",
          }}
        />
      </button>
      <span style={{ fontSize: 12.5, lineHeight: 1.4 }}>
        {label}
        {help && <span className="muted"> — {help}</span>}
      </span>
    </label>
  );
}


// ============================================================================
// New agent modal
// ============================================================================

function AG_ProfilePicker({ profiles, loading, value, onChange, missingId }) {
  // uiv2 Wave 2: the mockup renders model profiles as stacked
  // selectable rows (bound one green-bordered/green-mono, the rest
  // plain) rather than a <select> - same data (GET /model_profiles),
  // just a richer picker matching the reference exactly.
  return (
    <div className="col" style={{ gap: 6 }} data-testid="agent-profile-picker">
      {missingId && (
        <div style={{
          border: "1px solid var(--red)", borderRadius: 6, padding: "8px 10px",
          color: "var(--red)",
        }} data-testid="agent-profile-row-missing">
          <span className="mono">{missingId}</span> <span className="text-sm">(missing) — pick another below</span>
        </div>
      )}
      {loading && profiles.length === 0 && (
        <div className="muted text-sm" style={{ padding: 10 }}>Loading model profiles…</div>
      )}
      {!loading && profiles.length === 0 && (
        <div className="field-help" style={{ color: "var(--amber)" }}>
          No model profiles configured. Create one at <span className="mono">/providers?class=llm</span> first.
        </div>
      )}
      {profiles.map((pr) => {
        const active = pr.id === value;
        return (
          <button type="button" key={pr.id}
            onClick={() => onChange(pr.id)}
            data-testid={`agent-profile-row-${pr.id}`}
            data-active={active ? "true" : "false"}
            style={{
              display: "flex", flexDirection: "column", alignItems: "flex-start", gap: 2,
              textAlign: "left", padding: "8px 10px", borderRadius: 6, cursor: "pointer",
              border: "1px solid " + (active ? "var(--accent)" : "var(--border)"),
              background: active ? "var(--accent-dim)" : "var(--bg-1)",
            }}>
            <span className="mono" style={{ color: active ? "var(--accent)" : "var(--text)", fontSize: 12.5 }}>{pr.id}</span>
            <span className="muted text-sm" style={{ fontSize: 11 }}>
              {pr.provider_id}/{pr.model_name}
              {pr.config?.reasoning ? ` · reasoning ${pr.config.reasoning}` : ""}
            </span>
          </button>
        );
      })}
    </div>
  );
}

// uiv2 Wave 2: everything JSON-visible with no mockup form field
// (description was already homed; response_format/allow_external_tools/
// compaction_tool_access are real but rare enough the mockup's own
// minimal example never shows them) - kept reachable behind one
// collapsed disclosure rather than cluttering the two-column layout.
function AG_AdvancedDisclosure({ open, onToggle, children }) {
  // Controlled (not self-contained state): submit() must be able to
  // force this open when response_format fails to parse, the same way
  // the old tabbed layout jumped to the Advanced tab on that error.
  return (
    <div className="panel" data-testid="agent-advanced-disclosure">
      <button type="button" onClick={onToggle}
        style={{
          display: "flex", alignItems: "center", gap: 6, width: "100%",
          background: "none", border: "none", cursor: "pointer", padding: "8px 10px",
          color: "var(--text-2)", fontSize: 12,
        }}>
        <Icon name={open ? "chevron-down" : "chevron-right"} size={11} />
        Advanced
      </button>
      {open && (
        <div style={{ padding: "0 10px 10px", display: "flex", flexDirection: "column", gap: 12 }}>
          {children}
        </div>
      )}
    </div>
  );
}

// uiv2 Wave 2 (a-6, re-homed as a collapsible panel rather than its own
// tab): unchanged fetch/table from the old Sessions tab, just a smaller
// footprint so it fits the consolidated form's right column.
function AG_SessionsPanel({ agentId }) {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const [open, setOpen] = React.useState(false);
  const sessions = useResource(
    `agent-sessions:${agentId}`,
    (signal) => apiFetch("GET", "/sessions?agent_id=" + encodeURIComponent(agentId) + "&limit=200", null, { signal }),
    { pollMs: open ? 5000 : 0, deps: [agentId] }
  );
  const items = sessions.data?.items ?? [];
  return (
    <div className="panel" data-testid="agent-sessions-panel">
      <button type="button" onClick={() => setOpen((v) => !v)}
        style={{
          display: "flex", alignItems: "center", gap: 6, width: "100%",
          background: "none", border: "none", cursor: "pointer", padding: "8px 10px",
          color: "var(--text-2)", fontSize: 12,
        }}>
        <Icon name={open ? "chevron-down" : "chevron-right"} size={11} />
        Sessions{items.length ? ` (${items.length})` : ""}
      </button>
      {open && (
        <div style={{ padding: "0 10px 10px" }}>
          {sessions.loading && items.length === 0 ? (
            <div className="muted text-sm" style={{ padding: 10 }}>Loading…</div>
          ) : items.length === 0 ? (
            <div className="muted text-sm" style={{ padding: 10 }}>
              No sessions. Use Chat below to start one.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {items.slice(0, 10).map((s) => (
                <div key={s.id || s.session_id}
                  onClick={() => navigate("/sessions/" + (s.id || s.session_id))}
                  style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 11.5 }}>
                  <StatusPill status={s.status} />
                  <span className="mono muted" style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {s.id || s.session_id}
                  </span>
                  <span className="muted text-sm">
                    {s.created_at ? relativeTime((Date.now() - new Date(s.created_at).getTime()) / 1000) : "—"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function AG_NewAgentModal({ onClose, onCreate, pushToast, existing, status, onDelete, onChat, chatLoading }) {
  // Same modal serves both create (existing == null) and edit
  // (existing == agent row). In edit mode the id field is locked,
  // submit PUT-replaces, and the success callback is just close().
  const isEdit = !!existing;
  // Harness-managed rows 409 on any PUT (routers/agents.py) - render a
  // locked notice instead of a form that would fail on Save. Same
  // capability AG_ConfigTab's old isManaged check had (hiding Edit
  // entirely); this is that same check, just the only path left now
  // that direct-edit is the landing view instead of Edit being a
  // second click away.
  const isManaged = isEdit && !!existing.harness_id;
  const { useResource, useMutation, apiFetch } = window.primerApi;
  // The agent's model field is a single profile id. A profile already
  // pins the provider, the wire model name and the API-level config, so
  // the form picks one row rather than a provider+model pair.
  const profiles = useResource(
    "agents:model-profiles",
    (signal) => apiFetch("GET", "/model_profiles?limit=200", null, { signal }),
    { pollMs: null }
  );
  // The Advanced tab gates a tts_voice picker on the speech capability
  // and fills it from the voice list. Both were read here while only
  // AgentsPage fetched them, so opening this modal and reaching that
  // tab threw ReferenceError: caps is not defined, and the whole tab
  // rendered nothing -- taking the rest of its fields down with it.
  const caps = window.primerApi.useCapabilities();
  const voices = useResource(
    "agents:voices",
    (signal) => apiFetch("GET", "/audio/voices", null, { signal }),
    { pollMs: 0 },
  );

  // Initial values come from the existing agent in edit mode, else
  // blanks. compaction_prompt is stored as an array server-side but the
  // form only handles a single line there (unchanged this wave), so it
  // still collapses ["a", "b"] → "a\n\nb" on read and emits one entry on
  // save.
  const _joinPrompt = (p) => Array.isArray(p) ? p.join("\n\n") : (p || "");
  const _initialTools = () => {
    const t = existing?.tools;
    return new Set(Array.isArray(t) ? t : []);
  };
  // Platform wave P1b item 8: system_prompt is ALREADY list[str] server-
  // side (primer/model/agent.py:143, joined with "\n\n" at prompt-render
  // time) - the only gap was this form flattening it to one textarea. A
  // saved agent with N parts loads as N textareas; a legacy/absent value
  // defaults to one empty part so the editor always has at least one row.
  const _initialSystemPromptParts = () => {
    const p = existing?.system_prompt;
    return Array.isArray(p) && p.length ? p : [""];
  };

  const [id, setId] = React.useState(existing?.id || "");
  const [description, setDescription] = React.useState(existing?.description || "");
  const [profileId, setProfileId] = React.useState(existing?.model?.profile_id || "");
  const [systemPromptParts, setSystemPromptParts] = React.useState(_initialSystemPromptParts);
  const [compactionPrompt, setCompactionPrompt] = React.useState(_joinPrompt(existing?.compaction_prompt));
  const [compactionToolAccess, setCompactionToolAccess] = React.useState(existing?.compaction_tool_access ?? false);
  const [allowExternalTools, setAllowExternalTools] = React.useState(existing?.allow_external_tools ?? false);
  const [ttsVoice, setTtsVoice] = React.useState(existing?.tts_voice ?? null);
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
  // uiv2 Wave 2 (a-11): the two numerics that had NO control anywhere in
  // the app before this wave (grep-confirmed against agents.jsx pre-
  // change). max_tool_turns has a real server default (50, not null) -
  // initializing from it rather than leaving the field blank means a
  // save that never touches this field still round-trips the agent's
  // real value instead of PUT-replacing it back down to the schema
  // default.
  const [maxToolTurns, setMaxToolTurns] = React.useState(
    String(existing?.max_tool_turns ?? 50)
  );
  const [maxOutputTokens, setMaxOutputTokens] = React.useState(
    existing?.max_output_tokens != null ? String(existing.max_output_tokens) : ""
  );
  const [showAdvanced, setShowAdvanced] = React.useState(false);
  // Raw-JSON + cross-reference view (synthesis doc: "JSON can survive as
  // a secondary/advanced view but must not be the landing IA") - edit
  // mode only, folds AG_ConfigTab's read-only render + AG_ReferencesPanel
  // into one disclosure rather than a whole tab.
  const [showJson, setShowJson] = React.useState(false);

  React.useEffect(() => {
    if (!profileId && profiles.data?.items?.length) {
      setProfileId(profiles.data.items[0].id);
    }
  }, [profiles.data, profileId]);

  const profileOptions = profiles.data?.items ?? [];
  // An agent may already name a profile that has since been deleted. Keep
  // it in the list so editing an unrelated field does not silently repoint
  // the agent at whatever happens to sort first.
  const selectedProfile = profileOptions.find((pr) => pr.id === profileId);
  const profileMissing = !!profileId && !selectedProfile && !profiles.loading;

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
        setShowAdvanced(true);
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
      model: { profile_id: profileId },
      tools,
      // Empty parts dropped on save - an add-then-leave-blank part must
      // not send a hole in the array.
      system_prompt: systemPromptParts.map((p) => p.trim()).filter(Boolean),
      compaction_prompt: compactionPrompt ? [compactionPrompt] : [],
      compaction_tool_access: compactionToolAccess,
      allow_external_tools: allowExternalTools,
      tts_voice: ttsVoice,
    };
    if (temperature !== "" && !Number.isNaN(+temperature)) {
      body.temperature = Number(temperature);
    }
    // uiv2 Wave 2 (a-11): always sent, never conditionally dropped - see
    // the maxToolTurns state comment above for why (real non-null server
    // default means omitting it on a PUT-replace save would silently
    // reset a customized value).
    const parsedMaxToolTurns = Number(maxToolTurns);
    body.max_tool_turns = Number.isFinite(parsedMaxToolTurns) && parsedMaxToolTurns > 0
      ? Math.floor(parsedMaxToolTurns) : 50;
    // max_output_tokens defaults to null (unbounded) - same
    // always-on-edit/conditional-on-create pattern as response_format.
    let maxOutputTokensValue = null;
    if (maxOutputTokens.trim() !== "") {
      const n = Number(maxOutputTokens);
      if (Number.isFinite(n) && n > 0) maxOutputTokensValue = Math.floor(n);
    }
    if (maxOutputTokensValue !== null || isEdit) {
      body.max_output_tokens = maxOutputTokensValue;
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
  const verbChip = (
    <span className="pc-modal-chip mono text-sm muted"
      data-testid="agent-modal-verb-chip"
      style={{ marginLeft: 10, marginBottom: 0, verticalAlign: "middle" }}>
      verb: {isEdit ? "Edit" : "Create"} Agent
    </span>
  );
  // Edit mode identifies the specific row in the title itself (same
  // "Noun — id" pattern as KN_CollectionDetail) - the verb chip alone
  // ("Agent · verb: Edit Agent") lost the agent id when this overlay
  // stopped delegating to NV_OverlayPanel's own id-bearing title.
  const modalTitle = (
    <h1 className="page-title" style={{ font: "inherit", margin: 0, display: "inline" }}>
      {isEdit ? `Agent — ${existing.id}` : "Agent"}
      {verbChip}
    </h1>
  );

  // uiv2 Wave 2 (harness_id): a managed row 409s on any PUT
  // (routers/agents.py) - AG_ConfigTab used to just hide its Edit
  // button and leave the raw-JSON view up; now that direct-edit IS the
  // landing view, a locked summary replaces the form outright rather
  // than rendering inputs Save can never persist.
  if (isManaged) {
    return (
      <Modal
        title={modalTitle}
        onClose={onClose}
        footer={<Btn kind="ghost" onClick={onClose}>Close</Btn>}
      >
        <Banner kind="info" title={`Managed by harness ${existing.harness_id}`}
          detail="Direct edits are blocked - update the harness's sync/uninstall flow instead." />
        <div className="col" style={{ gap: 10, marginTop: 14 }}>
          <div><span className="field-label">Name</span><div className="mono">{existing.id}</div></div>
          <div><span className="field-label">Description</span><div>{existing.description}</div></div>
          <div><span className="field-label">Model profile</span><div className="mono">{existing.model?.profile_id || "—"}</div></div>
          <div><span className="field-label">Tools</span><div>{(existing.tools || []).length} registered</div></div>
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      title={modalTitle}
      width={760}
      onClose={onClose}
      footer={
        <>
          {isEdit && onDelete && (
            <Btn kind="ghost" onClick={onDelete} style={{ marginRight: "auto", color: "var(--red)" }}>Delete</Btn>
          )}
          {isEdit && onChat && (
            <Btn kind="ghost" icon="send" onClick={onChat} disabled={chatLoading}>
              {chatLoading ? "Opening chat…" : "Chat"}
            </Btn>
          )}
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn
            kind="primary"
            icon={isEdit ? "check" : "plus"}
            onClick={submit}
            disabled={!profileId || create.loading}
          >
            {create.loading ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save changes" : "Create")}
          </Btn>
        </>
      }
    >
      {isEdit && <AG_StatusPanel id={existing.id} status={status} />}
      <div style={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) 340px",
        gap: 22,
        marginTop: isEdit ? 14 : 0,
      }}>
        <div className="col" style={{ gap: 14, minWidth: 0 }}>
          <div className="field">
            <label className="field-label" htmlFor="na-id">
              Name {isEdit
                ? <span className="hint">locked — id cannot change after create</span>
                : <span className="hint">optional — backend assigns if blank</span>}
            </label>
            <input
              id="na-id"
              className="input"
              value={id}
              onChange={(e) => setId(e.target.value)}
              placeholder="e.g. refund-triage"
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
            <label className="field-label" id="na-model-profile-label">
              Model profile <span className="hint">default, overridable per run</span>
            </label>
            <AG_ProfilePicker
              profiles={profileOptions}
              loading={profiles.loading}
              value={profileId}
              onChange={setProfileId}
              missingId={profileMissing ? profileId : null}
            />
            <div className="field-help">
              This is the agent's DEFAULT model. A session or chat may name a
              different profile at invocation time.
            </div>
            {fieldErrors["body.model.profile_id"] && (
              <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.model.profile_id"]}</div>
            )}
          </div>
          {/* uiv2 Wave 2 (a-11): the numerics live right under the
              profile picker, not tucked behind Advanced - they gate the
              same model call the profile does. */}
          <div style={{ display: "flex", gap: 10 }}>
            <div className="field" style={{ flex: 1 }}>
              <label className="field-label" htmlFor="na-temperature">
                Temperature <span className="hint">optional</span>
              </label>
              <input id="na-temperature" className="input" type="number" step="0.05" min="0"
                value={temperature} onChange={(e) => setTemperature(e.target.value)} style={{ width: "100%" }} />
              {fieldErrors["body.temperature"] && (
                <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.temperature"]}</div>
              )}
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label className="field-label" htmlFor="na-max-tool-turns">Max tool turns</label>
              <input id="na-max-tool-turns" className="input" type="number" step="1" min="1"
                value={maxToolTurns} onChange={(e) => setMaxToolTurns(e.target.value)} style={{ width: "100%" }} />
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label className="field-label" htmlFor="na-max-output-tokens">
                Max output tokens <span className="hint">optional</span>
              </label>
              <input id="na-max-output-tokens" className="input" type="number" step="1" min="1"
                value={maxOutputTokens} onChange={(e) => setMaxOutputTokens(e.target.value)} style={{ width: "100%" }} />
            </div>
          </div>
          <div className="field">
            <label className="field-label">
              System prompt <span className="hint">optional · parts</span>
            </label>
            {/* Platform wave P1b item 8: repeatable textarea list - one
                array element per part, no delimiter tricks. Agent.
                system_prompt is already list[str] (primer/model/agent.py:
                143), joined with "\n\n" at prompt-render time; this is FE
                work on the existing field, not a new one. */}
            {systemPromptParts.map((part, i) => (
              <div key={i} style={{ display: "flex", gap: 6, alignItems: "flex-start", marginBottom: 6 }}>
                <textarea
                  data-testid={`agent-system-prompt-part-${i}`}
                  className="textarea"
                  style={{ flex: 1 }}
                  value={part}
                  onChange={(e) => {
                    const next = systemPromptParts.slice();
                    next[i] = e.target.value;
                    setSystemPromptParts(next);
                  }}
                  rows={3}
                />
                <Btn kind="ghost" size="sm" icon="x"
                  data-testid={`agent-system-prompt-remove-${i}`}
                  disabled={systemPromptParts.length === 1}
                  title={systemPromptParts.length === 1
                    ? "at least one part stays in the editor"
                    : "remove this part"}
                  onClick={() => setSystemPromptParts(
                    systemPromptParts.filter((_, j) => j !== i))}
                />
              </div>
            ))}
            <Btn kind="ghost" size="sm" icon="plus"
              data-testid="agent-system-prompt-add"
              onClick={() => setSystemPromptParts(systemPromptParts.concat([""]))}>
              Add part
            </Btn>
          </div>
          {/* uiv2 Wave 2 (b-2): a separately-labeled field, not a second
              unlabeled textarea under the System prompt heading - the
              schema already treats these as distinct concepts
              (compaction_prompt is its own field, system_prompt an
              unbounded ordered list, confirmed live pre-wave). One-pass
              adjustable if the user rules b-2 differently. */}
          <div className="field">
            <label className="field-label" htmlFor="na-compaction-prompt">
              Compaction prompt <span className="hint">optional · used when the conversation outgrows the LLM context window</span>
            </label>
            <textarea
              id="na-compaction-prompt"
              className="textarea"
              value={compactionPrompt}
              onChange={(e) => setCompactionPrompt(e.target.value)}
              rows={3}
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
            {/* Platform wave P1b item 4 / uiv2 Wave 2 ruling: the
                reference shows an "Autonomous" inherit/on/off segment,
                but autonomous (workspace_session.py:421) lives on the
                SESSION binding, not the Agent definition - Agent has no
                such field, and neither /agents create nor update accepts
                one (confirmed against primer/model/agent.py). Rendering
                a segment that silently no-ops would lie about what Save
                does, so it stays disabled with an explanatory note
                (August-round precedent) rather than a fake write or a
                unilateral backend addition - flagged to the user as its
                own open decision. */}
            <span>Autonomous</span>
            <div className="chip-group" data-testid="agent-autonomous-segment"
              style={{ opacity: 0.55, pointerEvents: "none", width: "fit-content" }}
              aria-disabled="true">
              {["inherit", "on", "off"].map((v) => (
                <span key={v} className={"chip" + (v === "inherit" ? " active" : "")}>{v}</span>
              ))}
            </div>
            <div className="field-help" data-testid="agent-autonomous-note">
              Autonomous is a per-session control (set at session create or
              binding switch), not part of the agent definition itself -
              there is nothing here for Save to write yet.
            </div>
          </div>
          {!!(caps.data && caps.data.speech && caps.data.speech.tts_configured) && (
            <div className="field">
              <label className="field-label" htmlFor="na-tts-voice">Voice <span className="hint">optional</span></label>
              <select
                id="na-tts-voice"
                className="select"
                data-testid="agent-tts-voice"
                value={ttsVoice || ""}
                onChange={(e) => setTtsVoice(e.target.value || null)}
                style={{ width: "100%" }}
              >
                <option value="">(use the global default)</option>
                {((voices.data && voices.data.voices) || []).map((name) => (
                  <option key={name} value={name}>{name}</option>
                ))}
              </select>
              {ttsVoice ? (
                <div className="field-help" data-testid="agent-voice-pairing-note">
                  {ttsVoice} · pairs with the identity chip
                </div>
              ) : null}
            </div>
          )}
        </div>
        <div className="col" style={{ gap: 12, minWidth: 0 }}>
          <div>
            <label className="field-label">
              Tools <span className="hint">scoped ids — never whole toolsets</span>
            </label>
            <window.ToolPicker selected={selectedScopedIds} onChange={setSelectedScopedIds} pageSize={6} />
          </div>
          {isEdit && <AG_SessionsPanel agentId={existing.id} />}
          <AG_AdvancedDisclosure open={showAdvanced} onToggle={() => setShowAdvanced((v) => !v)}>
            <AG_Toggle
              checked={compactionToolAccess}
              onChange={setCompactionToolAccess}
              testid="na-compaction-tool-access"
              label="Tool access during compaction"
              help="let the compaction prompt call this agent's tools while summarising — e.g. dump the compacted content to workspace files. Runs in a bounded, ephemeral loop; the tool calls don't enter conversation history. Leave off for plain text-only compaction."
            />
            <AG_Toggle
              checked={allowExternalTools}
              onChange={setAllowExternalTools}
              testid="na-allow-external-tools"
              label="Allow external tools"
              help="let API callers attach their own per-invocation tool definitions when invoking this agent. When the model calls one, the turn pauses until the caller responds through the invocation API. Leave off to reject invocation bodies carrying external tools."
            />
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
                rows={5}
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
          </AG_AdvancedDisclosure>
          {isEdit && (
            <AG_JsonDisclosure agent={existing} open={showJson} onToggle={() => setShowJson((v) => !v)} />
          )}
        </div>
      </div>
    </Modal>
  );
}

// ============================================================================
// Agent detail page
// ============================================================================

function AgentDetail({ agentId, pushToast }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.primerApi;
  const { params, navigate } = useRouter();
  const id = agentId || params.id;

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

  // "Chat" button: open an interactive session bound to this agent.
  // No auto_start - the session detail view takes the first message,
  // the same way the old chat detail page did.
  const startChatMut = useMutation(
    () => apiFetch("POST", "/sessions", {
      binding: { kind: "agent", agent_id: id },
      auto_start: false,
    }),
    {
      invalidates: ["sessions:list"],
      onSuccess: (row) => navigate("/sessions/" + row.id),
      onError: (err) => {
        if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err?.title || "Couldn't start session",
            detail: err?.detail || err?.message,
            requestId: err?.requestId,
          });
        }
      },
    }
  );
  const startChat = () => { if (!startChatMut.loading) startChatMut.mutate(); };

  // uiv2 Wave 2: the landing view IS the direct-edit form now (no more
  // Config-tab-with-raw-JSON as the first thing an operator sees) - a
  // loading/error state before AG_NewAgentModal has data to edit still
  // needs its own small standalone rendering, but there is no more
  // action bar to anchor it to (Chat/Delete/Back all moved into the
  // form's own footer, which needs `existing` to exist at all).
  if (detail.loading && !detail.data) {
    return <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>;
  }
  if (detail.error && !detail.data) {
    return (
      <Banner
        kind="error"
        title={detail.error.title || "Couldn't load agent"}
        detail={detail.error.detail || detail.error.message}
        actions={<Btn size="sm" icon="chevron-left" onClick={() => navigate("/agents")}>Back to list</Btn>}
      />
    );
  }

  const a = detail.data;

  return (
    <>
      <AG_NewAgentModal
        existing={a}
        status={status}
        pushToast={pushToast}
        onChat={startChat}
        chatLoading={startChatMut.loading}
        onDelete={() => { setDeleteError(null); setConfirmDelete(true); }}
        onClose={() => navigate("/agents")}
        onCreate={() => {
          if (typeof pushToast === "function") {
            pushToast({ kind: "info", title: "Agent updated", detail: a.id });
          }
          navigate("/agents");
        }}
      />

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
    </>
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

// uiv2 Wave 2: demoted from the landing IA to a secondary disclosure
// (synthesis doc: "JSON can survive as a secondary/advanced view but
// must not be the landing IA") - was AG_ConfigTab, a whole tab with its
// own Edit button; direct-edit is the landing view now so there is no
// more "Edit" mode-switch to render, just the read-only JSON +
// cross-reference check folded under one collapsed toggle.
function AG_JsonDisclosure({ agent, open, onToggle }) {
  const hl = window.primerVendor?.highlightJson;
  const pretty = React.useMemo(() => JSON.stringify(agent, null, 2), [agent]);
  return (
    <div className="panel" data-testid="agent-json-disclosure">
      <button type="button" onClick={onToggle}
        style={{
          display: "flex", alignItems: "center", gap: 6, width: "100%",
          background: "none", border: "none", cursor: "pointer", padding: "8px 10px",
          color: "var(--text-2)", fontSize: 12,
        }}>
        <Icon name={open ? "chevron-down" : "chevron-right"} size={11} />
        View raw config
      </button>
      {open && (
        <div style={{ padding: "0 10px 10px" }}>
          {hl
            ? <div className="code-block" dangerouslySetInnerHTML={{ __html: hl(pretty) }} />
            : <pre className="code-block">{pretty}</pre>}
          <AG_ReferencesPanel agent={agent} />
        </div>
      )}
    </div>
  );
}

function AG_ReferencesPanel({ agent }) {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  // The agent references a model PROFILE; the provider is one hop further
  // out. Both rows are shown because either can go missing independently:
  // a deleted profile and a deleted provider fail the agent in the same
  // way but are fixed in different places.
  const profileId = agent.model?.profile_id;
  const profile = useResource(
    profileId ? `model-profile:${profileId}` : "model-profile:none",
    (signal) =>
      profileId
        ? apiFetch("GET", "/model_profiles/" + encodeURIComponent(profileId), null, { signal })
        : Promise.resolve(null),
    { pollMs: null, deps: [profileId] }
  );
  const providerId = profile.data?.provider_id;
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
          <span className="label">Model profile</span>
          <span className="val">
            <a
              onClick={() => profileId && navigate("/providers", { class: "llm" })}
              style={{ cursor: profileId ? "pointer" : "default" }}
            >{profileId || "—"}</a>
            {profile.data ? (
              <span className="muted text-sm"> · {profile.data.model_name}</span>
            ) : null}
          </span>
          {profile.loading ? (
            <span className="muted text-sm">checking…</span>
          ) : profile.error?.status === 404 ? (
            <span className="pill pill-failed"><span className="dot"></span>missing</span>
          ) : profile.data ? (
            <span className="pill pill-ended"><span className="dot"></span>ok</span>
          ) : null}
        </div>
        <div className="ref-row">
          <Icon name="llm" size={13} className="ico" />
          <span className="label">LLM provider</span>
          <span className="val">
            <a
              onClick={() => providerId && navigate("/providers", { class: "llm", id: providerId })}
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
  // Any 5xx on an MCP-HTTP toolset's /tools (unreachable -> 504, etc.) is the
  // T0711 "tools unavailable" surface, not just the old leaked 500.
  const t711 = tools.error?.status >= 500;
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
        <span className="pill pill-failed" title="T0711 — MCP-HTTP server unreachable"><span className="dot"></span>T0711</span>
      ) : tools.error ? (
        <span className="pill pill-failed"><span className="dot"></span>err</span>
      ) : (
        <span className="pill pill-ended"><span className="dot"></span>ok</span>
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
