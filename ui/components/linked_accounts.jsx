/* global React, Icon, Btn, Modal, Banner */
// Self-service "Linked accounts" console page (Layer 2 OIDC SSO, Task 10).
//
// Prefix LA_ to avoid global name collisions with other components.
//
// Every logged-in user (role user/admin) manages their OWN linked SSO
// identities here — this is NOT an admin page (contrast sso_admin.jsx,
// which configures the OIDC providers themselves).
//
// Reads/writes the endpoints shipped in Task 7:
//   GET    /auth/sso/identities        -> the caller's own linked identities
//   DELETE /auth/sso/identities/{id}   -> unlink one (owner-scoped, 204)
//   GET    /auth/sso/providers         -> enabled providers [{id, name}]
// Linking a NEW provider is a full-page browser navigation to
// /v1/auth/sso/{id}/link (NOT an apiFetch call — it 302s to the IdP), so
// that path carries the /v1 prefix explicitly (mirrors auth.jsx's
// _SsoButtons login redirect).

// ============================================================================
// Helpers
// ============================================================================

// Extract a {code, message} from an ApiError envelope. Mirrors the parser
// used by api_tokens.jsx / triggers.jsx.
function LA_extractError(err) {
  const env = err && err.envelope;
  const envDetail = env && env.detail;
  let code = null;
  let msg = null;
  if (envDetail && typeof envDetail === "object") {
    code = envDetail.error || envDetail.code || null;
    msg = envDetail.message || null;
  }
  if (!msg && typeof err.detail === "string") msg = err.detail;
  if (!msg) msg = (err && (err.title || err.message)) || "Request failed";
  return { code, message: msg };
}

// Absolute local-time display for created_at — this is an audit-style
// timestamp users compare against "when did I link this", not a relative
// countdown like token expiry.
function LA_fmtDate(iso) {
  if (!iso) return "—";
  const t = typeof iso === "string" ? Date.parse(iso) : (iso instanceof Date ? iso.getTime() : NaN);
  if (!Number.isFinite(t)) return String(iso);
  return new Date(t).toLocaleString();
}

// ============================================================================
// LA_LinkedAccountsPage — top-level list view + link-provider entry points
// ============================================================================

