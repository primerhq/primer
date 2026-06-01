/* global React, Icon, Btn, Modal, Banner */
// Triggers list + detail (stub) + create wizard.
// Prefix TR_ to avoid global name collisions.

// ============================================================================
// Constants
// ============================================================================

const TR_SLUG_RE = /^[a-z][a-z0-9-]{1,63}$/;

const TR_CATCHUP_OPTIONS = [
  { value: "one", label: "one — fire once for the most recent missed tick" },
  { value: "all", label: "all — fire once per missed tick" },
  { value: "none", label: "none — drop missed ticks" },
];

// Fallback timezone list used when Intl.supportedValuesOf("timeZone") is
// unavailable (older browsers / non-standard runtimes). Keep this short
// and curated — the spec calls it out as the documented fallback.
const TR_TIMEZONE_FALLBACK = [
  "UTC",
  "America/Los_Angeles",
  "America/Denver",
  "America/Chicago",
  "America/New_York",
  "America/Sao_Paulo",
  "Europe/London",
  "Europe/Berlin",
  "Europe/Paris",
  "Europe/Moscow",
  "Africa/Cairo",
  "Asia/Dubai",
  "Asia/Karachi",
  "Asia/Kolkata",
  "Asia/Singapore",
  "Asia/Tokyo",
  "Australia/Sydney",
  "Pacific/Auckland",
];

// ============================================================================
// Helpers
// ============================================================================

function TR_supportedTimezones() {
  try {
    if (
      typeof Intl !== "undefined"
      && typeof Intl.supportedValuesOf === "function"
    ) {
      const list = Intl.supportedValuesOf("timeZone");
      if (Array.isArray(list) && list.length > 0) return list;
    }
  } catch (_e) { /* fall through */ }
  return TR_TIMEZONE_FALLBACK;
}

function TR_browserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch (_e) {
    return "UTC";
  }
}

function TR_validateSlug(v) {
  if (!v) return "Slug is required";
  if (!TR_SLUG_RE.test(v)) return "Slug must match ^[a-z][a-z0-9-]{1,63}$";
  return "";
}

// Auto-slug a free-text name (lowercase, hyphenate, trim length).
function TR_autoSlug(str) {
  return (str || "")
    .toLowerCase()
    .replace(/[^a-z0-9-]/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63);
}

// Relative-time formatter for next/last fire timestamps. Returns a short
// string like "in 2h" / "5m ago" / "—" for null.
function TR_relTime(iso) {
  if (!iso) return "—";
  const t = typeof iso === "string" ? Date.parse(iso) : (iso instanceof Date ? iso.getTime() : NaN);
  if (!Number.isFinite(t)) return iso;
  const diffMs = t - Date.now();
  const future = diffMs > 0;
  const abs = Math.abs(diffMs);
  const s = Math.round(abs / 1000);
  const m = Math.round(s / 60);
  const h = Math.round(m / 60);
  const d = Math.round(h / 24);
  let body;
  if (s < 45) body = `${s}s`;
  else if (m < 45) body = `${m}m`;
  else if (h < 36) body = `${h}h`;
  else body = `${d}d`;
  return future ? `in ${body}` : `${body} ago`;
}

// Build the default datetime-local value for "now + 1 hour" in the
// browser's local timezone (datetime-local inputs use local time, no
// offset). Returns "YYYY-MM-DDTHH:MM".
function TR_defaultFireAtLocal() {
  const d = new Date(Date.now() + 60 * 60 * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return (
    d.getFullYear() + "-" +
    pad(d.getMonth() + 1) + "-" +
    pad(d.getDate()) + "T" +
    pad(d.getHours()) + ":" +
    pad(d.getMinutes())
  );
}

// ============================================================================
// TR_TriggersPage — router shim
// ============================================================================

function TR_TriggersPage({ triggerId }) {
  const { useRouter } = window.primerApi;
  const { params } = useRouter();
  const id = triggerId || params.id;
  if (id) {
    const Detail = window.TR_TriggerDetail;
    if (Detail) return <Detail id={id} />;
    return null;
  }
  return <TR_TriggerList />;
}

// ============================================================================
// TR_TriggerList
// ============================================================================

function TR_TriggerList() {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const [createOpen, setCreateOpen] = React.useState(false);

  const list = useResource(
    "triggers:list",
    (signal) => apiFetch("GET", "/triggers", null, { signal }),
    { pollMs: 5000 },
  );

  const items = list.data?.items ?? [];

  const onCreated = (trigger) => {
    setCreateOpen(false);
    list.refetch();
    navigate("/triggers/" + trigger.id);
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <span style={{ fontSize: 13, fontWeight: 600 }}>Triggers</span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>Create trigger</Btn>
        </div>
      </div>

      {list.loading && items.length === 0 && (
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
      )}
      {list.error && items.length === 0 && (
        <Banner
          kind="error"
          title={list.error.title || "Couldn't load triggers"}
          detail={list.error.detail || list.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
        />
      )}
      {!list.loading && !list.error && items.length === 0 && (
        <div className="empty" style={{ padding: "40px 20px" }}>
          <div className="ico-wrap"><Icon name="clock" size={22} /></div>
          <div className="head">No triggers configured</div>
          <div className="sub">
            Triggers fire on a delay or cron schedule and dispatch to subscriptions
            (chat messages, fresh agent sessions, fresh graph sessions).
          </div>
          <div className="actions">
            <Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>Create trigger</Btn>
          </div>
        </div>
      )}

      {items.length > 0 && (
        <div
          data-testid="triggers-grid"
          style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 12 }}
        >
          {items.map((t) => (
            <TR_TriggerCard key={t.id} trigger={t} onOpen={() => navigate("/triggers/" + t.id)} />
          ))}
        </div>
      )}

      {createOpen && (
        <TR_CreateTriggerDialog
          onClose={() => setCreateOpen(false)}
          onCreated={onCreated}
        />
      )}
    </div>
  );
}

