/* global React */
// Native agent entity documents: the entity-form pattern (spec section
// 5). Dirty tracking + explicit save, 422s map to inline field errors,
// managed rows render read-only. Overrides the interim legacy iframe
// registration (loads after s2-legacy.jsx).
// Field set matches main's Agent model post model-profiles cutover:
// the agent references ONE model.profile_id; the profile carries the
// provider, wire model name, context length, and reasoning tunables.

function S2_Field({ label, hint, error, children }) {
  return (
    <div style={{ display: "grid", gap: 5 }}>
      <label style={{ fontSize: "var(--fs-11)", color: "var(--text-3)",
        fontWeight: 600, textTransform: "uppercase",
        letterSpacing: ".03em" }}>{label}</label>
      {children}
      {hint && <span style={{ fontSize: "var(--fs-11)",
        color: "var(--text-4)" }}>{hint}</span>}
      {error && <span data-testid="s2-field-error"
        style={{ fontSize: "var(--fs-11)", color: "var(--red)" }}>{error}</span>}
    </div>
  );
}
window.S2_Field = S2_Field;

const S2_INPUT_STYLE = {
  background: "var(--bg-1)", border: "1px solid var(--border)",
  borderRadius: "var(--r-6)", padding: "7px 10px",
  color: "var(--text)", fontSize: "var(--fs-12)", width: "100%",
};

