// ============================================================================
// Agent memory (agent memory T14): the Memory section of the agent editor and the Memory panel
// ============================================================================
//
// AG_MemorySettings: the owner's switches for what an agent learns (GET / PUT /agents/<id>/memory/settings). AG_MemoryPanel: what it has learned, by
// folder (GET /agents/<id>/memory/entries?status=), the candidates inbox, and the activity feed (the feed is the owner's). Everything here is disabled
// or hidden for anyone below owner: the server refuses it too (403 memory_owner_only), this only says so before the click. Names start with AG_ / AGM_ as in agents.jsx.

// The fields an owner sets; the PUT takes these and nothing else (primer/memory/settings.py EDITABLE).
var AGM_EDITABLE = [
  "enabled", "promotion", "teachers", "idle_minutes", "auto_inject", "inject_top_k", "inject_min_score", "max_index_entries", "max_active",
  "archive_after_days", "daily_reflections", "search", "capture_topics", "do_not_capture", "reflection_prompt",
];

// ---- the seam: the agent.memory verb opens the Memory fold of the agent editor that is open ----------------------------------------------------
var AGM_subs = new Set();
window.AG_onMemory = function (cb) { AGM_subs.add(cb); return function () { AGM_subs.delete(cb); }; };
window.AG_requestMemory = function () { AGM_subs.forEach(function (cb) { cb(); }); };
window.AG_memoryLive = function () { return AGM_subs.size > 0; };
// A counter that goes up each time the verb runs while the editor is mounted; the fold opens when it is above zero.
function AG_useMemoryOpens() {
  const st = React.useState(0);
  React.useEffect(() => window.AG_onMemory(() => st[1]((n) => n + 1)), []);
  return st[0];
}
window.AG_useMemoryOpens = AG_useMemoryOpens;

function AGM_message(err, fallback) {
  return (err && (err.detail || err.title || err.message)) || fallback;
}

function AGM_toast(pushToast, toast) {
  if (typeof pushToast === "function") pushToast(toast);
}

// The viewer's level on the agent ("read" | "full" | "owner"), from the same sharing route the Share dialog reads.
function AGM_useLevel(agentId) {
  const { useResource, apiFetch } = window.primerApi;
  const access = useResource(
    "agent-memory:access:" + agentId,
    (signal) => apiFetch("GET", "/agents/" + encodeURIComponent(agentId) + "/access", null, { signal }),
    { pollMs: null, deps: [agentId] }
  );
  return access.data ? access.data.your_level : null;
}

function AGM_Switch({ checked, onChange, disabled, testid, label, help }) {
  return (
    <div>
      <label style={{ display: "flex", alignItems: "center", gap: 10, opacity: disabled ? 0.5 : 1 }}>
        <button
          type="button" role="switch" aria-checked={!!checked} disabled={disabled} data-testid={testid}
          onClick={() => !disabled && onChange(!checked)}
          style={{
            flex: "0 0 auto", width: 34, height: 20, borderRadius: 999, border: "1px solid var(--border)", padding: 0,
            background: checked ? "var(--accent)" : "var(--bg-2)", position: "relative", cursor: disabled ? "default" : "pointer",
          }}
        >
          <span style={{ position: "absolute", top: 2, left: checked ? 16 : 2, width: 14, height: 14, borderRadius: 999, background: "var(--text)" }} />
        </button>
        <span>{label}</span>
      </label>
      {help && <div className="field-help">{help}</div>}
    </div>
  );
}

// The labelled row of the console (shared/form-field.jsx): the label names the control.
function AGM_Field({ label, help, children }) {
  return <FormField label={label} help={help}>{children}</FormField>;
}

function AGM_draftOf(row) {
  const draft = {};
  AGM_EDITABLE.forEach((k) => { draft[k] = row[k] === undefined ? null : row[k]; });
  return draft;
}

// A number field: the text as typed is kept (so "0." survives), the number is what is saved; empty is null where the field allows it.
function AGM_num(text, allowNull) {
  if (text === "" || text == null) return allowNull ? null : 0;
  const n = Number(text);
  return Number.isFinite(n) ? n : text;
}

// The scores of the recent lookups, newest first: the activity feed's memory.lookup events.
function AGM_lookupScores(activity) {
  const out = [];
  ((activity && activity.items) || []).forEach((e) => {
    if (e.type === "memory.lookup" && e.payload && Array.isArray(e.payload.scores)) e.payload.scores.forEach((s) => out.push(s));
  });
  return out.slice(0, 12);
}

