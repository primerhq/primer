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

// Build the full webhook URL from a trigger object.
// Uses window.location.origin so it adapts to the deployment URL.
function TR_webhookUrl(trigger) {
  if (!trigger || trigger.config?.kind !== "webhook") return null;
  const token = trigger.config?.token;
  if (!token) return null;
  const origin = (typeof window !== "undefined" && window.location?.origin) || "";
  return `${origin}/v1/webhooks/${token}`;
}

// Copy text to clipboard + flash a brief toast.
function TR_CopyButton({ text, label, testId }) {
  const [copied, setCopied] = React.useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (_e) {
      // Fallback: select the text.
    }
  };
  return (
    <Btn size="sm" kind="ghost" icon={copied ? "check" : "copy"} onClick={copy} data-testid={testId}>
      {copied ? "Copied!" : (label || "Copy")}
    </Btn>
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

// Page size for the triggers list table pager. Mirrors the windowing
// pattern used by the provider model picker (PAGE_SIZE = 50).
const TR_PAGE_SIZE = 25;

function TR_TriggerList() {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const [createOpen, setCreateOpen] = React.useState(false);
  const [page, setPage] = React.useState(0);

  const list = useResource(
    "triggers:list",
    (signal) => apiFetch("GET", "/triggers", null, { signal }),
    { pollMs: 5000 },
  );

  const items = list.data?.items ?? [];

  // Client-side windowing - the list endpoint returns the full set, so we
  // slice locally and expose Prev/Next controls for visual parity with the
  // other paginated console list pages.
  const total = items.length;
  const pages = Math.max(1, Math.ceil(total / TR_PAGE_SIZE));
  const safePage = Math.min(page, pages - 1);
  const start = safePage * TR_PAGE_SIZE;
  const rows = items.slice(start, start + TR_PAGE_SIZE);

  // Clamp back into range if the set shrinks (delete) under us.
  React.useEffect(() => {
    if (page > pages - 1) setPage(pages - 1);
  }, [page, pages]);

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
            Triggers fire on a delay, cron schedule, or inbound webhook POST
            and dispatch to subscriptions (chat messages, fresh agent sessions,
            fresh graph sessions).
          </div>
          <div className="actions">
            <Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>Create trigger</Btn>
          </div>
        </div>
      )}

      {items.length > 0 && (
        <div
          data-testid="triggers-grid"
          className="panel"
          style={{ padding: 0, overflow: "hidden" }}
        >
          <table className="table" data-testid="triggers-table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Name</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Kind</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Schedule</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Status</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Next fire</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Created</th>
                <th style={{ textAlign: "right", padding: "8px 12px" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => (
                <TR_TriggerRow
                  key={t.id}
                  trigger={t}
                  onOpen={() => navigate("/triggers/" + t.id)}
                  onChanged={list.refetch}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {total > 0 && pages > 1 && (
        <div
          data-testid="triggers-pager"
          style={{ display: "flex", gap: 8, alignItems: "center", justifyContent: "flex-end" }}
        >
          <Btn
            size="sm"
            kind="ghost"
            icon="chevron-left"
            onClick={() => setPage(Math.max(0, safePage - 1))}
            disabled={safePage === 0}
          >
            Prev
          </Btn>
          <span className="muted text-sm">
            Page {safePage + 1} of {pages} · {total} {total === 1 ? "trigger" : "triggers"}
          </span>
          <Btn
            size="sm"
            kind="ghost"
            icon="chevron-right"
            onClick={() => setPage(Math.min(pages - 1, safePage + 1))}
            disabled={safePage >= pages - 1}
          >
            Next
          </Btn>
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

// Short human label for a trigger's schedule/detail, keyed on kind:
//   delayed   -> the fire_at instant
//   scheduled -> "cron · timezone"
//   webhook   -> "webhook" (the URL lives on the detail page)
function TR_scheduleLabel(trigger) {
  const cfg = trigger?.config || {};
  // Reuse the shared empty placeholder (TR_relTime(null)) so the glyph
  // matches the rest of the page without re-typing the placeholder here.
  const dash = TR_relTime(null);
  if (cfg.kind === "delayed") return cfg.fire_at || dash;
  if (cfg.kind === "scheduled") {
    return cfg.cron ? `${cfg.cron}${cfg.timezone ? " · " + cfg.timezone : ""}` : dash;
  }
  if (cfg.kind === "webhook") return "webhook";
  return dash;
}

// ============================================================================
// TR_TriggerRow - one row of the triggers list table.
//
// Replaces the former card. Mirrors the AT_TokenRow pattern: click the row
// to open the detail page, with inline Fire-now / Delete actions that stop
// propagation. Edit and the webhook URL reveal live on the detail page,
// reached via the row click - parity with how api_tokens routes edit-like
// affordances through the detail surface.
// ============================================================================

function TR_TriggerRow({ trigger, onOpen, onChanged }) {
  const { apiFetch } = window.primerApi;
  const kind = trigger?.config?.kind || "—";
  const enabled = !!trigger.enabled;
  const [busy, setBusy] = React.useState(false);

  const stop = (e) => { e.stopPropagation(); };

  const fireNow = async (e) => {
    e.stopPropagation();
    setBusy(true);
    try {
      await apiFetch("POST", "/triggers/" + encodeURIComponent(trigger.id) + "/fire_now", {});
      onChanged && onChanged();
    } catch (_err) { /* surfaced on the detail page; row stays put */ }
    finally { setBusy(false); }
  };

  const remove = async (e) => {
    e.stopPropagation();
    if (!window.confirm(`Delete trigger ${trigger.name || trigger.slug}? This cascades to its subscriptions and cannot be undone.`)) return;
    setBusy(true);
    try {
      await apiFetch("DELETE", "/triggers/" + encodeURIComponent(trigger.id));
      onChanged && onChanged();
    } catch (_err) { /* ignore - detail page surfaces errors */ }
    finally { setBusy(false); }
  };

  return (
    <tr
      data-testid={`trigger-row-${trigger.id}`}
      onClick={onOpen}
      style={{ borderTop: "1px solid var(--border)", cursor: "pointer" }}
    >
      <td style={{ padding: "8px 12px" }}>
        <div style={{ fontWeight: 600 }}>{trigger.name || trigger.slug}</div>
        <div className="mono muted text-sm" style={{ fontSize: 11 }}>{trigger.slug}</div>
        {trigger.last_fire_error && (
          <span
            className="pill pill-failed"
            title={typeof trigger.last_fire_error === "string"
              ? trigger.last_fire_error
              : JSON.stringify(trigger.last_fire_error)}
            style={{ fontSize: 10.5, marginTop: 4, display: "inline-block" }}
          >
            last fire error
          </span>
        )}
      </td>
      <td style={{ padding: "8px 12px" }}>
        <span className="pill pill-paused" title={`Trigger kind: ${kind}`} style={{ fontSize: 10.5 }}>
          {kind}
        </span>
      </td>
      <td style={{ padding: "8px 12px" }}>
        <span className="mono" style={{ fontSize: 11, wordBreak: "break-word" }}>
          {TR_scheduleLabel(trigger)}
        </span>
      </td>
      <td style={{ padding: "8px 12px" }}>
        <span
          className={`pill ${enabled ? "pill-claimed" : "pill-ended"}`}
          title={enabled ? "Trigger is enabled" : "Trigger is disabled"}
          style={{ fontSize: 10.5 }}
        >
          {enabled ? "enabled" : "disabled"}
        </span>
      </td>
      <td style={{ padding: "8px 12px" }} title={trigger.next_fire_at || ""}>
        <span className="mono">{TR_relTime(trigger.next_fire_at)}</span>
      </td>
      <td style={{ padding: "8px 12px" }} title={trigger.created_at || ""}>
        <span className="mono">{TR_relTime(trigger.created_at)}</span>
      </td>
      <td style={{ padding: "8px 12px", textAlign: "right", whiteSpace: "nowrap" }} onClick={stop}>
        <Btn
          size="sm"
          kind="ghost"
          icon="zap"
          disabled={busy}
          onClick={fireNow}
          title="Fire this trigger immediately, bypassing the schedule"
          data-testid={`trigger-row-fire-${trigger.id}`}
        >
          Fire now
        </Btn>
        <Btn
          size="sm"
          kind="ghost"
          icon="edit"
          disabled={busy}
          onClick={(e) => { e.stopPropagation(); onOpen(); }}
          title="Open the trigger to edit it"
          data-testid={`trigger-row-edit-${trigger.id}`}
        >
          Edit
        </Btn>
        <Btn
          size="sm"
          kind="danger"
          icon="trash"
          disabled={busy}
          onClick={remove}
          title="Delete this trigger"
          data-testid={`trigger-row-delete-${trigger.id}`}
        >
          Delete
        </Btn>
      </td>
    </tr>
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

// Kind registry — single source of truth for the Step 1 dropdown.
// To add a new trigger kind: append an entry here and add a render
// branch in Step 2 (the existing `delayed`/`scheduled` blocks
// below are the template).
const TR_KIND_OPTIONS = [
  {
    value: "delayed",
    label: "Delayed",
    help: "One-off. Fires once at a chosen UTC instant.",
  },
  {
    value: "scheduled",
    label: "Scheduled",
    help: "Recurring cron expression evaluated in a chosen timezone.",
  },
  {
    value: "webhook",
    label: "Webhook",
    help: "Event-driven. Fires when an HTTP POST arrives at the generated URL.",
  },
];

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

  const step1Valid = kind === "delayed" || kind === "scheduled" || kind === "webhook";
  const step2Valid = (
    kind === "delayed"
      ? !!fireAtLocal
      : kind === "scheduled"
        ? (!!cron && !!timezone && !!catchup)
        : kind === "webhook"
          ? true  // No config required - server mints the token
          : false
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
    if (kind === "webhook") {
      // Token is server-minted - omit from payload (or send empty string).
      // hmac_secret can be set after creation via PUT.
      return { kind: "webhook" };
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
      ? `Create trigger - Step 2: ${kind === "delayed" ? "Delay" : kind === "scheduled" ? "Schedule" : "Webhook"}`
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
            Pick the trigger kind. The list below grows as new kinds
            land; their config form lands on Step 2 once selected.
          </div>
          <div className="field">
            <label className="field-label" htmlFor="tr-kind-select">Kind</label>
            <select
              id="tr-kind-select"
              className="select"
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              style={{ width: "100%" }}
            >
              {TR_KIND_OPTIONS.map((k) => (
                <option key={k.value} value={k.value}>{k.label}</option>
              ))}
            </select>
            <div className="field-help muted text-sm" style={{ marginTop: 6 }}>
              {TR_KIND_OPTIONS.find((k) => k.value === kind)?.help || ""}
            </div>
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

      {step === 2 && kind === "webhook" && (
        <div data-testid="tr-step-webhook">
          <div className="field-help muted text-sm" style={{ marginBottom: 12 }}>
            The server will generate a unique, unguessable URL for this webhook.
            You can optionally add an HMAC secret for signature verification after
            the trigger is created.
          </div>
          <div className="field">
            <div
              className="panel"
              style={{ background: "var(--surface-1)", padding: "10px 14px", borderRadius: 4 }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <Icon name="info" size={13} />
                <span className="muted text-sm">
                  A secure token will be minted on create. The webhook URL will
                  be shown on the trigger detail page.
                </span>
              </div>
            </div>
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
// TR_TriggerEditDialog — edit name / description / enabled.
//
// Per Spec §13.4: the detail page exposes an Edit affordance for the
// trigger metadata. `config` is mostly immutable here — the kind cannot
// change (server returns 409 trigger_kind_immutable) and the kind-specific
// payload (fire_at / cron / timezone / catchup) is intentionally NOT
// editable in this dialog to keep the surface tight. Operators that need
// to change schedule should delete + recreate.
// ============================================================================

function TR_TriggerEditDialog({ trigger, onClose, onSaved }) {
  const { apiFetch } = window.primerApi;
  const [name, setName] = React.useState(trigger.name || "");
  const [description, setDescription] = React.useState(trigger.description || "");
  const [enabled, setEnabled] = React.useState(!!trigger.enabled);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);

  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const updated = await apiFetch(
        "PUT",
        "/triggers/" + encodeURIComponent(trigger.id),
        { name: name || trigger.slug, description: description || null, enabled },
      );
      if (!mountedRef.current) return;
      onSaved(updated);
    } catch (err) {
      if (!mountedRef.current) return;
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
      setError({ code, message: msg });
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };

  return (
    <Modal
      title={`Edit trigger · ${trigger.slug}`}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn kind="primary" icon="check" onClick={submit} disabled={busy}>
            {busy ? "Saving…" : "Save changes"}
          </Btn>
        </>
      }
    >
      <div data-testid="tr-edit-form">
        <div className="field">
          <label className="field-label" htmlFor="tr-edit-name">Name</label>
          <input
            id="tr-edit-name"
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            style={{ width: "100%" }}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="tr-edit-description">
            Description <span className="hint">optional</span>
          </label>
          <textarea
            id="tr-edit-description"
            className="input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            style={{ width: "100%", resize: "vertical" }}
          />
        </div>
        <div className="field">
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            <span>Enabled</span>
          </label>
          <div className="field-help muted text-sm" style={{ marginTop: 4 }}>
            Disabled triggers never fire. The claim engine will not schedule them.
          </div>
        </div>
        <div className="field-help muted text-sm">
          Kind, schedule, and other config fields are immutable. To change them, delete this
          trigger and create a new one.
        </div>
        {error && (
          <Banner
            kind="error"
            title={error.code ? `Save failed (${error.code})` : "Save failed"}
            detail={error.message || ""}
          />
        )}
      </div>
    </Modal>
  );
}

// ============================================================================
// TR_FireErrorChip — small red chip rendering a {code, message} or plain string.
// ============================================================================

function TR_FireErrorChip({ error, testId }) {
  if (!error) return null;
  let code = null;
  let msg = null;
  if (typeof error === "string") {
    msg = error;
  } else if (error && typeof error === "object") {
    code = error.code || error.error_code || null;
    msg = error.message || error.error_message || error.detail || null;
    if (!msg) {
      try { msg = JSON.stringify(error); } catch (_e) { msg = String(error); }
    }
  }
  const title = code ? `${code}: ${msg || ""}` : (msg || "");
  return (
    <span
      data-testid={testId}
      className="pill pill-failed"
      title={title}
      style={{ fontSize: 10.5 }}
    >
      {code || "error"}
      {msg ? <span style={{ marginLeft: 6, fontWeight: 400, opacity: 0.85 }}>{msg}</span> : null}
    </span>
  );
}

// ============================================================================
// TR_SubTargetLabel — human-readable target for a subscription row.
//
// Per Spec §13.4: chat id / agent name / graph name / "(dynamic) session-{id}"
// for parked_session. We fetch by id when available (workspace + agent/graph
// or chat) so we can show a friendly name; falls back to the raw id.
// ============================================================================

function TR_SubTargetLabel({ sub }) {
  const cfg = sub?.config || {};
  const kind = cfg.kind;
  if (kind === "chat_message") {
    return <span className="mono">{cfg.chat_id || "—"}</span>;
  }
  if (kind === "agent_fresh_session") {
    return (
      <span className="mono" title={`workspace ${cfg.workspace_id || ""}`}>
        {cfg.agent_id || "—"}
      </span>
    );
  }
  if (kind === "graph_fresh_session") {
    return (
      <span className="mono" title={`workspace ${cfg.workspace_id || ""}`}>
        {cfg.graph_id || "—"}
      </span>
    );
  }
  if (kind === "parked_session") {
    return (
      <span className="muted">
        <span style={{ marginRight: 4 }}>(dynamic)</span>
        <span className="mono">session-{cfg.session_id || "?"}</span>
      </span>
    );
  }
  return <span className="muted">—</span>;
}

// ============================================================================
// TR_SubscriptionsPanel — table of subscriptions with inline toggles + actions.
//
// One row per subscription. The parked_session rows are read-only per
// Spec §13.6 — operators cannot toggle enabled / parallelism (the agent
// owns the lifecycle); only the delete button is offered, which the
// server treats as cancel-and-unpark.
// ============================================================================

function TR_SubscriptionsPanel({ trigger, subs, onChanged, onAdd, onEdit }) {
  const { apiFetch } = window.primerApi;
  const [busyId, setBusyId] = React.useState(null);
  const [error, setError] = React.useState(null);

  const setEnabled = async (sub, enabled) => {
    setBusyId(sub.id);
    setError(null);
    try {
      await apiFetch(
        "PUT",
        "/triggers/" + encodeURIComponent(trigger.id)
          + "/subscriptions/" + encodeURIComponent(sub.id),
        { enabled },
      );
      onChanged();
    } catch (err) {
      setError(err);
    } finally {
      setBusyId(null);
    }
  };

  const setParallelism = async (sub, parallelism) => {
    setBusyId(sub.id);
    setError(null);
    try {
      await apiFetch(
        "PUT",
        "/triggers/" + encodeURIComponent(trigger.id)
          + "/subscriptions/" + encodeURIComponent(sub.id),
        { parallelism },
      );
      onChanged();
    } catch (err) {
      setError(err);
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (sub) => {
    if (!window.confirm(
      sub.config?.kind === "parked_session"
        ? "Cancel this dynamic subscription? The parked session will be unparked."
        : "Delete this subscription? This cannot be undone."
    )) return;
    setBusyId(sub.id);
    setError(null);
    try {
      await apiFetch(
        "DELETE",
        "/triggers/" + encodeURIComponent(trigger.id)
          + "/subscriptions/" + encodeURIComponent(sub.id),
      );
      onChanged();
    } catch (err) {
      setError(err);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="panel">
      <div className="panel-h">
        <Icon name="link" size={13} />
        <span>Subscriptions</span>
        <span className="muted text-sm" style={{ marginLeft: 6 }}>({subs.length})</span>
        <div style={{ marginLeft: "auto" }}>
          <Btn
            size="sm"
            kind="primary"
            icon="plus"
            data-testid="add-subscription-btn"
            onClick={onAdd}
          >
            Add subscription
          </Btn>
        </div>
      </div>
      <div className="panel-body" style={{ padding: 0 }}>
        {error && (
          <div style={{ padding: "8px 14px" }}>
            <Banner
              kind="error"
              title={error.title || "Subscription update failed"}
              detail={error.detail || error.message || ""}
            />
          </div>
        )}
        {subs.length === 0 ? (
          <div className="muted text-sm" style={{ padding: "20px 14px", textAlign: "center" }}>
            No subscriptions yet. Add one to dispatch when this trigger fires.
          </div>
        ) : (
          <table
            className="table"
            data-testid="subscriptions-table"
            style={{ width: "100%", fontSize: 12 }}
          >
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: "6px 12px" }}>Kind</th>
                <th style={{ textAlign: "left", padding: "6px 12px" }}>Target</th>
                <th style={{ textAlign: "left", padding: "6px 12px" }}>Parallelism</th>
                <th style={{ textAlign: "left", padding: "6px 12px" }}>Enabled</th>
                <th style={{ textAlign: "left", padding: "6px 12px" }}>Last fired</th>
                <th style={{ textAlign: "left", padding: "6px 12px" }}>Status</th>
                <th style={{ textAlign: "right", padding: "6px 12px" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {subs.map((sub) => {
                const kind = sub?.config?.kind || "—";
                const isDynamic = kind === "parked_session";
                const rowBusy = busyId === sub.id;
                return (
                  <tr
                    key={sub.id}
                    data-testid={`sub-row-${sub.id}`}
                    style={{ borderTop: "1px solid var(--border)" }}
                  >
                    <td style={{ padding: "8px 12px" }}>
                      <span
                        className="pill pill-paused"
                        title={isDynamic ? "Dynamic subscription created by the yielding tool" : `Subscription kind: ${kind}`}
                        style={{ fontSize: 10.5 }}
                      >
                        {kind}
                      </span>
                    </td>
                    <td style={{ padding: "8px 12px" }}>
                      <TR_SubTargetLabel sub={sub} />
                      {sub.description && (
                        <div className="muted text-sm" style={{ fontSize: 11, marginTop: 2 }}>
                          {sub.description}
                        </div>
                      )}
                    </td>
                    <td style={{ padding: "8px 12px" }}>
                      {isDynamic ? (
                        <span className="muted text-sm">n/a</span>
                      ) : (
                        <select
                          className="input"
                          value={sub.parallelism || "skip"}
                          disabled={rowBusy}
                          onChange={(e) => setParallelism(sub, e.target.value)}
                          style={{ fontSize: 11, padding: "2px 6px" }}
                        >
                          <option value="skip">skip</option>
                          <option value="queue">queue</option>
                        </select>
                      )}
                    </td>
                    <td style={{ padding: "8px 12px" }}>
                      {isDynamic ? (
                        <span className="muted text-sm">n/a</span>
                      ) : (
                        <label style={{ display: "inline-flex", alignItems: "center", gap: 4, cursor: "pointer" }}>
                          <input
                            type="checkbox"
                            checked={!!sub.enabled}
                            disabled={rowBusy}
                            onChange={(e) => setEnabled(sub, e.target.checked)}
                          />
                        </label>
                      )}
                    </td>
                    <td style={{ padding: "8px 12px" }} title={sub.last_fired_at || ""}>
                      <span className="mono">{TR_relTime(sub.last_fired_at)}</span>
                    </td>
                    <td style={{ padding: "8px 12px" }}>
                      {sub.last_fire_error ? (
                        <TR_FireErrorChip
                          error={sub.last_fire_error}
                          testId={`sub-row-${sub.id}-error`}
                        />
                      ) : (
                        <span className="muted text-sm" style={{ fontSize: 11 }}>—</span>
                      )}
                    </td>
                    <td style={{ padding: "8px 12px", textAlign: "right", whiteSpace: "nowrap" }}>
                      {!isDynamic && (
                        <button
                          className="icon-btn"
                          style={{ width: 22, height: 22, marginRight: 4 }}
                          title="Edit subscription"
                          onClick={() => onEdit(sub)}
                          disabled={rowBusy}
                        >
                          <Icon name="edit" size={10} />
                        </button>
                      )}
                      <button
                        className="icon-btn"
                        style={{ width: 22, height: 22 }}
                        title={isDynamic ? "Cancel dynamic subscription" : "Delete subscription"}
                        onClick={() => remove(sub)}
                        disabled={rowBusy}
                      >
                        <Icon name="trash" size={10} />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ============================================================================
// TR_TriggerDetail — real Phase 10.1 implementation.
//
// Layout (Spec §13.4):
//   * Action bar with name + slug + kind + enabled status + Edit / Delete / Back
//   * Metadata panel (slug, name, description, config summary)
//   * Status panel (next_fire_at, last_fired_at, last_fire_error, Fire now)
//   * Subscriptions table (+ Add subscription)
//
// Polling: useResource with pollMs 2000 keeps next_fire_at / last_fired_at
// fresh while operators are watching things land.
// ============================================================================

function TR_TriggerDetail({ id }) {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();

  const detail = useResource(
    "trigger-detail:" + id,
    (signal) => apiFetch("GET", "/triggers/" + encodeURIComponent(id), null, { signal }),
    { pollMs: 2000, deps: [id] },
  );

  const subs = useResource(
    "trigger-subs:" + id,
    (signal) => apiFetch(
      "GET",
      "/triggers/" + encodeURIComponent(id) + "/subscriptions",
      null,
      { signal },
    ),
    { pollMs: 2000, deps: [id] },
  );

  const [editOpen, setEditOpen] = React.useState(false);
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [deleteBusy, setDeleteBusy] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState(null);
  const [fireBusy, setFireBusy] = React.useState(false);
  const [fireError, setFireError] = React.useState(null);
  const [fireResult, setFireResult] = React.useState(null);
  const [subDialog, setSubDialog] = React.useState(null); // {mode:"create"|"edit", sub?}
  // Webhook-specific state
  const [rotateBusy, setRotateBusy] = React.useState(false);
  const [rotateError, setRotateError] = React.useState(null);
  const [hmacDialogOpen, setHmacDialogOpen] = React.useState(false);

  const refetchAll = React.useCallback(() => {
    detail.refetch();
    subs.refetch();
  }, [detail, subs]);

  const fireNow = async () => {
    setFireBusy(true);
    setFireError(null);
    setFireResult(null);
    try {
      const res = await apiFetch(
        "POST",
        "/triggers/" + encodeURIComponent(id) + "/fire_now",
        {},
      );
      setFireResult(res);
      refetchAll();
    } catch (err) {
      const env = err && err.envelope;
      const envDetail = env && env.detail;
      let code = null;
      let msg = null;
      if (envDetail && typeof envDetail === "object") {
        code = envDetail.code || null;
        msg = envDetail.message || null;
      }
      if (!msg) msg = err.title || err.message || "Fire failed";
      setFireError({ code, message: msg });
    } finally {
      setFireBusy(false);
    }
  };

  const doDelete = async () => {
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await apiFetch("DELETE", "/triggers/" + encodeURIComponent(id));
      navigate("/triggers");
    } catch (err) {
      setDeleteError(err);
    } finally {
      setDeleteBusy(false);
    }
  };

  const rotateToken = async () => {
    if (!window.confirm("Rotate the webhook token? The old URL will stop working immediately.")) return;
    setRotateBusy(true);
    setRotateError(null);
    try {
      await apiFetch("POST", "/triggers/" + encodeURIComponent(id) + "/rotate_token", {});
      refetchAll();
    } catch (err) {
      const msg = (err && (err.message || err.title)) || "Rotate failed";
      setRotateError(msg);
    } finally {
      setRotateBusy(false);
    }
  };

  if (detail.loading && !detail.data) {
    return (
      <div className="col" style={{ gap: 14 }}>
        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("/triggers")}>Back</Btn>
        </div>
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
      </div>
    );
  }
  if (detail.error && !detail.data) {
    return (
      <div className="col" style={{ gap: 14 }}>
        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("/triggers")}>Back</Btn>
        </div>
        <Banner
          kind="error"
          title={detail.error.title || "Couldn't load trigger"}
          detail={detail.error.detail || detail.error.message}
          actions={<Btn size="sm" icon="chevron-left" onClick={() => navigate("/triggers")}>Back to list</Btn>}
        />
      </div>
    );
  }

  const t = detail.data;
  const kind = t?.config?.kind || "—";
  const subItems = subs.data?.items ?? [];

  return (
    <div className="col" data-testid="trigger-detail" style={{ gap: 14 }}>
      {/* Action bar */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, justifyContent: "space-between", flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ fontWeight: 600, fontSize: 14 }}>{t.name || t.slug}</span>
          <span className="mono muted text-sm">{t.slug}</span>
          <span
            className="pill pill-paused"
            title={`Trigger kind: ${kind}`}
            style={{ fontSize: 10.5 }}
          >
            {kind}
          </span>
          <span
            className={`pill ${t.enabled ? "pill-claimed" : "pill-ended"}`}
            title={t.enabled ? "Trigger is enabled" : "Trigger is disabled"}
            style={{ fontSize: 10.5 }}
          >
            {t.enabled ? "enabled" : "disabled"}
          </span>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="edit" onClick={() => setEditOpen(true)}>Edit</Btn>
          <Btn
            size="sm"
            kind="danger"
            icon="trash"
            onClick={() => setConfirmDelete(true)}
          >
            Delete trigger
          </Btn>
          <Btn size="sm" kind="ghost" icon="chevron-left" onClick={() => navigate("/triggers")}>Back</Btn>
        </div>
      </div>

      {/* Metadata panel */}
      <div className="panel">
        <div className="panel-h"><Icon name="info" size={13} /><span>Metadata</span></div>
        <div className="panel-body" style={{ padding: "8px 14px" }}>
          <dl className="kv" style={{ gridTemplateColumns: "160px 1fr", rowGap: 4 }}>
            <dt>Slug</dt>
            <dd className="mono">{t.slug}</dd>
            <dt>Name</dt>
            <dd>{t.name || <span className="muted">—</span>}</dd>
            <dt>Kind</dt>
            <dd className="mono">{kind}</dd>
            {kind === "delayed" && t.config?.fire_at && (
              <>
                <dt>Fire at</dt>
                <dd className="mono" title={t.config.fire_at}>{t.config.fire_at}</dd>
              </>
            )}
            {kind === "scheduled" && (
              <>
                <dt>Cron</dt>
                <dd className="mono">{t.config?.cron || "—"}</dd>
                <dt>Timezone</dt>
                <dd className="mono">{t.config?.timezone || "—"}</dd>
                <dt>Catchup</dt>
                <dd className="mono">{t.config?.catchup || "—"}</dd>
              </>
            )}
            {kind === "webhook" && (() => {
              const whUrl = TR_webhookUrl(t);
              const hasHmac = !!t.config?.hmac_secret;
              return (
                <>
                  <dt>Webhook URL</dt>
                  <dd style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <span className="mono" style={{ wordBreak: "break-all", fontSize: 11 }} data-testid="webhook-url">
                      {whUrl || "(loading…)"}
                    </span>
                    {whUrl && <TR_CopyButton text={whUrl} label="Copy URL" testId="copy-webhook-url-btn" />}
                  </dd>
                  <dt>HMAC secret</dt>
                  <dd style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span className={hasHmac ? "pill pill-claimed" : "muted text-sm"} style={{ fontSize: 10.5 }}>
                      {hasHmac ? "configured" : "not set"}
                    </span>
                    <Btn
                      size="sm"
                      kind="ghost"
                      icon="edit"
                      onClick={() => setHmacDialogOpen(true)}
                      data-testid="set-hmac-btn"
                    >
                      {hasHmac ? "Update" : "Set"}
                    </Btn>
                    {hasHmac && (
                      <Btn
                        size="sm"
                        kind="ghost"
                        icon="trash"
                        onClick={async () => {
                          if (!window.confirm("Clear the HMAC secret? Requests will no longer be verified.")) return;
                          try {
                            await apiFetch(
                              "PUT",
                              "/triggers/" + encodeURIComponent(id),
                              { config: { kind: "webhook", hmac_secret: null } },
                            );
                            refetchAll();
                          } catch (_e) { /* ignore */ }
                        }}
                        data-testid="clear-hmac-btn"
                      >
                        Clear
                      </Btn>
                    )}
                  </dd>
                  <dt>Token</dt>
                  <dd style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <Btn
                      size="sm"
                      kind="ghost"
                      icon="refresh"
                      onClick={rotateToken}
                      disabled={rotateBusy}
                      data-testid="rotate-token-btn"
                    >
                      {rotateBusy ? "Rotating…" : "Rotate token"}
                    </Btn>
                    {rotateError && (
                      <span className="muted text-sm" style={{ color: "var(--red)" }}>{rotateError}</span>
                    )}
                  </dd>
                </>
              );
            })()}
            {t.description && (
              <>
                <dt>Description</dt>
                <dd>{t.description}</dd>
              </>
            )}
          </dl>
        </div>
      </div>

      {/* HMAC secret dialog (webhook only) */}
      {hmacDialogOpen && kind === "webhook" && (
        <TR_HmacSecretDialog
          triggerId={id}
          onClose={() => setHmacDialogOpen(false)}
          onSaved={() => { setHmacDialogOpen(false); refetchAll(); }}
        />
      )}

      {/* Status panel */}
      <div className="panel" data-testid="trigger-status-panel">
        <div className="panel-h">
          <Icon name="clock" size={13} />
          <span>Status</span>
          <div style={{ marginLeft: "auto" }}>
            <Btn
              size="sm"
              kind="primary"
              icon="zap"
              data-testid="fire-now-btn"
              onClick={fireNow}
              disabled={fireBusy}
              title="Fire this trigger immediately, bypassing the schedule"
            >
              {fireBusy ? "Firing…" : "Fire now"}
            </Btn>
          </div>
        </div>
        <div className="panel-body" style={{ padding: "8px 14px" }}>
          <dl className="kv" style={{ gridTemplateColumns: "160px 1fr", rowGap: 4 }}>
            <dt>Next fire</dt>
            <dd>
              <span className="mono" title={t.next_fire_at || ""}>
                {TR_relTime(t.next_fire_at)}
              </span>
              {t.next_fire_at && (
                <span className="muted text-sm" style={{ marginLeft: 8 }}>
                  ({t.next_fire_at})
                </span>
              )}
            </dd>
            <dt>Last fired</dt>
            <dd>
              <span className="mono" title={t.last_fired_at || ""}>
                {TR_relTime(t.last_fired_at)}
              </span>
              {t.last_fired_at && (
                <span className="muted text-sm" style={{ marginLeft: 8 }}>
                  ({t.last_fired_at})
                </span>
              )}
            </dd>
            {t.last_fire_error && (
              <>
                <dt>Last error</dt>
                <dd>
                  <TR_FireErrorChip
                    error={t.last_fire_error}
                    testId="trigger-last-fire-error"
                  />
                </dd>
              </>
            )}
          </dl>
          {fireError && (
            <div style={{ marginTop: 8 }}>
              <Banner
                kind="error"
                title={fireError.code ? `Fire failed (${fireError.code})` : "Fire failed"}
                detail={fireError.message || ""}
              />
            </div>
          )}
          {fireResult && (
            <div style={{ marginTop: 8 }} className="muted text-sm">
              Fired{fireResult.fire_id ? <> · fire id <span className="mono">{fireResult.fire_id}</span></> : null}
              {Array.isArray(fireResult.results) && fireResult.results.length > 0 && (
                <> · {fireResult.results.length} {fireResult.results.length === 1 ? "subscription" : "subscriptions"} dispatched</>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Subscriptions */}
      <TR_SubscriptionsPanel
        trigger={t}
        subs={subItems}
        onChanged={refetchAll}
        onAdd={() => setSubDialog({ mode: "create" })}
        onEdit={(sub) => setSubDialog({ mode: "edit", sub })}
      />

      {/* Edit dialog */}
      {editOpen && (
        <TR_TriggerEditDialog
          trigger={t}
          onClose={() => setEditOpen(false)}
          onSaved={() => { setEditOpen(false); refetchAll(); }}
        />
      )}

      {/* Subscription create/edit dialog */}
      {subDialog && window.TR_SubscriptionDialog && (
        <window.TR_SubscriptionDialog
          triggerId={t.id}
          mode={subDialog.mode}
          initial={subDialog.sub}
          onClose={() => setSubDialog(null)}
          onSaved={() => { setSubDialog(null); refetchAll(); }}
        />
      )}

      {/* Confirm delete */}
      {confirmDelete && (
        <Modal
          title={`Delete trigger ${t.name || t.slug}?`}
          danger
          onClose={() => setConfirmDelete(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setConfirmDelete(false)} disabled={deleteBusy}>Cancel</Btn>
              <Btn
                kind="danger"
                icon="trash"
                disabled={deleteBusy}
                onClick={doDelete}
              >
                {deleteBusy ? "Deleting…" : "Delete trigger"}
              </Btn>
            </>
          }
        >
          <ul>
            <li>This deletes the trigger and cascades to all its subscriptions.</li>
            <li>In-flight fires complete; future fires are cancelled.</li>
            <li>This action cannot be undone.</li>
          </ul>
          {deleteError && (
            <Banner
              kind="error"
              title={deleteError.title || "Delete failed"}
              detail={deleteError.detail || deleteError.message || ""}
            />
          )}
        </Modal>
      )}
    </div>
  );
}

// ============================================================================
// TR_SubscriptionDialog — create / edit a subscription (Phase 10.2).
//
// Spec §13.5. Three creatable kinds:
//   * chat_message            — chat picker
//   * agent_fresh_session     — workspace + agent pickers
//   * graph_fresh_session     — workspace + graph pickers
//
// parked_session is intentionally EXCLUDED — it is created only by the
// subscribe_to_trigger yielding tool (see Spec §5.4 and the
// `parked_session_only_from_yield` error code §14). The dialog enforces
// this client-side by omitting it from the kind picker; the server is
// the source of truth.
//
// Common fields: payload_template (Jinja2), parallelism (skip|queue),
// description, enabled. Edit mode pre-populates everything from `initial`
// and locks the kind + config (config is immutable on PUT — only
// payload_template, parallelism, enabled, description are sent).
// ============================================================================

const TR_SUB_KIND_OPTIONS = [
  { value: "chat_message", label: "chat_message", description: "Append a user message to an existing chat." },
  { value: "agent_fresh_session", label: "agent_fresh_session", description: "Start a fresh workspace session bound to an agent." },
  { value: "graph_fresh_session", label: "graph_fresh_session", description: "Start a fresh workspace session bound to a graph." },
];

// Help text shown beneath the payload_template textarea. Echoes Spec §3.3.
const TR_SUB_FIRE_CONTEXT_HELP = (
  "Fire context variables available: "
  + "{{ trigger_id }}, {{ trigger_slug }}, {{ kind }}, "
  + "{{ fired_at }}, {{ scheduled_for }}, {{ fire_id }}"
);

function TR_SubscriptionDialog({ triggerId, mode, initial, onClose, onSaved }) {
  const { apiFetch, useResource } = window.primerApi;
  const isEdit = mode === "edit" && initial != null;

  // Lock the kind in edit mode; default to chat_message in create mode.
  const initialKind = isEdit ? (initial?.config?.kind || "chat_message") : "chat_message";
  const [kind, setKind] = React.useState(initialKind);

  // Per-kind config state.
  const [chatId, setChatId] = React.useState(
    isEdit && initial?.config?.kind === "chat_message" ? (initial.config.chat_id || "") : "",
  );
  const [workspaceId, setWorkspaceId] = React.useState(
    isEdit && initial?.config?.workspace_id ? initial.config.workspace_id : "",
  );
  const [agentId, setAgentId] = React.useState(
    isEdit && initial?.config?.kind === "agent_fresh_session" ? (initial.config.agent_id || "") : "",
  );
  const [graphId, setGraphId] = React.useState(
    isEdit && initial?.config?.kind === "graph_fresh_session" ? (initial.config.graph_id || "") : "",
  );

  // Common fields.
  const [payloadTemplate, setPayloadTemplate] = React.useState(
    isEdit && initial?.payload_template != null ? initial.payload_template : "",
  );
  const [parallelism, setParallelism] = React.useState(
    isEdit && initial?.parallelism ? initial.parallelism : "skip",
  );
  const [description, setDescription] = React.useState(
    isEdit && initial?.description ? initial.description : "",
  );
  const [enabled, setEnabled] = React.useState(
    isEdit ? !!initial?.enabled : true,
  );

  // Submit state.
  const [submitError, setSubmitError] = React.useState(null);
  const [busy, setBusy] = React.useState(false);

  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // Picker data (always queried so hook count stays fixed; the form
  // only renders the relevant picker for the selected kind). We use the
  // standard `?limit=200` pattern adopted by the chats / agents / graphs
  // pages so the dropdown isn't capped to a default page size.
  const chats = useResource(
    "tr-sub-chats",
    (signal) => apiFetch("GET", "/chats?limit=200", null, { signal }),
    { pollMs: null },
  );
  const workspaces = useResource(
    "tr-sub-workspaces",
    (signal) => apiFetch("GET", "/workspaces?limit=200", null, { signal }),
    { pollMs: null },
  );
  const agents = useResource(
    "tr-sub-agents",
    (signal) => apiFetch("GET", "/agents?limit=200", null, { signal }),
    { pollMs: null },
  );
  const graphs = useResource(
    "tr-sub-graphs",
    (signal) => apiFetch("GET", "/graphs?limit=200", null, { signal }),
    { pollMs: null },
  );

  const chatItems = chats.data?.items ?? [];
  const workspaceItems = workspaces.data?.items ?? [];
  // Filter agents/graphs by workspace if the workspace picker is set.
  // Many agent/graph rows carry a workspace_id; if not, show the full list.
  const agentItems = (agents.data?.items ?? []).filter((a) => (
    !workspaceId || !a.workspace_id || a.workspace_id === workspaceId
  ));
  const graphItems = (graphs.data?.items ?? []).filter((g) => (
    !workspaceId || !g.workspace_id || g.workspace_id === workspaceId
  ));

  // Validation gates.
  const configValid = (
    kind === "chat_message" ? !!chatId
      : kind === "agent_fresh_session" ? (!!workspaceId && !!agentId)
        : kind === "graph_fresh_session" ? (!!workspaceId && !!graphId)
          : false
  );
  const canSubmit = isEdit ? !busy : (!busy && configValid);

  const buildConfig = () => {
    if (kind === "chat_message") {
      return { kind: "chat_message", chat_id: chatId };
    }
    if (kind === "agent_fresh_session") {
      return { kind: "agent_fresh_session", workspace_id: workspaceId, agent_id: agentId };
    }
    return { kind: "graph_fresh_session", workspace_id: workspaceId, graph_id: graphId };
  };

  const submit = async () => {
    setSubmitError(null);
    setBusy(true);
    try {
      const tpl = payloadTemplate.trim() === "" ? null : payloadTemplate;
      const desc = description.trim() === "" ? null : description;
      let sub;
      if (isEdit) {
        // config is immutable per Spec §13.5 — only mutable fields sent.
        const body = {
          payload_template: tpl,
          parallelism,
          enabled,
          description: desc,
        };
        sub = await apiFetch(
          "PUT",
          "/triggers/" + encodeURIComponent(triggerId)
            + "/subscriptions/" + encodeURIComponent(initial.id),
          body,
        );
      } else {
        const body = {
          config: buildConfig(),
          payload_template: tpl,
          parallelism,
          description: desc,
          enabled,
        };
        sub = await apiFetch(
          "POST",
          "/triggers/" + encodeURIComponent(triggerId) + "/subscriptions",
          body,
        );
      }
      if (!mountedRef.current) return;
      onSaved(sub);
    } catch (err) {
      if (!mountedRef.current) return;
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

  return (
    <Modal
      title={isEdit ? `Edit subscription · ${initial.id}` : "Add subscription"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn
            kind="primary"
            icon={isEdit ? "check" : "plus"}
            onClick={submit}
            disabled={!canSubmit}
          >
            {busy
              ? (isEdit ? "Saving…" : "Adding…")
              : (isEdit ? "Save changes" : "Add subscription")}
          </Btn>
        </>
      }
    >
      <div data-testid="tr-sub-dialog">
        {/* Kind picker — create mode only; locked in edit mode. */}
        <div className="field">
          <label className="field-label">
            Kind
            {isEdit && <span className="hint"> locked — config is immutable</span>}
          </label>
          {isEdit ? (
            <div className="mono" style={{ padding: "6px 8px", border: "1px solid var(--border)", borderRadius: 4, background: "var(--surface-1)" }}>
              {kind}
            </div>
          ) : (
            <div data-testid="tr-sub-kind-picker">
              {TR_SUB_KIND_OPTIONS.map((opt) => (
                <label
                  key={opt.value}
                  className="row"
                  style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "6px 0", cursor: "pointer" }}
                >
                  <input
                    type="radio"
                    name="tr-sub-kind"
                    value={opt.value}
                    checked={kind === opt.value}
                    onChange={() => setKind(opt.value)}
                  />
                  <div>
                    <div style={{ fontWeight: 600 }}>{opt.label}</div>
                    <div className="muted text-sm">{opt.description}</div>
                  </div>
                </label>
              ))}
              <div className="field-help muted text-sm" style={{ marginTop: 4 }}>
                Note: <span className="mono">parked_session</span> subscriptions are created
                only by the <span className="mono">subscribe_to_trigger</span> yielding tool
                and cannot be added here.
              </div>
            </div>
          )}
        </div>

        {/* Per-kind config */}
        {kind === "chat_message" && !isEdit && (
          <div className="field">
            <label className="field-label" htmlFor="tr-sub-chat">Chat</label>
            <select
              id="tr-sub-chat"
              className="input mono"
              value={chatId}
              onChange={(e) => setChatId(e.target.value)}
              style={{ width: "100%" }}
            >
              <option value="">Select a chat…</option>
              {chatItems.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.title ? `${c.title} · ${c.id}` : c.id}
                </option>
              ))}
            </select>
            {chats.loading && (
              <div className="field-help muted text-sm">Loading chats…</div>
            )}
          </div>
        )}

        {(kind === "agent_fresh_session" || kind === "graph_fresh_session") && !isEdit && (
          <div className="field">
            <label className="field-label" htmlFor="tr-sub-workspace">Workspace</label>
            <select
              id="tr-sub-workspace"
              className="input mono"
              value={workspaceId}
              onChange={(e) => {
                setWorkspaceId(e.target.value);
                // Reset agent/graph when workspace changes.
                setAgentId("");
                setGraphId("");
              }}
              style={{ width: "100%" }}
            >
              <option value="">Select a workspace…</option>
              {workspaceItems.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name ? `${w.name} · ${w.id}` : w.id}
                </option>
              ))}
            </select>
            {workspaces.loading && (
              <div className="field-help muted text-sm">Loading workspaces…</div>
            )}
          </div>
        )}

        {kind === "agent_fresh_session" && !isEdit && (
          <div className="field">
            <label className="field-label" htmlFor="tr-sub-agent">Agent</label>
            <select
              id="tr-sub-agent"
              className="input mono"
              value={agentId}
              onChange={(e) => setAgentId(e.target.value)}
              disabled={!workspaceId}
              style={{ width: "100%" }}
            >
              <option value="">Select an agent…</option>
              {agentItems.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name ? `${a.name} · ${a.id}` : a.id}
                </option>
              ))}
            </select>
            {agents.loading && (
              <div className="field-help muted text-sm">Loading agents…</div>
            )}
          </div>
        )}

        {kind === "graph_fresh_session" && !isEdit && (
          <div className="field">
            <label className="field-label" htmlFor="tr-sub-graph">Graph</label>
            <select
              id="tr-sub-graph"
              className="input mono"
              value={graphId}
              onChange={(e) => setGraphId(e.target.value)}
              disabled={!workspaceId}
              style={{ width: "100%" }}
            >
              <option value="">Select a graph…</option>
              {graphItems.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name ? `${g.name} · ${g.id}` : g.id}
                </option>
              ))}
            </select>
            {graphs.loading && (
              <div className="field-help muted text-sm">Loading graphs…</div>
            )}
            <div className="field-help muted text-sm" style={{ marginTop: 4 }}>
              The rendered payload must be JSON that validates against the
              graph&apos;s Begin <span className="mono">input_schema</span>.
            </div>
          </div>
        )}

        {/* In edit mode, display the locked config summary in lieu of pickers. */}
        {isEdit && (
          <div className="field">
            <label className="field-label">Target <span className="hint">locked</span></label>
            <div className="mono" style={{ padding: "6px 8px", border: "1px solid var(--border)", borderRadius: 4, background: "var(--surface-1)" }}>
              <TR_SubTargetLabel sub={initial} />
            </div>
          </div>
        )}

        {/* Payload template */}
        <div className="field">
          <label className="field-label" htmlFor="tr-sub-payload-template">
            Payload template <span className="hint">Jinja2 · optional</span>
          </label>
          <textarea
            id="tr-sub-payload-template"
            name="payload_template"
            className="input mono"
            value={payloadTemplate}
            onChange={(e) => setPayloadTemplate(e.target.value)}
            rows={5}
            placeholder={kind === "graph_fresh_session"
              ? '{"task": "Fired at {{ fired_at }}"}'
              : 'Heads up — trigger {{ trigger_slug }} fired at {{ fired_at }}.'}
            style={{ width: "100%", resize: "vertical" }}
          />
          <div className="field-help muted text-sm" style={{ marginTop: 4 }}>
            {TR_SUB_FIRE_CONTEXT_HELP}
          </div>
        </div>

        {/* Parallelism */}
        <div className="field">
          <label className="field-label">Parallelism</label>
          <div data-testid="tr-sub-parallelism" style={{ display: "flex", gap: 12 }}>
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
              <input
                type="radio"
                name="tr-sub-parallelism"
                value="skip"
                checked={parallelism === "skip"}
                onChange={() => setParallelism("skip")}
              />
              <span><span className="mono">skip</span> — no-op if the previous fire&apos;s unit is still in-flight</span>
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
              <input
                type="radio"
                name="tr-sub-parallelism"
                value="queue"
                checked={parallelism === "queue"}
                onChange={() => setParallelism("queue")}
              />
              <span><span className="mono">queue</span> — always fire</span>
            </label>
          </div>
        </div>

        {/* Description */}
        <div className="field">
          <label className="field-label" htmlFor="tr-sub-description">
            Description <span className="hint">optional</span>
          </label>
          <textarea
            id="tr-sub-description"
            className="input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            style={{ width: "100%", resize: "vertical" }}
          />
        </div>

        {/* Enabled */}
        <div className="field">
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            <span>Enabled</span>
          </label>
          <div className="field-help muted text-sm" style={{ marginTop: 4 }}>
            Disabled subscriptions are skipped when the trigger fires.
          </div>
        </div>

        {submitError && (
          <Banner
            kind="error"
            title={submitError.code ? `${isEdit ? "Save" : "Create"} failed (${submitError.code})` : `${isEdit ? "Save" : "Create"} failed`}
            detail={submitError.message || ""}
          />
        )}
      </div>
    </Modal>
  );
}

// ============================================================================
// TR_HmacSecretDialog - set or update the HMAC secret on a webhook trigger.
//
// The server stores the secret verbatim (inside the config JSONB). This
// dialog lets the operator enter a new secret and PUT it via the standard
// trigger update route.
// ============================================================================

function TR_HmacSecretDialog({ triggerId, onClose, onSaved }) {
  const { apiFetch } = window.primerApi;
  const [secret, setSecret] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);

  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const submit = async () => {
    if (!secret.trim()) { setError({ message: "Secret cannot be empty" }); return; }
    setBusy(true);
    setError(null);
    try {
      await apiFetch(
        "PUT",
        "/triggers/" + encodeURIComponent(triggerId),
        { config: { kind: "webhook", hmac_secret: secret } },
      );
      if (!mountedRef.current) return;
      onSaved();
    } catch (err) {
      if (!mountedRef.current) return;
      const msg = (err && (err.message || err.title)) || "Save failed";
      setError({ message: msg });
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };

  return (
    <Modal
      title="Set HMAC secret"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn kind="primary" icon="check" onClick={submit} disabled={busy || !secret.trim()}>
            {busy ? "Saving…" : "Save secret"}
          </Btn>
        </>
      }
    >
      <div data-testid="tr-hmac-dialog">
        <div className="field-help muted text-sm" style={{ marginBottom: 10 }}>
          When set, every inbound webhook request must include a
          <span className="mono"> X-Primer-Signature: sha256=&lt;hex&gt;</span> header
          computed as HMAC-SHA256 over the raw request body using this secret.
          Requests without a valid signature are rejected 401.
        </div>
        <div className="field">
          <label className="field-label" htmlFor="tr-hmac-secret">HMAC secret</label>
          <input
            id="tr-hmac-secret"
            type="password"
            className="input mono"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            placeholder="Enter a strong secret…"
            style={{ width: "100%" }}
            autoComplete="new-password"
            data-testid="tr-hmac-secret-input"
          />
          <div className="field-help muted text-sm" style={{ marginTop: 4 }}>
            Use a long random string. This value is stored server-side and
            never returned in API responses after creation.
          </div>
        </div>
        {error && (
          <Banner kind="error" title="Save failed" detail={error.message || ""} />
        )}
      </div>
    </Modal>
  );
}

// ============================================================================
// Exports
// ============================================================================

window.TR_TriggersPage = TR_TriggersPage;
window.TR_TriggerList = TR_TriggerList;
window.TR_TriggerRow = TR_TriggerRow;
window.TR_TriggerDetail = TR_TriggerDetail;
window.TR_TriggerEditDialog = TR_TriggerEditDialog;
window.TR_SubscriptionsPanel = TR_SubscriptionsPanel;
window.TR_SubscriptionDialog = TR_SubscriptionDialog;
window.TR_CreateTriggerDialog = TR_CreateTriggerDialog;
window.TR_HmacSecretDialog = TR_HmacSecretDialog;