// ============================================================================
// TR_TriggerCard
// ============================================================================

function TR_TriggerCard({ trigger, onOpen }) {
  const kind = trigger?.config?.kind || "—";
  const subsCount = Array.isArray(trigger?.subscriptions)
    ? trigger.subscriptions.length
    : (typeof trigger?.subscription_count === "number" ? trigger.subscription_count : null);
  const enabled = !!trigger.enabled;
  return (
    <div
      className="panel"
      data-testid={`trigger-card-${trigger.id}`}
      style={{ cursor: "pointer", transition: "border-color 0.15s" }}
      onClick={onOpen}
      onMouseEnter={(e) => e.currentTarget.style.borderColor = "var(--accent)"}
      onMouseLeave={(e) => e.currentTarget.style.borderColor = ""}
    >
      <div className="panel-body" style={{ padding: "12px 14px" }}>
        <div style={{ display: "flex", alignItems: "flex-start", gap: 8, marginBottom: 6 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 13.5, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {trigger.name || trigger.slug}
            </div>
            <div className="mono muted text-sm" style={{ fontSize: 11 }}>{trigger.slug}</div>
          </div>
          <span
            className="pill pill-paused"
            title={`Trigger kind: ${kind}`}
            style={{ fontSize: 10.5 }}
          >
            {kind}
          </span>
          <span
            className={`pill ${enabled ? "pill-claimed" : "pill-ended"}`}
            title={enabled ? "Trigger is enabled" : "Trigger is disabled"}
            style={{ fontSize: 10.5 }}
          >
            {enabled ? "enabled" : "disabled"}
          </span>
        </div>

        {trigger.description && (
          <div className="muted text-sm" style={{ marginBottom: 6, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {trigger.description}
          </div>
        )}

        <div className="muted text-sm" style={{ fontSize: 11.5, display: "flex", flexWrap: "wrap", gap: 10 }}>
          <span title={trigger.next_fire_at || ""}>
            <span style={{ color: "var(--text-3)" }}>next </span>
            <span className="mono">{TR_relTime(trigger.next_fire_at)}</span>
          </span>
          <span title={trigger.last_fired_at || ""}>
            <span style={{ color: "var(--text-3)" }}>last </span>
            <span className="mono">{TR_relTime(trigger.last_fired_at)}</span>
          </span>
          {subsCount != null && (
            <span>
              <span style={{ color: "var(--text-3)" }}>· </span>
              {subsCount} {subsCount === 1 ? "subscription" : "subscriptions"}
            </span>
          )}
        </div>

        {trigger.last_fire_error && (
          <div style={{ marginTop: 6 }}>
            <span
              className="pill pill-failed"
              title={typeof trigger.last_fire_error === "string"
                ? trigger.last_fire_error
                : JSON.stringify(trigger.last_fire_error)}
              style={{ fontSize: 10.5 }}
            >
              last fire error
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================================
// TR_CreateTriggerDialog — three-step wizard for POST /v1/triggers
//
// Step 1: kind picker (delayed | scheduled)
// Step 2: per-kind config
//   delayed   -> datetime-local input for fire_at (default now + 1 hour)
//   scheduled -> cron expression + IANA timezone dropdown + catchup
// Step 3: slug (validated client-side), name, description
// Submits POST /v1/triggers with {slug, name, description, config, enabled: true}
// then navigates to /triggers/{id} via window.primerApi.useRouter().navigate.
// ============================================================================

function TR_CreateTriggerDialog({ onClose, onCreated }) {
  const { apiFetch } = window.primerApi;

  const [step, setStep] = React.useState(1);

  // Step 1
  const [kind, setKind] = React.useState("delayed");

  // Step 2 — delayed
  const [fireAtLocal, setFireAtLocal] = React.useState(TR_defaultFireAtLocal());

  // Step 2 — scheduled
  const [cron, setCron] = React.useState("0 * * * *");
  const [timezone, setTimezone] = React.useState(TR_browserTimezone());
  const [catchup, setCatchup] = React.useState("one");
  const timezones = React.useMemo(() => TR_supportedTimezones(), []);

  // Step 3
  const [slug, setSlug] = React.useState("");
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [slugError, setSlugError] = React.useState("");

  // Submit state
  const [submitError, setSubmitError] = React.useState(null); // {code, message} | string
  const [busy, setBusy] = React.useState(false);

  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const onNameChange = (v) => {
    setName(v);
    if (!slug || slug === TR_autoSlug(name)) {
      setSlug(TR_autoSlug(v));
    }
  };

  const step1Valid = kind === "delayed" || kind === "scheduled";
  const step2Valid = (
    kind === "delayed"
      ? !!fireAtLocal
      : (kind === "scheduled" ? (!!cron && !!timezone && !!catchup) : false)
  );
  const step3Valid = !TR_validateSlug(slug) && !!name;

  // Build the trigger config payload for POST /v1/triggers.
  const buildConfig = () => {
    if (kind === "delayed") {
      // datetime-local has no timezone — interpret as the browser's
      // local time and emit a UTC ISO8601 instant so the server stores
      // a tz-aware timestamp.
      const dt = new Date(fireAtLocal);
      const fireAtIso = isNaN(dt.getTime()) ? fireAtLocal : dt.toISOString();
      return { kind: "delayed", fire_at: fireAtIso };
    }
    return {
      kind: "scheduled",
      cron,
      timezone,
      catchup,
    };
  };

  const submit = async () => {
    const slugErr = TR_validateSlug(slug);
    if (slugErr) { setSlugError(slugErr); return; }
    setSlugError("");
    setSubmitError(null);
    setBusy(true);
    try {
      const body = {
        slug,
        name: name || slug,
        description: description || null,
        config: buildConfig(),
        enabled: true,
      };
      // POST /v1/triggers — see primer/api/routers/triggers.py:create_trigger_endpoint
      const created = await apiFetch("POST", "/triggers", body);
      if (!mountedRef.current) return;
      onCreated(created);
    } catch (err) {
      if (!mountedRef.current) return;
      // Server error shape: {detail: {code, message}} (see _raise_code).
      // FastAPI unwraps `detail` into envelope.detail when status != 422.
      // The ApiError stores envelope.detail as `detail` directly, which
      // may be an object {code, message} or a string.
      const env = err && err.envelope;
      const envDetail = env && env.detail;
      let code = null;
      let msg = null;
      if (envDetail && typeof envDetail === "object") {
        code = envDetail.code || null;
        msg = envDetail.message || null;
      }
      if (!msg && typeof err.detail === "string") msg = err.detail;
      if (!msg) msg = err.title || err.message || "Request failed";
      setSubmitError({ code, message: msg });
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };

  const stepTitle = step === 1
    ? "Create trigger — Step 1: Kind"
    : step === 2
      ? `Create trigger — Step 2: ${kind === "delayed" ? "Delay" : "Schedule"}`
      : "Create trigger — Step 3: Details";

  return (
    <Modal
      title={stepTitle}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          {step > 1 && (
            <Btn kind="ghost" icon="chevron-left" onClick={() => setStep(step - 1)} disabled={busy}>Back</Btn>
          )}
          {step < 3 && (
            <Btn
              kind="primary"
              icon="chevron-right"
              onClick={() => setStep(step + 1)}
              disabled={step === 1 ? !step1Valid : !step2Valid}
            >
              Next
            </Btn>
          )}
          {step === 3 && (
            <Btn
              kind="primary"
              icon="check"
              onClick={submit}
              disabled={busy || !step3Valid}
            >
              {busy ? "Creating…" : "Create"}
            </Btn>
          )}
        </>
      }
    >
      {step === 1 && (
        <div data-testid="tr-step-kind">
          <div className="field-help" style={{ marginBottom: 10 }}>
            Pick the trigger kind. Delayed triggers fire once at a chosen instant; scheduled
            triggers fire on a cron schedule.
          </div>
          <div className="field">
            <label
              className="row"
              style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "8px 0", cursor: "pointer" }}
            >
              <input
                type="radio"
                name="tr-kind"
                value="delayed"
                checked={kind === "delayed"}
                onChange={() => setKind("delayed")}
              />
              <div>
                <div style={{ fontWeight: 600 }}>Delayed</div>
                <div className="muted text-sm">One-off. Fires once at a chosen UTC instant.</div>
              </div>
            </label>
            <label
              className="row"
              style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "8px 0", cursor: "pointer" }}
            >
              <input
                type="radio"
                name="tr-kind"
                value="scheduled"
                checked={kind === "scheduled"}
                onChange={() => setKind("scheduled")}
              />
              <div>
                <div style={{ fontWeight: 600 }}>Scheduled</div>
                <div className="muted text-sm">Recurring cron expression evaluated in a chosen timezone.</div>
              </div>
            </label>
          </div>
        </div>
      )}

      {step === 2 && kind === "delayed" && (
        <div data-testid="tr-step-delayed">
          <div className="field">
            <label className="field-label" htmlFor="tr-fire-at">
              Fire at <span className="hint">browser local time · converted to UTC on submit</span>
            </label>
            <input
              id="tr-fire-at"
              className="input mono"
              type="datetime-local"
              value={fireAtLocal}
              onChange={(e) => setFireAtLocal(e.target.value)}
              style={{ width: "100%" }}
            />
            <div className="field-help muted text-sm" style={{ marginTop: 4 }}>
              Defaults to one hour from now.
            </div>
          </div>
        </div>
      )}

      {step === 2 && kind === "scheduled" && (
        <div data-testid="tr-step-scheduled">
          <div className="field">
            <label className="field-label" htmlFor="tr-cron">
              Cron expression <span className="hint">standard 5-field cron (m h dom mon dow)</span>
            </label>
            <input
              id="tr-cron"
              className="input mono"
              value={cron}
              onChange={(e) => setCron(e.target.value)}
              placeholder="0 * * * *"
              style={{ width: "100%" }}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="tr-timezone">
              Timezone <span className="hint">IANA tz name</span>
            </label>
            <select
              id="tr-timezone"
              className="input mono"
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
              style={{ width: "100%" }}
            >
              {timezones.map((tz) => (
                <option key={tz} value={tz}>{tz}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="tr-catchup">
              Catchup policy <span className="hint">behaviour after downtime</span>
            </label>
            <select
              id="tr-catchup"
              className="input"
              value={catchup}
              onChange={(e) => setCatchup(e.target.value)}
              style={{ width: "100%" }}
            >
              {TR_CATCHUP_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
        </div>
      )}

      {step === 3 && (
        <div data-testid="tr-step-meta">
          <div className="field">
            <label className="field-label" htmlFor="tr-name">Name</label>
            <input
              id="tr-name"
              className="input"
              value={name}
              onChange={(e) => onNameChange(e.target.value)}
              placeholder="My trigger"
              style={{ width: "100%" }}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="tr-slug">
              Slug <span className="hint">^[a-z][a-z0-9-]{1,63}$</span>
            </label>
            <input
              id="tr-slug"
              className="input mono"
              value={slug}
              onChange={(e) => { setSlug(e.target.value); setSlugError(TR_validateSlug(e.target.value)); }}
              placeholder="my-trigger"
              style={{ width: "100%" }}
            />
            {slugError && <div className="field-help" style={{ color: "var(--red)" }}>{slugError}</div>}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="tr-description">Description <span className="hint">optional</span></label>
            <textarea
              id="tr-description"
              className="input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              style={{ width: "100%", resize: "vertical" }}
            />
          </div>
          {submitError && (
            <Banner
              kind="error"
              title={submitError.code ? `Create failed (${submitError.code})` : "Create failed"}
              detail={submitError.message || ""}
            />
          )}
        </div>
      )}
    </Modal>
  );
}

// ============================================================================
// TR_TriggerDetail — placeholder stub (real implementation lands in Phase 10).
// ============================================================================

function TR_TriggerDetail({ id }) {
  const { useRouter } = window.primerApi;
  const { navigate } = useRouter();
  return (
    <div className="col" style={{ gap: 14 }}>
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("/triggers")}>Back</Btn>
      </div>
      <div className="panel">
        <div className="panel-h"><Icon name="clock" size={13} /><span>Trigger</span></div>
        <div className="panel-body" style={{ padding: "12px 14px" }}>
          <div className="muted text-sm">
            Trigger detail view is coming in Phase 10. Trigger id:{" "}
            <span className="mono">{id}</span>.
          </div>
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// Exports
// ============================================================================

window.TR_TriggersPage = TR_TriggersPage;
window.TR_TriggerList = TR_TriggerList;
window.TR_TriggerCard = TR_TriggerCard;
window.TR_TriggerDetail = TR_TriggerDetail;
window.TR_CreateTriggerDialog = TR_CreateTriggerDialog;