function AG_MemorySettings({ agentId, pushToast }) {
  const { useResource, apiFetch } = window.primerApi;
  const level = AGM_useLevel(agentId);
  const owner = level === "owner";
  const settings = useResource(
    "agent-memory:settings:" + agentId,
    (signal) => apiFetch("GET", "/agents/" + encodeURIComponent(agentId) + "/memory/settings", null, { signal }),
    { pollMs: null, deps: [agentId] }
  );
  // The activity feed carries the lookup scores; it is the owner's, so a viewer below owner does not ask for it.
  const activity = useResource(
    "agent-memory:activity:" + agentId,
    (signal) => owner
      ? apiFetch("GET", "/agents/" + encodeURIComponent(agentId) + "/memory/activity?limit=100", null, { signal })
      : Promise.resolve({ items: [] }),
    { pollMs: null, deps: [agentId, owner] }
  );
  const [draft, setDraft] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);

  if (settings.loading && !settings.data) {
    return <div className="muted text-sm" data-testid="memory-settings-loading">Loading…</div>;
  }
  if (settings.error && !settings.data) {
    return <div role="alert" className="field-help" style={{ color: "var(--red)" }} data-testid="memory-settings-error">{AGM_message(settings.error, "Couldn't load the memory settings")}</div>;
  }
  if (!settings.data) return null;

  const row = settings.data;
  const form = draft || AGM_draftOf(row);
  const set = (patch) => setDraft(Object.assign({}, form, patch));
  const locked = !owner;
  const scores = AGM_lookupScores(activity.data);

  const save = async () => {
    if (locked || busy) return;
    setBusy(true);
    setError(null);
    try {
      const saved = await apiFetch("PUT", "/agents/" + encodeURIComponent(agentId) + "/memory/settings", form);
      setDraft(null);
      if (typeof settings.refetch === "function") settings.refetch();
      AGM_toast(pushToast, { kind: "success", title: "Memory settings saved", detail: saved && saved.enabled ? "Memory is on" : "Memory is off" });
    } catch (err) {
      setError(AGM_message(err, "Couldn't save the memory settings"));
      AGM_toast(pushToast, { kind: "error", title: err.title || "Couldn't save the memory settings", detail: err.detail || err.message, requestId: err.requestId });
    } finally {
      setBusy(false);
    }
  };

  const ownPrompt = form.reflection_prompt != null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }} data-testid="memory-settings">
      {locked && <div className="field-help" data-testid="memory-readonly-note">Only an owner of this agent changes its memory settings.</div>}
      <AGM_Switch
        checked={form.enabled} disabled={locked} testid="memory-enabled" label="Learn from this agent's sessions"
        onChange={(v) => set({ enabled: v })}
        help="When on, finished sessions are reviewed in the background and what they teach is kept as notes for this agent. Off keeps what it already learned."
      />
      <AGM_Field label="Promotion" help="Auto: a note proven in a trial run is used at once. Approve: every note waits for an owner.">
        <select className="select" data-testid="memory-promotion" disabled={locked} value={form.promotion} onChange={(e) => set({ promotion: e.target.value })}>
          <option value="auto">Auto</option>
          <option value="approve">Approve first</option>
        </select>
      </AGM_Field>
      <AGM_Field label="Who teaches" help="Owners only: a note learned in someone else's session waits for approval. Everyone: any session teaches.">
        <select className="select" data-testid="memory-teachers" disabled={locked} value={form.teachers} onChange={(e) => set({ teachers: e.target.value })}>
          <option value="owner">Owners</option>
          <option value="everyone">Everyone</option>
        </select>
      </AGM_Field>
      <AGM_Field label="Idle minutes" help="How long a session is quiet before it is reviewed.">
        <input
          className="input" type="number" min="1" data-testid="memory-idle-minutes" disabled={locked} value={form.idle_minutes}
          onChange={(e) => set({ idle_minutes: AGM_num(e.target.value, false) })}
        />
      </AGM_Field>
      <AGM_Switch
        checked={form.auto_inject} disabled={locked} testid="memory-auto-inject" label="Put the best notes in front of the agent"
        onChange={(v) => set({ auto_inject: v })}
        help="Before each turn the closest notes to the user's message are added to the prompt. Off: the agent looks notes up itself."
      />
      {form.auto_inject && (
        <>
          <AGM_Field label="Notes per turn" help="At most 3.">
            <input
              className="input" type="number" min="1" max="3" data-testid="memory-top-k" disabled={locked} value={form.inject_top_k}
              onChange={(e) => set({ inject_top_k: AGM_num(e.target.value, false) })}
            />
          </AGM_Field>
          <AGM_Field label="Minimum score" help="Empty: no threshold. A note scoring lower is not added. Scores exist for semantic search only.">
            <input
              className="input" type="number" step="0.05" data-testid="memory-min-score" disabled={locked}
              value={form.inject_min_score == null ? "" : form.inject_min_score}
              onChange={(e) => set({ inject_min_score: AGM_num(e.target.value, true) })}
            />
            {owner && (
              <div className="field-help" data-testid="memory-lookup-scores">
                {scores.length ? "Recent lookup scores: " + scores.map((s) => Number(s).toFixed(2)).join(", ") : "No lookups yet."}
              </div>
            )}
          </AGM_Field>
        </>
      )}
      <AGM_Field label="Topics to capture" help="One per line. Empty: the agent picks.">
        <textarea
          className="textarea" rows={3} data-testid="memory-capture-topics" disabled={locked} value={(form.capture_topics || []).join("\n")}
          onChange={(e) => set({ capture_topics: e.target.value.split("\n").map((t) => t.trim()).filter(Boolean) })}
        />
        {row.topics_suggestion && row.topics_suggestion.length > 0 && (
          <div className="field-help" data-testid="memory-topics-suggestion">
            Suggested from this agent's prompt: {row.topics_suggestion.join(", ")}{" "}
            <Btn size="sm" kind="ghost" data-testid="memory-topics-use" disabled={locked} onClick={() => set({ capture_topics: row.topics_suggestion })}>Use these</Btn>
          </div>
        )}
      </AGM_Field>
      <AGM_Field label="Never capture" help="One per line.">
        <textarea
          className="textarea" rows={2} data-testid="memory-do-not-capture" disabled={locked} value={(form.do_not_capture || []).join("\n")}
          onChange={(e) => set({ do_not_capture: e.target.value.split("\n").map((t) => t.trim()).filter(Boolean) })}
        />
      </AGM_Field>
      <AGM_Field label="Reflection prompt" help={ownPrompt ? "Your prompt replaces the built-in one. The answer is still checked against the same schema." : "The built-in prompt. Use your own to change what is captured."}>
        {ownPrompt ? (
          <>
            <textarea
              className="textarea" rows={10} data-testid="memory-prompt-editor" disabled={locked} value={form.reflection_prompt}
              style={{ fontFamily: "var(--font-mono)", fontSize: 12 }} onChange={(e) => set({ reflection_prompt: e.target.value })}
            />
            <div><Btn size="sm" kind="ghost" data-testid="memory-prompt-reset" disabled={locked} onClick={() => set({ reflection_prompt: null })}>Reset to built-in</Btn></div>
          </>
        ) : (
          <>
            <textarea
              className="textarea" rows={10} readOnly data-testid="memory-prompt-builtin" value={row.default_reflection_prompt || ""}
              style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}
            />
            <div><Btn size="sm" kind="ghost" data-testid="memory-prompt-own" disabled={locked} onClick={() => set({ reflection_prompt: row.default_reflection_prompt || "" })}>Use my own prompt</Btn></div>
          </>
        )}
      </AGM_Field>
      {error && <div role="alert" className="field-help" style={{ color: "var(--red)" }} data-testid="memory-save-error">{error}</div>}
      <div>
        <Btn kind="primary" data-testid="memory-save" busy={busy} disabled={locked} onClick={save}>Save memory settings</Btn>
      </div>
    </div>
  );
}

