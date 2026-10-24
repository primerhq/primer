/* global React, Icon, StatusPill, Btn, Modal, Banner, CardList, Card, Fab, relativeTime, PK_FormOverlay, PK_Grid, PK_Field, PK_Fold */

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
            aria-label="Filter agents"
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
  // The switch and its label are one line; the help sits UNDER them (design 6.2), under the label text, and the switch is described by it.
  const helpId = React.useId();
  return (
    <div>
      <label
        style={{
          display: "flex", alignItems: "center", gap: 10,
          cursor: disabled ? "default" : "pointer", opacity: disabled ? 0.5 : 1,
        }}
      >
        <button
          type="button"
          role="switch"
          aria-checked={checked}
          aria-describedby={help ? helpId : undefined}
          disabled={disabled}
          data-testid={testid}
          onClick={() => !disabled && onChange(!checked)}
          style={{
            flex: "0 0 auto", width: 34, height: 20, borderRadius: 999,
            border: "1px solid var(--border)", padding: 0,
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
        <span data-testid={testid ? testid + "-label" : undefined} style={{ fontSize: 12.5, lineHeight: 1.4 }}>{label}</span>
      </label>
      {help && (
        <div className="field-help" id={helpId} data-testid={testid ? testid + "-help" : undefined} style={{ marginLeft: 44 }}>{help}</div>
      )}
    </div>
  );
}


// ============================================================================
// New agent modal
// ============================================================================

// uiv2 Wave 2 (a-6, re-homed as a collapsible panel rather than its own
// tab): unchanged fetch/table from the old Sessions tab, just a smaller
// footprint so it sits at the foot of the consolidated form.
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
                  onClick={() => navigate("/sessions/" + (s.id || s.session_id), { workspace_id: s.workspace_id })}
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

// What the CREATE form refuses before it posts (admin review ADM-14), as a map under the server's own field paths so a client refusal and a
// server 422 share one display. The id is optional (the backend assigns agent-<hex> when it is blank), but a typed one is permanent and sits
// in URLs and references, so it is lowercase letters, digits, hyphens and single underscores (two in a row are reserved for the ids a harness install
// mints, <slug>__<template>), starting with a letter or digit, at most 63 characters;
// surrounding spaces are not part of it, and a whitespace-only id is a blank one. The description is required: it is how OTHER agents find
// an agent. The id is checked on CREATE only (an existing agent's id is locked, so the edit passes a blank one); the description on create and
// on edit. The backend enforces the same rule on create (primer/agent/agent_checks.py, AGENT_ID_PATTERN); tests/ui/test_agent_form_validation.py
// runs this and fails when the two differ.
function AG_validateNewAgent(id, description) {
  var problems = {};
  var name = String(id || "").trim();
  if (name && !/^(?!.*__)[a-z0-9][a-z0-9_-]{0,62}$/.test(name)) {
    problems["body.id"] = "Use lowercase letters, digits, hyphens and underscores, starting with a letter or digit (up to 63 characters), for example refund-triage. Two underscores in a row are reserved for the agents a harness installs. The id cannot change after the agent is created.";
  }
  if (!String(description || "").trim()) {
    problems["body.description"] = "Describe what this agent is for: other agents find it by its description.";
  }
  return problems;
}

// A number typed into a field that is a limit (Max output tokens): true when it rounds down to less than 1, which no limit can be (0, a negative number, 0.5).
// An empty field is no number and no limit, so it is not "under one"; text the browser cannot read arrives as "" and has its own error (numberHandlers).
function AG_underOne(text) {
  if (String(text == null ? "" : text).trim() === "") return false;
  const n = Number(text);
  return Number.isFinite(n) && Math.floor(n) < 1;
}

// The locked summary of a harness-managed agent: read-only values under their captions, so a definition list (a caption names a value, not a control).
function AG_ManagedSummary({ existing }) {
  return (
    <dl className="col" style={{ gap: 10, marginTop: 14, marginBottom: 0 }}>
      <div><dt className="field-label">Name</dt><dd className="mono" style={{ margin: 0 }}>{existing.id}</dd></div>
      <div><dt className="field-label">Description</dt><dd style={{ margin: 0 }}>{existing.description}</dd></div>
      <div><dt className="field-label">Model profile</dt><dd className="mono" style={{ margin: 0 }}>{existing.model?.profile_id || "\u2014"}</dd></div>
      <div><dt className="field-label">Tools</dt><dd style={{ margin: 0 }}>{(existing.tools || []).length} registered</dd></div>
    </dl>
  );
}

// Where a refusal of the form goes. A save the server refuses with 422 carries one message per field path, kept in the form's `fieldErrors` map under
// those paths ("body.temperature", "body.system_prompt.1"). AG_FIELD_PATHS is every path the overlay draws a field for; AG_fieldError(map, path) is the
// message for one field (its own, else the first under it, so "body.system_prompt.1" is the system prompt's; "body.tools_extra" is not under "body.tools");
// AG_otherFieldErrors(map, paths) is one line ("harness_id: Extra inputs are not permitted") for every message that no field in `paths` owns, so a
// refusal on a field the form does not show is said above the footer and not stored under a key nothing reads. An empty or null message is no message.
const AG_FIELD_PATHS = [
  "body.id", "body.description", "body.model", "body.system_prompt", "body.compaction_prompt", "body.temperature",
  "body.max_tool_turns", "body.max_output_tokens", "body.tools", "body.tts_voice", "body.response_format",
];

function AG_fieldError(fieldErrors, path) {
  var map = fieldErrors || {};
  if (map[path]) return map[path];
  var keys = Object.keys(map);
  for (var i = 0; i < keys.length; i++) {
    if (keys[i].indexOf(path + ".") === 0 && map[keys[i]]) return map[keys[i]];
  }
  return undefined;
}

function AG_otherFieldErrors(fieldErrors, paths) {
  var map = fieldErrors || {};
  return Object.keys(map)
    .filter(function (key) {
      return !!map[key] && !paths.some(function (p) { return key === p || key.indexOf(p + ".") === 0; });
    })
    .map(function (key) {
      return key === "body" ? map[key] : key.replace(/^body\./, "") + ": " + map[key];
    });
}

// The messages of a save the server refused, keyed by the form's field paths ("body.temperature", "body.model.profile_id"), or null when the
// refusal names no field (it is then a toast). The server says a field in two shapes (audit G3-03), and both land under the field:
// - request validation, one entry per field in `fieldErrors` (ApiError's extensions.errors). The create route locates them in the body
//   (["body", "temperature"]); the edit route validates the body itself, so the same refusal is ["temperature"]. A location without the
//   "body" prefix gets it, so the two map to the same field.
// - a pre-write check's {error, field, message} (a tool binding naming a toolset that does not exist, a profile that does not exist), which
//   has no field list: the console's one reader (window.primerApi.readRefusal) gives its field and its words.
function AG_refusalFieldErrors(err) {
  if (!err || err.status !== 422) return null;
  var map = {};
  if (Array.isArray(err.fieldErrors) && err.fieldErrors.length > 0) {
    err.fieldErrors.forEach(function (fe) {
      var loc = fe && Array.isArray(fe.loc) ? fe.loc.slice() : [];
      if (loc[0] !== "body" && loc[0] !== "query" && loc[0] !== "path") loc.unshift("body");
      map[loc.join(".")] = fe && fe.msg;
    });
    return map;
  }
  var refusal = window.primerApi.readRefusal(err);
  if (!refusal.field) return null;
  map["body." + refusal.field] = refusal.sentence || refusal.message;
  return map;
}

// A save the form refuses is brought into view (platform revamp Review Focus 5; review of #779, required fix 3). The overlay's body scrolls (the edit form is
// about 1900px tall and the window shows about 620 of it), and a message sits under its own field, often far from the Save button the operator just pressed: a
// refusal out of sight reads as a button that does nothing. After every refused save the FIRST message of the overlay (the first role="alert" in the dialog) is
// scrolled into view inside the body, and the control it describes takes the focus, so the keyboard carries on from the field to fix. A message under a field is the
// shared form field's alert, which ties itself to its control with aria-describedby, and that is how the control is found; the line above the footer, and a field made
// of several controls (the system prompt, the tools), have no such control and are only scrolled to. It runs from an effect, after the commit that draws the message.
// The message is CENTRED in the body, so the field just above it is in sight with it ("nearest" left the message flush against an edge, clipped by a fraction of a
// pixel, and its field above the top edge when the body had scrolled past it); the focus does not scroll on its own (preventScroll), so the body moves once.
function AG_revealRefusal() {
  const dialog = document.querySelector('[data-testid="agent-form"]');
  const message = dialog ? dialog.querySelector('[role="alert"]') : null;
  if (!message) return;
  const control = message.id ? dialog.querySelector('[aria-describedby~="' + message.id + '"]') : null;
  message.scrollIntoView({ block: "center" });
  if (control && typeof control.focus === "function") control.focus({ preventScroll: true });
}

// The system prompt as tabs over ONE editor (platform revamp design 9): a tab per part ("Part 1", "Part 2"), "+ Part" and "Remove part" beside them,
// and a Full screen toggle that lifts the box over the window. `parts` is the agent's list of prompt strings (the runtime joins them with a blank
// line) and always holds at least one; `onChange(next)` hands the parent the new list. The tab that is shown is a position, kept in range when the
// list shrinks: removing the selected part selects the next one (or the one before, at the end), so the next keystroke never goes to a part that is
// gone. Full screen is a layer of the Escape stack (foundation/escape-stack.js): Escape leaves it first and only the next Escape closes the overlay
// behind it, so the draft is not lost. It is also a FOCUS TRAP (foundation/focus-trap.js): every layer that holds focus above a dialog is one, and without
// it Tab walked from the editor on to the controls the layer covers (23 Tabs reached the covered Cancel, and the Enter that followed closed the overlay and lost
// the draft). Tab and Shift+Tab now cycle through the layer's own controls, and leaving full screen gives the focus back to the toggle. The editor takes the
// focus when full screen opens (the toggle is inside the box, so the trap finds focus already in the layer and leaves it there; the effect below moves it to the
// editor, and `initial` names the editor for a layer that opens with the focus elsewhere). Its z-index is above the layers inside the dialog (the tool
// picker's sticky group headers are 1, the kit's selector menu 3): at 1 a toolset header showed on top of the full-screen editor.
function AG_PromptTabs({ parts, onChange }) {
  const [active, setActive] = React.useState(0);
  const [fullScreen, setFullScreen] = React.useState(false);
  const [focused, setFocused] = React.useState(false);
  const uid = React.useId();
  const boxRef = React.useRef(null);
  const editorRef = React.useRef(null);
  const current = Math.max(0, Math.min(active, parts.length - 1));
  const panelId = uid + "panel";
  const tabId = (i) => uid + "tab" + i;
  window.primerApi.useEscape(() => setFullScreen(false), fullScreen);
  window.primerApi.useFocusTrap(boxRef, fullScreen, { initial: () => editorRef.current });
  React.useEffect(() => {
    const node = editorRef.current;
    if (fullScreen && node && typeof node.focus === "function") node.focus();
  }, [fullScreen]);
  const barButton = (color) => ({
    padding: "4px 10px", border: "none", borderRadius: "var(--r-6)", background: "transparent",
    color, fontSize: 12, whiteSpace: "nowrap", cursor: "pointer", flexShrink: 0,
  });
  const box = fullScreen
    ? { position: "fixed", inset: 0, zIndex: 10, background: "var(--bg-1)", display: "flex", flexDirection: "column" }
    : {
      border: "1px solid " + (focused ? "var(--accent)" : "var(--border)"), borderRadius: "var(--r-8)", overflow: "hidden",
      background: "var(--bg)", display: "flex", flexDirection: "column", boxShadow: focused ? "0 0 0 3px var(--accent-dim)" : "none",
    };
  return (
    <div ref={boxRef} data-testid="agent-system-prompt" data-fullscreen={fullScreen ? "true" : "false"} style={box}>
      <div style={{
        display: "flex", alignItems: "center", gap: 4, padding: "4px 6px", flexShrink: 0,
        background: "var(--bg-2)", borderBottom: "1px solid var(--border)",
      }}>
        <div role="tablist" aria-label="System prompt parts" data-testid="agent-system-prompt-tabs"
          style={{ display: "flex", gap: 2, overflowX: "auto", minWidth: 0 }}>
          {parts.map((_part, i) => (
            <button key={i} type="button" role="tab" id={tabId(i)} aria-selected={i === current} aria-controls={panelId}
              data-testid={`agent-system-prompt-tab-${i}`} onClick={() => setActive(i)}
              style={{
                ...barButton(i === current ? "var(--text)" : "var(--text-3)"),
                background: i === current ? "var(--bg)" : "transparent", fontWeight: i === current ? 600 : 400,
              }}>
              Part {i + 1}
            </button>
          ))}
        </div>
        <button type="button" data-testid="agent-system-prompt-add" style={barButton("var(--accent)")}
          onClick={() => { onChange(parts.concat([""])); setActive(parts.length); }}>+ Part</button>
        <span style={{ flex: 1 }} />
        <button type="button" data-testid="agent-system-prompt-remove" disabled={parts.length === 1}
          title={parts.length === 1 ? "At least one part stays" : "Remove the part that is shown"}
          style={{ ...barButton("var(--text-3)"), opacity: parts.length === 1 ? 0.5 : 1, cursor: parts.length === 1 ? "default" : "pointer" }}
          onClick={() => {
            const next = parts.filter((_p, j) => j !== current);
            onChange(next);
            setActive(Math.min(current, next.length - 1));
          }}>Remove part</button>
        <button type="button" data-testid="agent-system-prompt-fullscreen" aria-pressed={fullScreen}
          style={barButton("var(--text-3)")} onClick={() => setFullScreen(!fullScreen)}>
          {fullScreen ? "Exit full screen" : "Full screen"}
        </button>
      </div>
      <div role="tabpanel" id={panelId} aria-labelledby={tabId(current)}
        style={{ display: "flex", flexDirection: "column", flex: fullScreen ? 1 : undefined, minHeight: 0 }}>
        <textarea ref={editorRef} className="textarea mono" data-testid="agent-system-prompt-editor"
          aria-label={`System prompt part ${current + 1}`} rows={12}
          value={parts[current] == null ? "" : parts[current]}
          onChange={(e) => {
            const next = parts.slice();
            next[current] = e.target.value;
            onChange(next);
          }}
          onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
          style={{
            display: "block", boxSizing: "border-box", width: "100%", flex: fullScreen ? 1 : undefined, minHeight: 0,
            border: "none", borderRadius: 0, boxShadow: "none", background: "var(--bg)",
            fontFamily: "var(--font-mono)", fontSize: 12, resize: fullScreen ? "none" : "vertical",
          }} />
      </div>
    </div>
  );
}

// What a save sends for the system prompt. `parts` is what the form holds, `stored` the list the agent was loaded with. A part the operator did not touch goes
// back exactly as it was stored (a part with spaces around it, or an empty one, is not rewritten by a save that was about something else); a part they typed is
// trimmed, and one left blank is dropped (an add-then-leave-blank part must not send a hole in the array). A stored part excuses ONE part of the form, so a
// second copy the operator typed is theirs. With no stored parts the form's one empty part is dropped, and the prompt is [].
function AG_systemPromptForSave(parts, stored) {
  var untouched = Array.isArray(stored) ? stored.slice() : [];
  var out = [];
  parts.forEach(function (part) {
    var at = untouched.indexOf(part);
    if (at >= 0) {
      untouched.splice(at, 1);
      out.push(part);
      return;
    }
    var text = part.trim();
    if (text) out.push(text);
  });
  return out;
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
  // the form picks one row rather than a provider+model pair. The rows come
  // from the CHOICES route (id, description, kind, context_length), which
  // every signed-in user may read: the full list is admin-only, and a
  // user-role account may create an agent too (audit G3-02). Its own cache
  // key, because the agents list reads the full rows under "agents:model-profiles".
  const profiles = useResource(
    "agents:model-profile-choices",
    (signal) => apiFetch("GET", "/model_profiles/choices?limit=200", null, { signal }),
    { pollMs: null }
  );
  // The Voice fold gates a tts_voice picker on the speech capability
  // and fills it from the voice list. Both were read here while only
  // AgentsPage fetched them, so opening this modal and reaching that
  // picker threw ReferenceError: caps is not defined, and the whole
  // form rendered nothing -- taking the rest of its fields down with it.
  const caps = window.primerApi.useCapabilities();
  const voices = useResource(
    "agents:voices",
    (signal) => apiFetch("GET", "/audio/voices", null, { signal }),
    { pollMs: 0 },
  );

  // Initial values come from the existing agent in edit mode, else
  // blanks. compaction_prompt is stored as an array server-side but the
  // form edits it as one text: ["a", "b"] shows as "a\n\nb", the blank-line
  // join the runtime uses (primer/agent/compaction.py). Saved with that same
  // text, the stored parts go back unchanged; an edited text is one part.
  const _joinPrompt = (p) => Array.isArray(p) ? p.join("\n\n") : (p || "");
  const _initialTools = () => {
    const t = existing?.tools;
    return new Set(Array.isArray(t) ? t : []);
  };
  // Platform wave P1b item 8: system_prompt is ALREADY list[str] server-
  // side (primer/model/agent.py:143, joined with "\n\n" at prompt-render
  // time) - the only gap was this form flattening it to one textarea. A
  // saved agent with N parts loads as N tabs over one editor; a legacy/absent
  // value defaults to one empty part so the editor always has at least one.
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
  // How many saves the form has refused. Every refusal (the checks before a request, and the server's 422) bumps it, and the effect brings the first message into view
  // (AG_revealRefusal): the message is drawn by the commit that follows the bump, so it exists when the effect looks for it.
  const [refusals, setRefusals] = React.useState(0);
  const refused = () => setRefusals((n) => n + 1);
  React.useEffect(() => {
    if (refusals > 0) AG_revealRefusal();
  }, [refusals]);
  // uiv2 Wave 2 (a-11): the two numerics that had NO control anywhere in
  // the app before this wave (grep-confirmed against agents.jsx pre-
  // change). max_tool_turns has a real server default (50, not null) -
  // initializing from it rather than leaving the field blank means a
  // save that never touches this field still round-trips the agent's
  // real value instead of PUT-replacing it back down to the schema
  // default. A stored null is "no limit" (primer/model/agent.py): it
  // starts as an empty field that says so, and an empty field saves
  // null (it used to show and save 50, capping the agent on any edit).
  const [maxToolTurns, setMaxToolTurns] = React.useState(
    existing && existing.max_tool_turns === null ? "" : String(existing?.max_tool_turns ?? 50)
  );
  const [maxOutputTokens, setMaxOutputTokens] = React.useState(
    existing?.max_output_tokens != null ? String(existing.max_output_tokens) : ""
  );
  // PK_Fold's `open` is only its STARTING state, so the form opens a fold for the operator by changing its key: the new fold starts open. The values live
  // here, not in the fold, so a fold that is remounted (or closed) loses and hides nothing. The Advanced fold opens when its response format does not parse or the
  // server refuses it; the Voice fold, when the server refuses the voice.
  const [advancedOpens, setAdvancedOpens] = React.useState(0);
  const [voiceOpens, setVoiceOpens] = React.useState(0);
  // A number input the browser cannot read reports "" (validity.badInput), and "" means "model default" or "no limit": it would save as that. It is an error
  // under the field instead, and nothing is sent until the operator fixes or clears it. Keyed by the field's path, like the server's refusals.
  const [numberErrors, setNumberErrors] = React.useState({});
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
  const profileMissing = !!profileId && !selectedProfile && !profiles.loading && !profiles.error;

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
        const map = AG_refusalFieldErrors(err);
        if (map) {
          setFieldErrors(map);
          // A refusal on a field inside a fold would be said where nobody looks: open the fold it belongs to.
          if (AG_fieldError(map, "body.response_format")) setAdvancedOpens((n) => n + 1);
          if (AG_fieldError(map, "body.tts_voice")) setVoiceOpens((n) => n + 1);
          refused();
        } else if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err.title || (isEdit ? "Save failed" : "Create failed"),
            detail: window.primerApi.readRefusal(err, isEdit ? "Save failed" : "Create failed").message,
            requestId: err.requestId,
          });
        }
      },
    }
  );

  const submit = async () => {
    setFieldErrors({});
    setResponseFormatError(null);
    // An existing agent's id is locked, so an edit passes a blank one and can only be refused for its description (a blank one used to be replaced
    // by a placeholder text and saved).
    const problems = AG_validateNewAgent(isEdit ? "" : id, description);
    if (Object.keys(problems).length) { setFieldErrors(problems); refused(); return; }
    // A number the browser could not read is already an error under its field (see numberErrors): nothing is sent until it is fixed or cleared.
    if (Object.keys(numberErrors).length) { refused(); return; }
    // response_format: parse the textarea once here so a malformed
    // schema is caught client-side (open Advanced + show the error)
    // before the request goes out. Empty text == no structured output.
    let responseFormatValue = null;
    if (responseFormat.trim() !== "") {
      try {
        responseFormatValue = JSON.parse(responseFormat);
      } catch (e) {
        setResponseFormatError(String(e.message || e));
        setAdvancedOpens((n) => n + 1);
        refused();
        return;
      }
    }
    // Agent.tools is the list of scoped tool ids - no separate
    // allowlist field; an empty list means no tools registered.
    const tools = [...selectedScopedIds].sort();
    const body = {
      // On edit the id is locked but still sent (PUT-replace contract).
      ...(isEdit ? { id: existing.id } : (id.trim() ? { id: id.trim() } : {})),
      description: description.trim(),
      model: { profile_id: profileId },
      tools,
      // The parts nobody touched go back as stored; a typed part is trimmed and a blank one dropped (AG_systemPromptForSave).
      system_prompt: AG_systemPromptForSave(systemPromptParts, existing?.system_prompt),
      // The stored parts when the text is the one the form showed (see
      // _joinPrompt above), so a save does not rewrite a multi-part prompt.
      compaction_prompt: Array.isArray(existing?.compaction_prompt) && compactionPrompt === _joinPrompt(existing.compaction_prompt)
        ? existing.compaction_prompt
        : (compactionPrompt ? [compactionPrompt] : []),
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
    // reset a customized value). An empty field is null, no limit. A number
    // that rounds down to under 1 (0, a negative, 0.5) is no cap: it saves the
    // server default, 50, as "0" does (it used to send 0.5 as 0, which the
    // server refuses; audit G3-03).
    const parsedMaxToolTurns = Math.floor(Number(maxToolTurns));
    body.max_tool_turns = maxToolTurns.trim() === "" ? null
      : Number.isFinite(parsedMaxToolTurns) && parsedMaxToolTurns >= 1 ? parsedMaxToolTurns : 50;
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

  // The two handlers of a number input. onChange keeps what was typed AND whether the field can take it; onInput reads only the second. The browser reports ""
  // for text it cannot read, and React sends no change for an input whose value stays "": the first unreadable character typed into an EMPTY field would never
  // reach onChange, but the native input event always reaches onInput. `refuses(text)` (optional) names a number that WAS read but is no value for the field, and it
  // is said with the same message: a limit under 1 (AG_underOne) would otherwise be read as an empty field, which means no limit.
  const numberHandlers = (path, set, message, refuses) => {
    const note = (e) => {
      const bad = !!(e.target.validity && e.target.validity.badInput) || (typeof refuses === "function" && refuses(e.target.value));
      setNumberErrors((m) => {
        if (!!m[path] === bad) return m;
        const next = { ...m };
        if (bad) next[path] = message; else delete next[path];
        return next;
      });
    };
    return { onChange: (e) => { set(e.target.value); note(e); }, onInput: note };
  };

  // The model profile dropdown. A profile the agent names that is no longer in the list stays the selection (a native select would otherwise show its first
  // option and the save would repoint the agent): it is the first option, marked "(missing)" when the list loaded and plain when the load failed.
  const profilesFailed = !!profiles.error;
  const profileGone = !!profileId && !selectedProfile && !profiles.loading;
  // Said through the console's one reader of a failed request, so a refusal that carries only a code (forbidden_role) is said in words.
  const profilesLoadError = profilesFailed ? window.primerApi.readRefusal(profiles.error, "").message : "";
  const profileError = AG_fieldError(fieldErrors, "body.model")
    || (profilesFailed ? "Could not load the model profiles" + (profilesLoadError ? ": " + profilesLoadError : ".") : null)
    || (profileMissing ? "This profile no longer exists. Pick another one." : null);
  const profileHelp = !profiles.loading && !profilesFailed && profileOptions.length === 0
    ? "No model profiles yet. Register an LLM provider and import its models first."
    : "The default; a run can override it";

  // Refusals that no field of the overlay owns are said in one line above the footer. A voice control that is not drawn (text to speech is not set up) owns none.
  const voiceShown = !!(caps.data && caps.data.speech && caps.data.speech.tts_configured);
  const shownPaths = voiceShown ? AG_FIELD_PATHS : AG_FIELD_PATHS.filter((path) => path !== "body.tts_voice");
  const otherErrors = AG_otherFieldErrors(fieldErrors, shownPaths);

  // uiv2 Wave 2 (harness_id): a managed row 409s on any PUT
  // (routers/agents.py) - AG_ConfigTab used to just hide its Edit
  // button and leave the raw-JSON view up; now that direct-edit IS the
  // landing view, a locked summary replaces the form outright rather
  // than rendering inputs Save can never persist.
  if (isManaged) {
    return (
      <PK_FormOverlay
        title="Managed agent"
        onClose={onClose}
        testid="agent-form"
        footer={{ right: <Btn kind="ghost" data-testid="agent-form-close" onClick={onClose}>Close</Btn> }}
      >
        <Banner kind="info" title={`Managed by harness ${existing.harness_id}`}
          detail="Direct edits are blocked. Update the harness's sync or uninstall flow instead." />
        <AG_ManagedSummary existing={existing} />
      </PK_FormOverlay>
    );
  }

  return (
    <PK_FormOverlay
      title={isEdit ? "Edit agent" : "New agent"}
      onClose={onClose}
      testid="agent-form"
      footer={{
        left: (
          <>
            <Btn kind="ghost" data-testid="agent-form-cancel" onClick={onClose}>Cancel</Btn>
            {isEdit && onDelete && (
              <Btn kind="ghost" data-testid="agent-form-delete" onClick={onDelete} style={{ color: "var(--red)" }}>Delete</Btn>
            )}
          </>
        ),
        right: (
          <>
            {isEdit && onChat && (
              <Btn kind="ghost" icon="send" data-testid="agent-form-chat" onClick={onChat} busy={chatLoading}>
                {chatLoading ? "Opening chat…" : "Chat"}
              </Btn>
            )}
            <Btn
              kind="primary"
              icon={isEdit ? "check" : "plus"}
              data-testid="agent-form-submit"
              onClick={submit}
              disabled={!profileId}
              busy={create.loading}
            >
              {create.loading ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save" : "Create")}
            </Btn>
          </>
        ),
      }}
    >
      {isEdit && <AG_StatusPanel id={existing.id} status={status} />}
      <PK_Grid cols={2}>
        <PK_Field label="Id" help={isEdit ? "The id cannot change after the agent is created." : undefined} error={AG_fieldError(fieldErrors, "body.id")}>
          <input
            id="na-id"
            data-testid="agent-id"
            className="input"
            value={id}
            onChange={(e) => { setId(e.target.value); setFieldErrors((m) => ({ ...m, "body.id": undefined })); }}
            placeholder="Optional, e.g. refund-triage"
            disabled={isEdit}
          />
        </PK_Field>
        <PK_Field label="Model profile" required help={profileHelp} error={profileError}>
          <select
            id="na-model-profile"
            data-testid="agent-model-profile"
            className="select"
            aria-required="true"
            value={profileId}
            onChange={(e) => setProfileId(e.target.value)}
          >
            {profileGone && <option value={profileId}>{profilesFailed ? profileId : profileId + " (missing)"}</option>}
            {profiles.loading && profileOptions.length === 0 && <option value="">Loading model profiles…</option>}
            {!profiles.loading && profileOptions.length === 0 && !profileId && <option value="">No model profiles yet</option>}
            {profileOptions.map((pr) => <option key={pr.id} value={pr.id}>{pr.id}</option>)}
          </select>
        </PK_Field>
        <PK_Field label="Description" span="full" required error={AG_fieldError(fieldErrors, "body.description")}>
          <input
            id="na-description"
            data-testid="agent-description"
            className="input"
            value={description}
            onChange={(e) => { setDescription(e.target.value); setFieldErrors((m) => ({ ...m, "body.description": undefined })); }}
            placeholder="What this agent is for"
            aria-required="true"
          />
        </PK_Field>
      </PK_Grid>
      <PK_Field label="System prompt" help="The parts are joined with a blank line when the prompt is built."
        error={AG_fieldError(fieldErrors, "body.system_prompt")}>
        <AG_PromptTabs parts={systemPromptParts} onChange={setSystemPromptParts} />
      </PK_Field>
      {/* uiv2 Wave 2 (b-2): a separately-labeled field, not a second
          unlabeled textarea under the System prompt heading - the
          schema already treats these as distinct concepts
          (compaction_prompt is its own field, system_prompt an
          unbounded ordered list, confirmed live pre-wave). */}
      <PK_Field label="Compaction prompt" help="Leave empty to use the default compaction prompt."
        error={AG_fieldError(fieldErrors, "body.compaction_prompt")}>
        <textarea
          id="na-compaction-prompt"
          data-testid="agent-compaction-prompt"
          className="textarea mono"
          value={compactionPrompt}
          onChange={(e) => setCompactionPrompt(e.target.value)}
          rows={7}
          placeholder="How to summarise older turns when the conversation outgrows the context window"
          style={{ resize: "vertical", fontFamily: "var(--font-mono)", fontSize: 12 }}
        />
      </PK_Field>
      {/* uiv2 Wave 2 (a-11): the numerics sit in one row under the
          prompts, not tucked behind Advanced - they gate the same model
          call the profile does. */}
      <PK_Grid cols={3}>
        <PK_Field label="Temperature" error={numberErrors["body.temperature"] || AG_fieldError(fieldErrors, "body.temperature")}>
          <input
            id="na-temperature"
            data-testid="agent-temperature"
            className="input"
            type="number"
            step="0.05"
            min="0"
            placeholder="Model default"
            value={temperature}
            {...numberHandlers("body.temperature", setTemperature, "Enter a number, or leave it empty for the model default.")}
          />
        </PK_Field>
        <PK_Field label="Max tool turns" error={numberErrors["body.max_tool_turns"] || AG_fieldError(fieldErrors, "body.max_tool_turns")}>
          <input
            id="na-max-tool-turns"
            data-testid="agent-max-tool-turns"
            className="input"
            type="number"
            step="1"
            min="1"
            placeholder="No limit"
            value={maxToolTurns}
            {...numberHandlers("body.max_tool_turns", setMaxToolTurns, "Enter a whole number, or leave it empty for no limit.")}
          />
        </PK_Field>
        <PK_Field label="Max output tokens" help="Empty means no limit"
          error={numberErrors["body.max_output_tokens"] || AG_fieldError(fieldErrors, "body.max_output_tokens")}>
          <input
            id="na-max-output-tokens"
            data-testid="agent-max-output-tokens"
            className="input"
            type="number"
            step="1"
            min="1"
            placeholder="No limit"
            value={maxOutputTokens}
            {...numberHandlers("body.max_output_tokens", setMaxOutputTokens, "Enter a whole number, or leave it empty for no limit.", AG_underOne)}
          />
        </PK_Field>
      </PK_Grid>
      <PK_Field label="Tools" help="Pick single tools, never whole toolsets" error={AG_fieldError(fieldErrors, "body.tools")}>
        <window.ToolPicker selected={selectedScopedIds} onChange={setSelectedScopedIds} pageSize={6} />
      </PK_Field>
      {voiceShown && (
        <PK_Fold key={"voice-" + voiceOpens} open={voiceOpens > 0} label="Voice" testid="agent-voice-fold">
          <PK_Field label="Text-to-speech voice" error={AG_fieldError(fieldErrors, "body.tts_voice")}>
            <select
              id="na-tts-voice"
              className="select"
              data-testid="agent-tts-voice"
              value={ttsVoice || ""}
              onChange={(e) => setTtsVoice(e.target.value || null)}
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
          </PK_Field>
        </PK_Fold>
      )}
      <PK_Fold key={"adv-" + advancedOpens} open={advancedOpens > 0} label="Advanced" testid="agent-advanced-fold">
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <AG_Toggle
            checked={compactionToolAccess}
            onChange={setCompactionToolAccess}
            testid="na-compaction-tool-access"
            label="Tool access during compaction"
            help="Let the compaction prompt call this agent's tools while it summarises, for example to save the summary to workspace files. The calls run in a bounded loop (Max tool turns, or 8 rounds when it has no limit) and do not enter the conversation history. Leave off for text-only compaction."
          />
          <AG_Toggle
            checked={allowExternalTools}
            onChange={setAllowExternalTools}
            testid="na-allow-external-tools"
            label="Allow external tools"
            help="Let API callers attach their own tool definitions when they invoke this agent. When the model calls one, the turn pauses until the caller answers through the invocation API. Leave off to refuse such calls."
          />
          <PK_Field label="Response format" help="A JSON Schema the answer must match. Leave empty to run the agent unconstrained."
            error={responseFormatError ? "JSON parse: " + responseFormatError : AG_fieldError(fieldErrors, "body.response_format")}>
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
              style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}
              data-testid="agent-response-format"
            />
          </PK_Field>
        </div>
      </PK_Fold>
      {isEdit && <AG_SessionsPanel agentId={existing.id} />}
      {isEdit && (
        <AG_JsonDisclosure agent={existing} open={showJson} onToggle={() => setShowJson((v) => !v)} />
      )}
      {otherErrors.length > 0 && (
        <div role="alert" data-testid="agent-form-error" className="field-help" style={{ color: "var(--red)" }}>
          <Icon name="x-circle" size={11} style={{ verticalAlign: -1, marginRight: 3 }} />
          {otherErrors.join("; ")}
        </div>
      )}
    </PK_FormOverlay>
  );
}

// ============================================================================
// Agent detail page
// ============================================================================

// The default workspace: the ONE row GET /workspaces flags is_default (a response-only flag; the server refuses to delete that row). The list is paged, and an
// install with more workspaces than one page may hold the flagged row beyond the first, so the pages are read (limit and offset, as the pager does) until the
// flagged row is found or the list ends: a page shorter than the limit, or the offset reaching `total` (a server that cannot count sends none, and then only a
// short page ends it). The first request is `?limit=200` with no offset. Resolves to the row, or null when no row on any page is flagged: there is no fallback.
const AG_WORKSPACE_PAGE = 200;

async function AG_findDefaultWorkspace(apiFetch) {
  let offset = 0;
  for (;;) {
    const list = await apiFetch("GET", "/workspaces?limit=" + AG_WORKSPACE_PAGE + (offset ? "&offset=" + offset : ""));
    const rows = (list && list.items) || [];
    const flagged = rows.find((w) => w.is_default === true);
    if (flagged) return flagged;
    offset += rows.length;
    const total = list && list.total;
    if (rows.length < AG_WORKSPACE_PAGE || (typeof total === "number" && offset >= total)) return null;
  }
}

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
  // the same way the old chat detail page did. A session lives in a
  // workspace and the only create route is POST /workspaces/{wid}/sessions
  // (there is no POST /sessions), so it is made in the DEFAULT workspace:
  // the one row GET /workspaces flags is_default (AG_findDefaultWorkspace).
  // With no row flagged it says so and creates nothing: the first workspace
  // is not the default, and a chat in some other workspace is not what the
  // button promises. The router is told the session's workspace, so the
  // studio opens it there.
  const startChatMut = useMutation(
    async () => {
      const target = await AG_findDefaultWorkspace(apiFetch);
      if (!target) {
        const none = new Error("A chat runs in the default workspace, and none of the workspaces the server lists is marked as the default. Nothing was created.");
        none.title = "No default workspace found";
        none.detail = "A chat runs in the default workspace, and none of the workspaces the server lists is marked as the default. Nothing was created.";
        throw none;
      }
      const row = await apiFetch("POST", `/workspaces/${encodeURIComponent(target.id)}/sessions`, {
        binding: { kind: "agent", agent_id: id },
        auto_start: false,
      });
      return { row, workspaceId: target.id };
    },
    {
      invalidates: ["sessions:list"],
      onSuccess: ({ row, workspaceId }) => navigate("/sessions/" + (row.session_id || row.id), { workspace_id: workspaceId }),
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
                busy={delMut.loading}
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
  // Things that may work but probably do not (01a10c6b): shown under the verdict, never part of it.
  const warnings = status.data?.warnings || [];
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
          <div className="muted text-sm" data-testid="agent-status-note" title={`GET /v1/agents/${id}/status, polled every 30s`}>
            Checked automatically while this window is open
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
          {warnings.length > 0 && (
            <div className="mt-2" data-testid="ag-status-warnings">
              {warnings.map((w, i) => (
                <div key={i} className="ref-row" style={{ borderColor: "var(--amber-dim)" }}>
                  <Icon name="alert" size={12} className="ico" style={{ color: "var(--amber)" }} />
                  <span className="label" style={{ color: "var(--amber)" }}>warning</span>
                  <span className="val">{typeof w === "string" ? w : (w.detail || w.message || JSON.stringify(w))}</span>
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