function LA_LinkedAccountsPage() {
  const { useResource, apiFetch } = window.primerApi;
  const [confirmUnlink, setConfirmUnlink] = React.useState(null); // identity | null
  const [providers, setProviders] = React.useState([]);

  const list = useResource(
    "linked-accounts:list",
    (signal) => apiFetch("GET", "/auth/sso/identities", null, { signal }),
    { pollMs: 10000 },
  );

  // GET /auth/sso/identities returns a bare JSON array (not {items: [...]}),
  // unlike the tokens list — guard with Array.isArray rather than ?.items.
  const items = Array.isArray(list.data) ? list.data : [];

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await apiFetch("GET", "/auth/sso/providers", null, {});
        if (!cancelled && Array.isArray(r)) setProviders(r);
      } catch {
        // No providers configured, or the endpoint isn't reachable yet —
        // degrade to just the linked-identities table (no crash).
      }
    })();
    return () => { cancelled = true; };
  }, [apiFetch]);

  const linkedProviderIds = new Set(items.map((i) => i.provider_id));
  const linkable = providers.filter((p) => !linkedProviderIds.has(p.id));

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <span style={{ fontSize: 13, fontWeight: 600 }}>Linked accounts</span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
        </div>
      </div>

      {list.loading && items.length === 0 && (
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
      )}
      {list.error && items.length === 0 && (
        <Banner
          kind="error"
          title={list.error.title || "Couldn't load linked accounts"}
          detail={list.error.detail || list.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
        />
      )}
      {!list.loading && !list.error && items.length === 0 && (
        <div className="empty" style={{ padding: "40px 20px" }} data-testid="linked-accounts-empty">
          <div className="ico-wrap"><Icon name="key" size={22} /></div>
          <div className="head">No linked accounts yet</div>
          <div className="sub">
            Link a single sign-on provider below to sign in without a
            password.
          </div>
        </div>
      )}

      {items.length > 0 && (
        <div
          data-testid="linked-accounts-table"
          className="panel"
          style={{ padding: 0, overflow: "hidden" }}
        >
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Provider</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Subject</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Email</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Linked</th>
                <th style={{ textAlign: "right", padding: "8px 12px" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((identity) => (
                <LA_IdentityRow
                  key={identity.id}
                  identity={identity}
                  onUnlink={() => setConfirmUnlink(identity)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {linkable.length > 0 && (
        <div className="panel" style={{ padding: 14 }} data-testid="linked-accounts-link-providers">
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Link a provider</div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {linkable.map((p) => (
              <Btn
                key={p.id}
                size="sm"
                kind="default"
                icon="external"
                data-testid={`link-provider-btn-${p.id}`}
                onClick={() => {
                  // Full-page nav (not apiFetch) — the endpoint 302s to the
                  // IdP. Real browser URL, so it carries the /v1 prefix.
                  window.location.href = "/v1/auth/sso/" + encodeURIComponent(p.id) + "/link";
                }}
              >
                Link {p.name}
              </Btn>
            ))}
          </div>
        </div>
      )}

      {confirmUnlink && (
        <LA_UnlinkConfirmDialog
          identity={confirmUnlink}
          onClose={() => setConfirmUnlink(null)}
          onUnlinked={() => {
            setConfirmUnlink(null);
            list.refetch();
          }}
        />
      )}
    </div>
  );
}

// ============================================================================
// LA_IdentityRow — one row of the table.
// ============================================================================

function LA_IdentityRow({ identity, onUnlink }) {
  return (
    <tr
      data-testid={`linked-account-row-${identity.id}`}
      style={{ borderTop: "1px solid var(--border)" }}
    >
      <td style={{ padding: "8px 12px", fontWeight: 600 }}>{identity.provider_name}</td>
      <td style={{ padding: "8px 12px" }}>
        <span className="mono" style={{ fontSize: 11 }}>{identity.subject}</span>
      </td>
      <td style={{ padding: "8px 12px" }}>
        {identity.email ? identity.email : <span className="muted text-sm">—</span>}
      </td>
      <td style={{ padding: "8px 12px" }} title={identity.created_at || ""}>
        <span className="mono">{LA_fmtDate(identity.created_at)}</span>
      </td>
      <td style={{ padding: "8px 12px", textAlign: "right", whiteSpace: "nowrap" }}>
        <Btn
          size="sm"
          kind="danger"
          icon="trash"
          onClick={onUnlink}
          title="Unlink this identity"
          data-testid={`unlink-account-btn-${identity.id}`}
        >
          Unlink
        </Btn>
      </td>
    </tr>
  );
}

// ============================================================================
// LA_UnlinkConfirmDialog — confirm + DELETE /v1/auth/sso/identities/{id}
// ============================================================================

function LA_UnlinkConfirmDialog({ identity, onClose, onUnlinked }) {
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
      await apiFetch("DELETE", "/auth/sso/identities/" + encodeURIComponent(identity.id));
      if (!mountedRef.current) return;
      onUnlinked && onUnlinked();
    } catch (err) {
      if (!mountedRef.current) return;
      setError(LA_extractError(err));
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };

  return (
    <Modal
      title={`Unlink account · ${identity.provider_name}`}
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
            data-testid="unlink-confirm-btn"
          >
            {busy ? "Unlinking…" : "Unlink account"}
          </Btn>
        </>
      }
    >
      <div data-testid="la-unlink-confirm">
        <p>
          You will no longer be able to sign in with{" "}
          <strong>{identity.provider_name}</strong> using this identity.
        </p>
        <ul>
          <li>Your account and other linked identities are unaffected.</li>
          <li>You can re-link this provider at any time.</li>
          <li>This action cannot be undone.</li>
        </ul>
        {error && (
          <Banner
            kind="error"
            title={error.code ? `Unlink failed (${error.code})` : "Unlink failed"}
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

window.LA_LinkedAccountsPage = LA_LinkedAccountsPage;
window.LA_IdentityRow = LA_IdentityRow;
window.LA_UnlinkConfirmDialog = LA_UnlinkConfirmDialog;