// ============================================================================
// The panel
// ============================================================================

var AGM_TABS = [["active", "Active"], ["candidates", "Candidates"], ["archived", "Archived"], ["activity", "Activity"]];
var AGM_FOLDERS = ["active", "candidates", "archived"];

function AGM_Items({ title, items }) {
  if (!items || !items.length) return null;
  return (
    <div>
      <div className="muted text-sm" style={{ fontWeight: 600 }}>{title}</div>
      <ul style={{ margin: "2px 0 6px 18px" }}>{items.map((i) => <li key={i.id}>{i.text}</li>)}</ul>
    </div>
  );
}

function AGM_Entry({ entry, owner, agentId, act, openSession }) {
  const { apiFetch } = window.primerApi;
  const meta = entry.meta || {};
  const base = "/agents/" + encodeURIComponent(agentId) + "/memory/entries/" + encodeURIComponent(entry.name);
  const [editing, setEditing] = React.useState(false);
  const [description, setDescription] = React.useState(entry.description);
  const [texts, setTexts] = React.useState({});
  const sections = [["steps", entry.body.steps], ["pitfalls", entry.body.pitfalls]];
  const save = () => {
    const body = [];
    sections.forEach(([section, items]) => items.forEach((i) => {
      if (texts[i.id] !== undefined && texts[i.id] !== i.text) body.push({ action: "change", section, item_id: i.id, text: texts[i.id] });
    }));
    const patch = {};
    if (description !== entry.description) patch.description = description;
    if (body.length) patch.body = body;
    if (!Object.keys(patch).length) { setEditing(false); return; }
    act("Entry saved", () => apiFetch("PATCH", base, patch)).then((ok) => { if (ok) setEditing(false); });
  };
  return (
    <div className="panel" data-testid={"memory-entry:" + entry.name} style={{ padding: 10, display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <strong>{entry.name}</strong>
        {meta.pinned && <span className="muted text-sm" data-testid={"memory-pinned:" + entry.name}>pinned</span>}
        {meta.needs_approval && <span className="muted text-sm">needs approval</span>}
      </div>
      {editing ? (
        <input className="input" data-testid={"memory-edit-description:" + entry.name} value={description} onChange={(e) => setDescription(e.target.value)} />
      ) : (
        <div>{entry.description}</div>
      )}
      {sections.map(([section, items]) => editing ? (
        <div key={section}>
          <div className="muted text-sm" style={{ fontWeight: 600 }}>{section === "steps" ? "Steps" : "Pitfalls"}</div>
          {items.map((i) => (
            <input
              key={i.id} className="input" style={{ marginBottom: 4 }} data-testid={"memory-edit-item:" + entry.name + ":" + i.id}
              value={texts[i.id] === undefined ? i.text : texts[i.id]} onChange={(e) => setTexts(Object.assign({}, texts, { [i.id]: e.target.value }))}
            />
          ))}
        </div>
      ) : <AGM_Items key={section} title={section === "steps" ? "Steps" : "Pitfalls"} items={items} />)}
      <div className="muted text-sm" data-testid={"memory-counts:" + entry.name}>{"helped " + (meta.helpful || 0) + " · hurt " + (meta.harmful || 0)}</div>
      {(meta.sources || []).length > 0 && (
        <div className="muted text-sm" data-testid={"memory-sources:" + entry.name}>
          Learned in{" "}
          {meta.sources.map((s, n) => (
            <button key={n} type="button" className="link" style={{ marginRight: 8 }} data-testid={"memory-source:" + entry.name + ":" + n} onClick={() => openSession(s.session_id)}>
              {"session " + s.session_id + " (turns " + s.from_turn + " to " + s.to_turn + ")"}
            </button>
          ))}
        </div>
      )}
      {owner && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {entry.status === "candidates" && (
            <Btn size="sm" kind="primary" data-testid={"memory-approve:" + entry.name} onClick={() => act("Entry approved", () => apiFetch("POST", base + "/approve"))}>Approve</Btn>
          )}
          {entry.status !== "archived" && (
            <Btn size="sm" data-testid={"memory-archive:" + entry.name} onClick={() => act("Entry archived", () => apiFetch("POST", base + "/archive"))}>Archive</Btn>
          )}
          {entry.status === "active" && (
            <Btn size="sm" kind="ghost" data-testid={"memory-pin:" + entry.name} onClick={() => act(meta.pinned ? "Entry unpinned" : "Entry pinned", () => apiFetch("PATCH", base, { pinned: !meta.pinned }))}>
              {meta.pinned ? "Unpin" : "Pin"}
            </Btn>
          )}
          {editing ? (
            <>
              <Btn size="sm" kind="primary" data-testid={"memory-edit-save:" + entry.name} onClick={save}>Save</Btn>
              <Btn size="sm" kind="ghost" data-testid={"memory-edit-cancel:" + entry.name} onClick={() => setEditing(false)}>Cancel</Btn>
            </>
          ) : (
            <Btn size="sm" kind="ghost" data-testid={"memory-edit:" + entry.name} onClick={() => setEditing(true)}>Edit</Btn>
          )}
        </div>
      )}
    </div>
  );
}

function AGM_Activity({ activity }) {
  if (activity.loading && !activity.data) return <div className="muted text-sm" data-testid="memory-loading">Loading…</div>;
  if (activity.error && !activity.data) {
    return <div role="alert" className="field-help" style={{ color: "var(--red)" }} data-testid="memory-error">{AGM_message(activity.error, "Couldn't load the activity")}</div>;
  }
  const items = (activity.data && activity.data.items) || [];
  if (!items.length) return <div className="muted text-sm" data-testid="memory-empty">No memory activity yet.</div>;
  return (
    <ul style={{ margin: 0, paddingLeft: 18 }} data-testid="memory-activity">
      {items.map((e) => (
        <li key={e.id} data-testid={"memory-activity-item:" + e.id}>
          <span>{e.type.replace("memory.", "")}</span>{" "}
          <span className="muted text-sm">{e.actor + " · " + relativeTime(e.at)}</span>
          {e.payload && e.payload.name ? <span className="muted text-sm">{" · " + e.payload.name}</span> : null}
        </li>
      ))}
    </ul>
  );
}

function AG_MemoryPanel({ agentId, pushToast }) {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const level = AGM_useLevel(agentId);
  const owner = level === "owner";
  const [tab, setTab] = React.useState("active");
  const entries = {};
  AGM_FOLDERS.forEach((folder) => {
    entries[folder] = useResource(
      "agent-memory:entries:" + agentId + ":" + folder,
      (signal) => apiFetch("GET", "/agents/" + encodeURIComponent(agentId) + "/memory/entries?status=" + folder, null, { signal }),
      { pollMs: null, deps: [agentId] }
    );
  });
  const activity = useResource(
    "agent-memory:activity:" + agentId,
    (signal) => owner
      ? apiFetch("GET", "/agents/" + encodeURIComponent(agentId) + "/memory/activity?limit=100", null, { signal })
      : Promise.resolve({ items: [] }),
    { pollMs: null, deps: [agentId, owner] }
  );

  // Runs one action: the toast says what happened, the lists reload. Resolves true when it worked.
  const act = async (title, fn) => {
    try {
      await fn();
      AGM_toast(pushToast, { kind: "success", title: title, detail: agentId });
      AGM_FOLDERS.forEach((f) => { if (typeof entries[f].refetch === "function") entries[f].refetch(); });
      if (typeof activity.refetch === "function") activity.refetch();
      return true;
    } catch (err) {
      AGM_toast(pushToast, { kind: "error", title: err.title || "Couldn't change the entry", detail: err.detail || err.message, requestId: err.requestId });
      return false;
    }
  };
  // A source opens its session only when the viewer can read it.
  const openSession = async (sessionId) => {
    try {
      await apiFetch("GET", "/sessions/" + encodeURIComponent(sessionId));
    } catch (_err) {
      AGM_toast(pushToast, { kind: "info", title: "That session is not available to you", detail: sessionId });
      return;
    }
    navigate("/sessions/" + encodeURIComponent(sessionId));
  };

  const shown = tab === "activity" ? null : entries[tab];
  let body;
  if (tab === "activity") {
    body = owner ? <AGM_Activity activity={activity} /> : <div className="muted text-sm" data-testid="memory-empty">The activity feed is for owners.</div>;
  } else if (shown.loading && !shown.data) {
    body = <div className="muted text-sm" data-testid="memory-loading">Loading…</div>;
  } else if (shown.error && !shown.data) {
    body = <div role="alert" className="field-help" style={{ color: "var(--red)" }} data-testid="memory-error">{AGM_message(shown.error, "Couldn't load the entries")}</div>;
  } else {
    const items = (shown.data && shown.data.items) || [];
    body = items.length === 0
      ? <div className="muted text-sm" data-testid="memory-empty">{"No " + tab + " entries."}</div>
      : <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>{items.map((e) => <AGM_Entry key={e.name} entry={e} owner={owner} agentId={agentId} act={act} openSession={openSession} />)}</div>;
  }
  const candidates = entries.candidates.data && entries.candidates.data.items ? entries.candidates.data.items.length : 0;
  return (
    <div className="panel" data-testid="agent-memory-panel" style={{ padding: 10, display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center" }} role="tablist">
        {AGM_TABS.filter(([key]) => key !== "activity" || owner).map(([key, label]) => (
          <Btn key={key} size="sm" kind={tab === key ? "primary" : "ghost"} data-testid={"memory-tab-" + key} aria-pressed={tab === key} onClick={() => setTab(key)}>
            {key === "candidates" && candidates ? label + " (" + candidates + ")" : label}
          </Btn>
        ))}
      </div>
      {body}
      <div>
        <Btn size="sm" kind="ghost" data-testid="memory-open-settings" data-verb="agent.memory" onClick={() => window.AG_requestMemory()}>Memory settings</Btn>
      </div>
    </div>
  );
}

window.AG_MemorySettings = AG_MemorySettings;
window.AG_MemoryPanel = AG_MemoryPanel;
