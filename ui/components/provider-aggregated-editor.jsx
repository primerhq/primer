/* global React, Btn */

// The aggregated LLM provider variant: an ordered member picker plus the
// routing and failover switches. Ported out of ui/components/providers.jsx
// so the page can be deleted; an ordered list of (provider, model) pairs
// is not a flat field set, which is why llm_providers/_types marks this
// type variant:"aggregated" instead of describing config_fields.

// Mirrors the backend field defaults exactly (primer/model/providers/
// llm.py:275-285: RoutingStrategy.SEQUENTIAL, FailoverPoint.
// BEFORE_FIRST_TOKEN, FailoverClasses.TRANSIENT_AND_CONFIG).
const PC_AGG_CONFIG_DEFAULT = {
  members: [],
  strategy: "sequential",
  failover_point: "before_first_token",
  failover_on: "transient_and_config",
};

function PC_Toggle({ checked, onChange, label, help, disabled, testid }) {
  return (
    <label style={{ display: "flex", alignItems: "flex-start", gap: 10,
      cursor: disabled ? "default" : "pointer", opacity: disabled ? 0.5 : 1 }}>
      <button type="button" role="switch" aria-checked={checked} disabled={disabled}
        data-testid={testid} onClick={() => !disabled && onChange(!checked)}
        style={{ flex: "0 0 auto", width: 34, height: 20, borderRadius: 999,
          border: "1px solid var(--border)", padding: 0, marginTop: 1,
          background: checked ? "var(--accent)" : "var(--bg-2)", position: "relative",
          cursor: disabled ? "default" : "pointer", transition: "background 0.12s ease" }}>
        <span style={{ position: "absolute", top: 1, left: checked ? 15 : 1, width: 16,
          height: 16, borderRadius: "50%", background: checked ? "var(--accent-fg)" : "var(--text-3)",
          transition: "left 0.12s ease" }} />
      </button>
      <span style={{ fontSize: 12.5, lineHeight: 1.4 }}>
        {label}{help && <span className="muted"> - {help}</span>}
      </span>
    </label>
  );
}

function PC_AggregatedEditor({ value, onChange, candidates, profiles }) {
  // value: { members: [{provider_id, model_name}], strategy, failover_point,
  //          failover_on }
  const v = value || PC_AGG_CONFIG_DEFAULT;
  const members = v.members || [];
  const set = (patch) => onChange({ ...v, ...patch });
  const setMember = (i, patch) =>
    set({ members: members.map((m, j) => (j === i ? { ...m, ...patch } : m)) });
  const move = (i, d) => {
    const j = i + d;
    if (j < 0 || j >= members.length) return;
    const next = members.slice();
    [next[i], next[j]] = [next[j], next[i]];
    set({ members: next });
  };
  const remove = (i) => set({ members: members.filter((_, j) => j !== i) });
  const add = () => set({ members: [...members, { provider_id: "", model_name: "" }] });
  // A member pins a downstream (provider, model) PAIR rather than a
  // profile: the aggregated adapter dispatches to the upstream directly,
  // so there is no profile of its own to resolve. The model names still
  // come from that provider's ModelProfile rows, because that is what a
  // provider publishes now. Deduped: several profiles may name one model,
  // and a member picks the model, not the profile.
  const modelsFor = (pid) => [
    ...new Set(
      (profiles || [])
        .filter((pr) => pr.provider_id === pid)
        .map((pr) => pr.model_name),
    ),
  ];
  return (
    <div className="field" data-testid="provider-form-aggregated">
      <label className="field-label">Members (ordered; failover walks top to bottom)</label>
      {members.map((m, i) => (
        <div key={i} style={{ display: "flex", gap: 6, marginTop: 4, alignItems: "center" }}>
          <span className="mono muted">#{i + 1}</span>
          <select className="select" value={m.provider_id}
            onChange={(e) => setMember(i, { provider_id: e.target.value, model_name: "" })}>
            <option value="">select provider...</option>
            {(candidates || []).map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}
          </select>
          <select className="select" value={m.model_name}
            onChange={(e) => setMember(i, { model_name: e.target.value })}>
            <option value="">select model...</option>
            {modelsFor(m.provider_id).map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <Btn size="sm" kind="ghost" onClick={() => move(i, -1)} title="Up">Up</Btn>
          <Btn size="sm" kind="ghost" onClick={() => move(i, 1)} title="Down">Down</Btn>
          <Btn size="sm" kind="ghost" onClick={() => remove(i)} title="Remove">Remove</Btn>
        </div>
      ))}
      <div style={{ marginTop: 6 }}>
        <Btn size="sm" onClick={add}>Add member</Btn>
      </div>
      <div style={{ marginTop: 10, display: "grid", gap: 8 }}>
        <PC_Toggle checked={(v.strategy || "sequential") === "round_robin"}
          onChange={(on) => set({ strategy: on ? "round_robin" : "sequential" })}
          label="Round-robin" help="rotate the starting member per call (off = sequential)" />
        <PC_Toggle checked={(v.failover_point || "before_first_token") === "mid_stream"}
          onChange={(on) => set({ failover_point: on ? "mid_stream" : "before_first_token" })}
          label="Mid-stream failover" help="may duplicate already-shown tokens (off = before first token)" />
        <div className="field">
          <label className="field-label">Failover on</label>
          <select className="select" value={v.failover_on || "transient_and_config"}
            onChange={(e) => set({ failover_on: e.target.value })} style={{ width: "100%" }}>
            <option value="transient">transient</option>
            <option value="transient_and_config">transient_and_config</option>
          </select>
        </div>
      </div>
    </div>
  );
}

window.PC_AGG_CONFIG_DEFAULT = PC_AGG_CONFIG_DEFAULT;
window.PC_Toggle = PC_Toggle;
window.PC_AggregatedEditor = PC_AggregatedEditor;