function S2_AgentDoc({ refId, docApi }) {
  const { useResource, apiFetch } = window.primerApi;
  const isNew = refId === "__new__";
  const res = useResource(
    isNew ? "studio2:agent:new" : "studio2:agent:" + refId,
    (signal) => isNew
      ? Promise.resolve(null)
      : apiFetch("GET", "/agents/" + encodeURIComponent(refId), null, { signal }),
    { pollMs: 0 },
  );
  const profiles = useResource(
    "studio2:model-profiles",
    (signal) => apiFetch("GET", "/model_profiles?limit=200", null, { signal }),
    { pollMs: 0 },
  );
  const [draft, setDraft] = React.useState(null);
  const [fieldErrors, setFieldErrors] = React.useState({});
  const [saving, setSaving] = React.useState(false);
  React.useEffect(() => { setDraft(null); setFieldErrors({}); }, [refId]);

  const base = isNew
    ? { id: "", description: "",
        model: { profile_id: "" },
        temperature: null, tools: [], system_prompt: [] }
    : res.data;
  // All hooks run unconditionally BEFORE any early return (Rules of
  // Hooks): base flips null -> loaded between renders.
  const managed = !!(base && base.harness_id);
  const cur = draft || base || {};
  const edit = (patch) => {
    if (managed || saving || !base) return;
    setDraft({ ...cur, ...patch });
    docApi.setDirty(true);
  };
  const profileRows = (profiles.data && profiles.data.items) || [];

  const save = async () => {
    if (managed || saving || !base) return;
    const body = draft || cur;
    setSaving(true);
    setFieldErrors({});
    try {
      if (isNew) {
        const payload = { ...body };
        if (!payload.id) delete payload.id;
        const created = await apiFetch("POST", "/agents", payload);
        docApi.setDirty(false);
        window.primerApi.toastPush && window.primerApi.toastPush({
          kind: "success", title: "Agent created: " + created.id });
        docApi.close();
        window.S2_Docs.open("agent", created.id);
      } else {
        await apiFetch("PUT", "/agents/" + encodeURIComponent(refId), body);
        setDraft(null);
        docApi.setDirty(false);
        window.primerApi.toastPush && window.primerApi.toastPush({
          kind: "success", title: "Saved agent " + refId });
      }
    } catch (err) {
      const detail = err && err.envelope && err.envelope.detail;
      if (Array.isArray(detail)) {
        // 422: map pydantic loc paths to field errors, per console
        // conventions (design-pack rule 14: cause + recovery inline).
        const fe = {};
        for (const d of detail) {
          const loc = (d.loc || []).filter((p) => p !== "body");
          fe[loc.join(".") || "form"] = d.msg;
        }
        setFieldErrors(fe);
      } else {
        window.primerApi.toastPush && window.primerApi.toastPush({
          kind: "error",
          title: (err && err.title) || "Save failed",
          detail: err && (err.detail || err.message),
        });
      }
    } finally {
      setSaving(false);
    }
  };

  // Mod+S saves the ACTIVE dirty document; when() keeps it out of the
  // palette when nothing is dirty.
  React.useEffect(() => {
    window.S2_Commands.register({
      id: "doc:save", title: "Save", glyph: "⌘", cat: "edit",
      shortcut: (window.S2_MOD || "Ctrl") + " S",
      when: () => !!draft && !managed,
      run: save,
    });
  });
  React.useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        window.S2_Commands.run("doc:save");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!base) {
    return (
      <div style={{ padding: 16, color: "var(--text-3)",
        fontSize: "var(--fs-12)" }}>
        {res.error ? "Couldn't load this agent. It may have been deleted."
          : "loading…"}
      </div>
    );
  }

  return (
    <div data-testid="s2-agent-doc">
      <div style={{ display: "flex", gap: 10, alignItems: "center",
        padding: "12px 18px", borderBottom: "1px solid var(--border)",
        position: "sticky", top: 0, background: "var(--bg)", zIndex: 5 }}>
        <span style={{ color: "var(--accent)" }}>◆</span>
        <b className="mono" style={{ fontWeight: 600 }}>
          {isNew ? (cur.id || "new agent") : refId}
        </b>
        <span style={{ flex: 1 }} />
        <span className="s2-kbd">{(window.S2_MOD || "Ctrl")} S</span>
        <button data-testid="s2-agent-save"
          disabled={(!draft && !isNew) || managed || saving}
          onClick={save}
          style={{ padding: "5px 12px", borderRadius: "var(--r-6)",
            cursor: "pointer", border: "1px solid var(--accent)",
            background: "var(--accent)", color: "var(--accent-fg)",
            fontWeight: 600, fontSize: "var(--fs-12)",
            opacity: (!draft && !isNew) || managed || saving ? 0.45 : 1 }}>
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
      <div style={{ padding: "14px 18px", maxWidth: 880, display: "grid",
        gap: 12 }}>
        {managed && (
          <div style={{ background: "var(--violet-dim)",
            border: "1px solid var(--violet)", color: "var(--violet)",
            borderRadius: "var(--r-6)", padding: "7px 11px",
            fontSize: "var(--fs-12)" }}>
            ◈ managed by harness {base.harness_id}: direct edits are
            rejected; re-run the harness to change this agent
          </div>
        )}
        {isNew && (
          <S2_Field label="id"
            hint="optional; generated when left blank"
            error={fieldErrors.id}>
            <input style={S2_INPUT_STYLE} value={cur.id || ""}
              data-testid="s2-agent-id"
              onChange={(e) => edit({ id: e.target.value })} />
          </S2_Field>
        )}
        <S2_Field label="description" error={fieldErrors.description}>
          <input style={S2_INPUT_STYLE} value={cur.description || ""}
            data-testid="s2-agent-description" disabled={managed}
            onChange={(e) => edit({ description: e.target.value })} />
        </S2_Field>
        <S2_Field label="model profile"
          hint="the profile carries provider, wire model, context length, and reasoning"
          error={fieldErrors["model.profile_id"]}>
          <select style={S2_INPUT_STYLE} disabled={managed}
            data-testid="s2-agent-profile"
            value={(cur.model || {}).profile_id || ""}
            onChange={(e) => edit({
              model: { profile_id: e.target.value } })}>
            <option value="">choose a profile…</option>
            {profileRows.map((p) => (
              <option key={p.id} value={p.id}>
                {(p.description ? p.description + "  ·  " : "") + p.model_name}
              </option>
            ))}
          </select>
        </S2_Field>
        <S2_Field label="temperature"
          hint="blank defers to the provider default"
          error={fieldErrors.temperature}>
          <input style={S2_INPUT_STYLE} className="mono" disabled={managed}
            data-testid="s2-agent-temperature"
            value={cur.temperature == null ? "" : String(cur.temperature)}
            onChange={(e) => edit({
              temperature: e.target.value === "" ? null : Number(e.target.value) })} />
        </S2_Field>
        <S2_Field label="external tools"
          hint="API callers may attach per-invocation tool definitions; the turn pauses until the caller responds"
          error={fieldErrors.allow_external_tools}>
          <label style={{ display: "flex", alignItems: "center", gap: 8,
            fontSize: "var(--fs-12)", color: "var(--text-2)",
            cursor: managed ? "default" : "pointer" }}>
            <input type="checkbox" disabled={managed}
              data-testid="s2-agent-allow-external-tools"
              checked={!!cur.allow_external_tools}
              onChange={(e) => edit({
                allow_external_tools: e.target.checked })} />
            allow_external_tools
          </label>
        </S2_Field>
        <S2_Field label="system prompt"
          hint="one segment per line; joined at prompt-assembly time"
          error={fieldErrors.system_prompt}>
          <textarea disabled={managed}
            data-testid="s2-agent-prompt"
            style={{ ...S2_INPUT_STYLE, minHeight: 130,
              fontFamily: "var(--font-mono)", lineHeight: 1.5,
              resize: "vertical" }}
            value={(cur.system_prompt || []).join("\n")}
            onChange={(e) => edit({
              system_prompt: e.target.value ? e.target.value.split("\n") : [] })} />
        </S2_Field>
        <S2_Field label="tools">
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {(cur.tools || []).length
              ? cur.tools.map((t) => (
                <span key={t} className="mono" style={{
                  fontSize: "var(--fs-11)", padding: "2px 9px",
                  borderRadius: 9, background: "var(--bg-2)",
                  border: "1px solid var(--border)",
                  color: "var(--text-2)" }}>{t}</span>))
              : <span style={{ fontSize: "var(--fs-12)",
                  color: "var(--text-4)" }}>
                  No tools attached. Tool wiring stays on the classic
                  agent page for now.
                </span>}
          </div>
        </S2_Field>
      </div>
    </div>
  );
}

window.S2_Docs.registerKind("agent", {
  glyph: "◆",
  title: (ref) => (ref === "__new__" ? "new agent" : ref),
  render: (ref, docApi) => <S2_AgentDoc refId={ref} docApi={docApi} />,
});
window.S2_Commands.register({
  id: "new:agent", title: "New agent", glyph: "◆", cat: "create",
  run: () => window.S2_Docs.open("agent", "__new__"),
});
window.S2_AgentDoc = S2_AgentDoc;
