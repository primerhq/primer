/* global React, Icon, Btn, Modal, Banner */
// API tokens console page (Spec §8).
//
// Prefix AT_ to avoid global name collisions with other components.
//
// Reads/writes the /v1/auth/tokens CRUD endpoints shipped in commit
// d7e6755. POST /v1/auth/tokens is the ONLY place plaintext appears —
// the create flow surfaces a "copy this now, you won't see it again"
// one-time view dialog before refreshing the list.

// ============================================================================
// Constants
// ============================================================================

// Scopes are validated server-side against a known allowlist (Spec §6).
// v1 ships with a single scope; the checkbox list is intentionally tiny.
const AT_SCOPE_OPTIONS = [
  {
    value: "mcp",
    label: "mcp",
    description: "Allow the token to call the MCP bridge endpoints.",
  },
];

// ============================================================================
// Helpers
// ============================================================================

// Relative-time formatter — "in 2h" / "5m ago" / "—" for null. Matches
// the formatter used by triggers.jsx so the look is consistent.
function AT_relTime(iso) {
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

// Status derivation (Spec §8):
//   * revoked_at set         -> "revoked"
//   * expires_at in past     -> "expired"
//   * otherwise              -> "active"
function AT_statusOf(token) {
  if (token.revoked_at) return "revoked";
  if (token.expires_at) {
    const t = Date.parse(token.expires_at);
    if (Number.isFinite(t) && t <= Date.now()) return "expired";
  }
  return "active";
}

// Extract a {code, message} from an ApiError envelope. The server wraps
// every 4xx with {detail: {code, message}} (see _raise_code). FastAPI
// unwraps `detail` to the envelope; ApiError stores it on `.detail` or
// `.envelope.detail`. Mirrors the parser used by triggers.jsx.
function AT_extractError(err) {
  const env = err && err.envelope;
  const envDetail = env && env.detail;
  let code = null;
  let msg = null;
  if (envDetail && typeof envDetail === "object") {
    code = envDetail.code || null;
    msg = envDetail.message || null;
  }
  if (!msg && typeof err.detail === "string") msg = err.detail;
  if (!msg) msg = (err && (err.title || err.message)) || "Request failed";
  return { code, message: msg };
}

// ============================================================================
// AT_ApiTokensPage — top-level list view + create entry point
// ============================================================================

function AT_ApiTokensPage() {
  const { useResource, apiFetch } = window.primerApi;
  const [createOpen, setCreateOpen] = React.useState(false);
  const [confirmRevoke, setConfirmRevoke] = React.useState(null); // token | null

  const list = useResource(
    "api-tokens:list",
    (signal) => apiFetch("GET", "/auth/tokens", null, { signal }),
    { pollMs: 10000 },
  );

  const items = list.data?.items ?? [];

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <span style={{ fontSize: 13, fontWeight: 600 }}>API tokens</span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
          <Btn
            size="sm"
            kind="primary"
            icon="plus"
            data-testid="create-token-btn"
            onClick={() => setCreateOpen(true)}
          >
            Create token
          </Btn>
        </div>
      </div>

      {list.loading && items.length === 0 && (
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
      )}
      {list.error && items.length === 0 && (
        <Banner
          kind="error"
          title={list.error.title || "Couldn't load tokens"}
          detail={list.error.detail || list.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
        />
      )}
      {!list.loading && !list.error && items.length === 0 && (
        <div className="empty" style={{ padding: "40px 20px" }}>
          <div className="ico-wrap"><Icon name="key" size={22} /></div>
          <div className="head">No API tokens yet</div>
          <div className="sub">
            Create a token to authenticate programmatic clients (e.g. the
            MCP bridge) without a browser session.
          </div>
          <div className="actions">
            <Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>Create token</Btn>
          </div>
        </div>
      )}

      {items.length > 0 && (
        <div
          data-testid="api-tokens-table"
          className="panel"
          style={{ padding: 0, overflow: "hidden" }}
        >
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Name</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Prefix</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Scopes</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Last used</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Expires</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Status</th>
                <th style={{ textAlign: "right", padding: "8px 12px" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((t) => (
                <AT_TokenRow
                  key={t.id}
                  token={t}
                  onRevoke={() => setConfirmRevoke(t)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {createOpen && (
        <AT_CreateTokenDialog
          onClose={() => setCreateOpen(false)}
          onCreated={() => {
            // The create dialog handles the one-time-view modal internally.
            // The list refresh happens after THAT modal closes — see the
            // dialog's onDone callback below.
          }}
          onDone={() => {
            setCreateOpen(false);
            list.refetch();
          }}
        />
      )}

      {confirmRevoke && (
        <AT_RevokeConfirmDialog
          token={confirmRevoke}
          onClose={() => setConfirmRevoke(null)}
          onRevoked={() => {
            setConfirmRevoke(null);
            list.refetch();
          }}
        />
      )}
    </div>
  );
}

// ============================================================================
// AT_TokenRow — one row of the table.
// ============================================================================

function AT_TokenRow({ token, onRevoke }) {
  const status = AT_statusOf(token);
  // Color mapping (Spec §8):
  //   active  -> green  (pill-claimed)
  //   revoked -> red    (pill-failed)
  //   expired -> grey   (pill-ended)
  const statusClass = status === "active"
    ? "pill-claimed"
    : status === "revoked"
      ? "pill-failed"
      : "pill-ended";
  const isRevoked = status === "revoked";
  return (
    <tr
      data-testid={`api-token-row-${token.id}`}
      style={{ borderTop: "1px solid var(--border)" }}
    >
      <td style={{ padding: "8px 12px", fontWeight: 600 }}>{token.name}</td>
      <td style={{ padding: "8px 12px" }}>
        <span className="mono" style={{ fontSize: 11 }}>{token.prefix}…</span>
      </td>
      <td style={{ padding: "8px 12px" }}>
        {Array.isArray(token.scopes) && token.scopes.length > 0 ? (
          <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
            {token.scopes.map((s) => (
              <span
                key={s}
                className="pill pill-paused"
                style={{ fontSize: 10.5 }}
              >
                {s}
              </span>
            ))}
          </div>
        ) : (
          <span className="muted text-sm">—</span>
        )}
      </td>
      <td style={{ padding: "8px 12px" }} title={token.last_used_at || ""}>
        <span className="mono">{AT_relTime(token.last_used_at)}</span>
      </td>
      <td style={{ padding: "8px 12px" }} title={token.expires_at || ""}>
        {token.expires_at
          ? <span className="mono">{AT_relTime(token.expires_at)}</span>
          : <span className="muted">never</span>}
      </td>
      <td style={{ padding: "8px 12px" }}>
        <span className={`pill ${statusClass}`} style={{ fontSize: 10.5 }}>
          {status}
        </span>
      </td>
      <td style={{ padding: "8px 12px", textAlign: "right", whiteSpace: "nowrap" }}>
        <Btn
          size="sm"
          kind="danger"
          icon="trash"
          disabled={isRevoked}
          onClick={onRevoke}
          title={isRevoked ? "Token already revoked" : "Revoke this token"}
          data-testid={`revoke-token-btn-${token.id}`}
        >
          Revoke
        </Btn>
      </td>
    </tr>
  );
}

// ============================================================================
// AT_CreateTokenDialog — form → POST /v1/auth/tokens → one-time view dialog
// ============================================================================

function AT_CreateTokenDialog({ onClose, onCreated, onDone }) {
  const { apiFetch } = window.primerApi;

  const [name, setName] = React.useState("");
  const [scopes, setScopes] = React.useState([]); // selected scope values
  // expires_at uses a datetime-local input (browser local time, no
  // offset). We convert to a UTC ISO instant at submit. Empty string
  // means "never expires" — the server treats null as no expiry.
  const [expiresAtLocal, setExpiresAtLocal] = React.useState("");

  const [busy, setBusy] = React.useState(false);
  const [submitError, setSubmitError] = React.useState(null); // {code, message}
  // When the POST succeeds we flip to the one-time-view step; the
  // parent stays open so closing the inner modal triggers a list refresh.
  const [created, setCreated] = React.useState(null);

  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const toggleScope = (value) => {
    setScopes((prev) => prev.includes(value)
      ? prev.filter((s) => s !== value)
      : [...prev, value]);
  };

  const canSubmit = !busy && !!name.trim();

  const submit = async () => {
    setSubmitError(null);
    setBusy(true);
    try {
      let expiresIso = null;
      if (expiresAtLocal) {
        // datetime-local has no timezone — interpret as the browser's
        // local time, emit UTC ISO so the server stores a tz-aware ts.
        const dt = new Date(expiresAtLocal);
        if (!isNaN(dt.getTime())) {
          expiresIso = dt.toISOString();
        }
      }
      const body = {
        name: name.trim(),
        scopes,
        expires_at: expiresIso,
      };
      const res = await apiFetch("POST", "/auth/tokens", body);
      if (!mountedRef.current) return;
      setCreated(res);
      onCreated && onCreated(res);
    } catch (err) {
      if (!mountedRef.current) return;
      setSubmitError(AT_extractError(err));
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };

  // Once we have the plaintext response, render the one-time-view dialog
  // instead of the form. Closing that dialog refreshes the list via onDone.
  if (created) {
    return (
      <AT_PlaintextOneTimeDialog
        token={created}
        onClose={() => onDone && onDone()}
      />
    );
  }

  return (
    <Modal
      title="Create API token"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn
            kind="primary"
            icon="check"
            onClick={submit}
            disabled={!canSubmit}
            data-testid="create-token-submit"
          >
            {busy ? "Creating…" : "Create token"}
          </Btn>
        </>
      }
    >
      <div data-testid="at-create-form">
        <div className="field">
          <label className="field-label" htmlFor="at-name">Name</label>
          <input
            id="at-name"
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. mcp-bridge-prod"
            style={{ width: "100%" }}
            autoFocus
          />
          <div className="field-help muted text-sm" style={{ marginTop: 4 }}>
            Must be unique among your tokens. ≤ 128 chars.
          </div>
        </div>

        <div className="field">
          <label className="field-label">Scopes</label>
          <div data-testid="at-scopes">
            {AT_SCOPE_OPTIONS.map((opt) => (
              <label
                key={opt.value}
                className="row"
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: 8,
                  padding: "6px 0",
                  cursor: "pointer",
                }}
              >
                <input
                  type="checkbox"
                  checked={scopes.includes(opt.value)}
                  onChange={() => toggleScope(opt.value)}
                />
                <div>
                  <div style={{ fontWeight: 600 }}>{opt.label}</div>
                  <div className="muted text-sm">{opt.description}</div>
                </div>
              </label>
            ))}
          </div>
          <div className="field-help muted text-sm" style={{ marginTop: 4 }}>
            Tokens without any scope can authenticate but won't be able
            to call scope-gated endpoints.
          </div>
        </div>

        <div className="field">
          <label className="field-label" htmlFor="at-expires">
            Expires at <span className="hint">optional · browser local time · stored as UTC</span>
          </label>
          <input
            id="at-expires"
            className="input mono"
            type="datetime-local"
            value={expiresAtLocal}
            onChange={(e) => setExpiresAtLocal(e.target.value)}
            style={{ width: "100%" }}
          />
          <div className="field-help muted text-sm" style={{ marginTop: 4 }}>
            Leave blank for a non-expiring token. Must be in the future.
          </div>
        </div>

        {submitError && (
          <Banner
            kind="error"
            title={submitError.code ? `Create failed (${submitError.code})` : "Create failed"}
            detail={submitError.message || ""}
          />
        )}
      </div>
    </Modal>
  );
}

// ============================================================================
// AT_PlaintextOneTimeDialog — the only place the plaintext is visible.
//
// Per Spec §8: copy button + dire warning + separate close button. The
// `<pre>` is selectable for keyboard users; the copy button uses the
// Clipboard API with a writeable fallback to document.execCommand.
// ============================================================================

function AT_PlaintextOneTimeDialog({ token, onClose }) {
  const [copied, setCopied] = React.useState(false);
  const [copyError, setCopyError] = React.useState(null);

  const copy = async () => {
    setCopyError(null);
    const text = token.plaintext || "";
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // Fallback for browsers without the async Clipboard API.
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        try { document.execCommand("copy"); } finally { document.body.removeChild(ta); }
      }
      setCopied(true);
      // Reset the affordance after 2s so users can copy again if they
      // missed the clipboard the first time round.
      setTimeout(() => setCopied(false), 2000);
    } catch (e) {
      setCopyError(e && e.message ? e.message : "Copy failed");
    }
  };

  return (
    <Modal
      title="Token created — copy it now"
      danger
      onClose={onClose}
      footer={
        <>
          <Btn
            kind="primary"
            icon={copied ? "check" : "copy"}
            onClick={copy}
            data-testid="copy-token-btn"
          >
            {copied ? "Copied!" : "Copy token"}
          </Btn>
          <Btn
            kind="default"
            onClick={onClose}
            data-testid="close-plaintext-btn"
          >
            I have saved it — close
          </Btn>
        </>
      }
    >
      <div data-testid="at-plaintext-dialog">
        <Banner
          kind="warning"
          title="This is the only time you'll see this token."
          detail="Copy it now — it cannot be retrieved later. If you lose it, revoke this token and create a new one."
        />
        <div className="field" style={{ marginTop: 12 }}>
          <label className="field-label">Token</label>
          <pre
            data-testid="plaintext-display"
            className="mono"
            style={{
              padding: "10px 12px",
              border: "1px solid var(--border)",
              borderRadius: 4,
              background: "var(--surface-1)",
              fontSize: 12,
              userSelect: "text",
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
              margin: 0,
            }}
          >
            {token.plaintext}
          </pre>
        </div>
        <div className="field" style={{ marginTop: 8 }}>
          <dl className="kv" style={{ gridTemplateColumns: "120px 1fr", rowGap: 4 }}>
            <dt>Name</dt>
            <dd>{token.name}</dd>
            <dt>Prefix</dt>
            <dd className="mono">{token.prefix}…</dd>
            {Array.isArray(token.scopes) && token.scopes.length > 0 && (
              <>
                <dt>Scopes</dt>
                <dd>
                  {token.scopes.map((s) => (
                    <span
                      key={s}
                      className="pill pill-paused"
                      style={{ fontSize: 10.5, marginRight: 4 }}
                    >
                      {s}
                    </span>
                  ))}
                </dd>
              </>
            )}
            {token.expires_at && (
              <>
                <dt>Expires</dt>
                <dd className="mono" title={token.expires_at}>{token.expires_at}</dd>
              </>
            )}
          </dl>
        </div>
        {copyError && (
          <Banner
            kind="error"
            title="Copy failed"
            detail={copyError + " — select the token above and copy manually."}
          />
        )}
      </div>
    </Modal>
  );
}

// ============================================================================
// AT_RevokeConfirmDialog — confirm + DELETE /v1/auth/tokens/{id}
// ============================================================================

function AT_RevokeConfirmDialog({ token, onClose, onRevoked }) {
  const { apiFetch } = window.primerApi;
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
      await apiFetch("DELETE", "/auth/tokens/" + encodeURIComponent(token.id));
      if (!mountedRef.current) return;
      onRevoked && onRevoked();
    } catch (err) {
      if (!mountedRef.current) return;
      setError(AT_extractError(err));
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };

  return (
    <Modal
      title={`Revoke token · ${token.name}`}
      danger
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn
            kind="danger"
            icon="trash"
            onClick={submit}
            disabled={busy}
            data-testid="revoke-confirm-btn"
          >
            {busy ? "Revoking…" : "Revoke token"}
          </Btn>
        </>
      }
    >
      <div data-testid="at-revoke-confirm">
        <p>Token will stop working immediately. The row stays for audit.</p>
        <ul>
          <li>Any in-flight request authenticated by this token completes.</li>
          <li>Future requests with this bearer get a 401.</li>
          <li>This action cannot be undone.</li>
        </ul>
        {error && (
          <Banner
            kind="error"
            title={error.code ? `Revoke failed (${error.code})` : "Revoke failed"}
            detail={error.message || ""}
          />
        )}
      </div>
    </Modal>
  );
}

// ============================================================================
// Exports
// ============================================================================

window.AT_ApiTokensPage = AT_ApiTokensPage;
window.AT_TokenRow = AT_TokenRow;
window.AT_CreateTokenDialog = AT_CreateTokenDialog;
window.AT_PlaintextOneTimeDialog = AT_PlaintextOneTimeDialog;
window.AT_RevokeConfirmDialog = AT_RevokeConfirmDialog;
