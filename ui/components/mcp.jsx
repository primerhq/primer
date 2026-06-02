/* global React, Icon, Btn, Banner */
// MCP server endpoint console page (Spec §11).
//
// Prefix MC_ to avoid global name collisions with other components.
//
// Two stacked panels:
//   1. Endpoint     -- enable/disable toggle + URL/Claude Desktop config
//                      copy buttons + "exposed N tools" caption + last-edited.
//   2. Exposed tools -- table of every catalogue tool with availability flags
//                      (sourced from GET /v1/mcp_exposure/available) and an
//                      allowlist editor PUT'd back to /v1/mcp_exposure.
//
// PUT shape:  { enabled?: bool, allowed_tools?: string[] }
// Both fields are optional ("PATCH-shaped" -- spec §6); the page sends
// only the field that changed so a toggle never accidentally clobbers
// the allowlist and vice versa.

// ============================================================================
// Helpers
// ============================================================================

// Relative-time formatter — matches the api_tokens.jsx implementation
// so the "last edited" caption reads the same in both consoles.
function MC_relTime(iso) {
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

// Extract a {code, message} from an ApiError envelope. Mirrors
// AT_extractError so PUT failures surface the same way as the API
// tokens page.
function MC_extractError(err) {
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

// Copy text to the clipboard with a graceful fallback for browsers
// without the async Clipboard API. Returns true on success.
async function MC_copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try { document.execCommand("copy"); } finally { document.body.removeChild(ta); }
    return true;
  } catch {
    return false;
  }
}

// Derived MCP endpoint URL. The MCP transport is mounted at /v1/mcp
// (see primer/api/app.py); we surface the absolute URL so operators
// can paste it straight into a Claude Desktop config.
function MC_endpointUrl() {
  try {
    return `${window.location.origin}/v1/mcp`;
  } catch {
    return "/v1/mcp";
  }
}

// Build the Claude Desktop config snippet. The token placeholder is
// intentionally a marker the user must replace — we do NOT render the
// plaintext of any real token here (those are one-time-view per
// api_tokens.jsx).
function MC_claudeDesktopConfig() {
  return JSON.stringify({
    mcpServers: {
      primer: {
        type: "streamable_http",
        url: MC_endpointUrl(),
        headers: { Authorization: "Bearer <YOUR_TOKEN>" },
      },
    },
  }, null, 2);
}

// Conservative read-only recommendation. The picker still requires an
// explicit Save click — this only changes the staged set.
function MC_isSafeDefault(scoped_id) {
  if (!scoped_id || typeof scoped_id !== "string") return false;
  // Match toolset__bare patterns.
  const idx = scoped_id.indexOf("__");
  if (idx === -1) return false;
  const toolset = scoped_id.slice(0, idx);
  const bare = scoped_id.slice(idx + 2);
  // Anything in the search toolset is read-only by construction.
  if (toolset === "search") return true;
  // Web search is safe to recommend by default; web__http-request is
  // still exposable but the operator has to opt-in explicitly.
  if (scoped_id === "web__web-search") return true;
  // Cherry-picked misc tools (pure-functions / lookups).
  const MISC_SAFE = new Set([
    "get_datetime", "uuid_v4", "calculate", "hash",
  ]);
  if (toolset === "misc" && MISC_SAFE.has(bare)) return true;
  // Generic read-only prefixes on bare names.
  if (
    bare.startsWith("list_") ||
    bare.startsWith("get_") ||
    bare.startsWith("find_")
  ) return true;
  return false;
}

// Denial-reason label used by the table's status cell.
function MC_reasonLabel(reason) {
  if (!reason) return "blocked";
  return reason;
}

// ============================================================================
// MC_McpPage — top-level page
// ============================================================================

function MC_McpPage() {
  const { useResource, apiFetch } = window.primerApi;

  // Singleton exposure row. 10s poll keeps the "last edited" caption
  // and exposed-count fresh without hammering the endpoint — the
  // mutations below trigger an immediate refetch as well.
  const exposure = useResource(
    "mcp-exposure",
    (signal) => apiFetch("GET", "/mcp_exposure", null, { signal }),
    { pollMs: 10000 },
  );

  // Catalogue probe — every catalogue tool with availability flags.
  // 30s cadence: the catalogue changes only on toolset re-registration,
  // so we keep this lighter than the singleton.
  const available = useResource(
    "mcp-available",
    (signal) => apiFetch("GET", "/mcp_exposure/available", null, { signal }),
    { pollMs: 30000 },
  );

  return (
    <div className="col" style={{ gap: 14 }}>
      <MC_EndpointPanel
        exposure={exposure}
        availableItems={available.data?.items}
      />
      <MC_ToolsPanel
        exposure={exposure}
        available={available}
      />
    </div>
  );
}

// ============================================================================
// MC_EndpointPanel — Panel 1
// ============================================================================

function MC_EndpointPanel({ exposure, availableItems }) {
  const { apiFetch } = window.primerApi;

  const row = exposure.data;
  const enabled = !!(row && row.enabled);
  const allowedCount = Array.isArray(row && row.allowed_tools)
    ? row.allowed_tools.length
    : 0;
  // The catalogue count is a sanity check for the user — "you've
  // exposed N of M tools" reads more naturally than "exposed N".
  const totalCount = Array.isArray(availableItems) ? availableItems.length : null;

  const [busy, setBusy] = React.useState(false);
  const [toggleError, setToggleError] = React.useState(null);
  const [copiedUrl, setCopiedUrl] = React.useState(false);
  const [copiedConfig, setCopiedConfig] = React.useState(false);

  const url = MC_endpointUrl();

  const toggle = async () => {
    setBusy(true);
    setToggleError(null);
    try {
      // PATCH-shaped: send only the field we're changing.
      await apiFetch("PUT", "/mcp_exposure", { enabled: !enabled });
      exposure.refetch();
    } catch (err) {
      setToggleError(MC_extractError(err));
    } finally {
      setBusy(false);
    }
  };

  const copyUrl = async () => {
    const ok = await MC_copyText(url);
    if (ok) {
      setCopiedUrl(true);
      setTimeout(() => setCopiedUrl(false), 2000);
    }
  };

  const copyConfig = async () => {
    const ok = await MC_copyText(MC_claudeDesktopConfig());
    if (ok) {
      setCopiedConfig(true);
      setTimeout(() => setCopiedConfig(false), 2000);
    }
  };

  return (
    <div
      data-testid="mcp-endpoint-panel"
      className="panel"
      style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>MCP server endpoint</div>
          <div className="muted text-sm" style={{ marginTop: 2 }}>
            Streamable-HTTP transport at <span className="mono">/v1/mcp</span> ·
            requires a bearer token with the <span className="mono">mcp</span> scope
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span
            className={`pill ${enabled ? "pill-claimed" : "pill-ended"}`}
            style={{ fontSize: 10.5 }}
          >
            {enabled ? "enabled" : "disabled"}
          </span>
          <Btn
            size="sm"
            kind={enabled ? "default" : "primary"}
            icon={enabled ? "pause" : "play"}
            disabled={busy || exposure.loading}
            onClick={toggle}
            data-testid="mcp-toggle-btn"
          >
            {busy ? "…" : (enabled ? "Disable" : "Enable")}
          </Btn>
        </div>
      </div>

      {toggleError && (
        <Banner
          kind="error"
          title={toggleError.code
            ? `Toggle failed (${toggleError.code})`
            : "Toggle failed"}
          detail={toggleError.message || ""}
        />
      )}

      <div className="field" style={{ margin: 0 }}>
        <label className="field-label">Endpoint URL</label>
        <div style={{ display: "flex", gap: 6, alignItems: "stretch" }}>
          <input
            className="input mono"
            value={url}
            readOnly
            style={{ flex: 1 }}
            data-testid="mcp-endpoint-url"
          />
          <Btn
            size="sm"
            kind="ghost"
            icon={copiedUrl ? "check" : "copy"}
            onClick={copyUrl}
            data-testid="copy-url-btn"
          >
            {copiedUrl ? "Copied" : "Copy URL"}
          </Btn>
          <Btn
            size="sm"
            kind="ghost"
            icon={copiedConfig ? "check" : "code"}
            onClick={copyConfig}
            data-testid="copy-claude-config-btn"
            title="Copy a JSON snippet for ~/Library/Application Support/Claude/claude_desktop_config.json"
          >
            {copiedConfig ? "Copied" : "Copy Claude Desktop config"}
          </Btn>
        </div>
        <div className="field-help muted text-sm" style={{ marginTop: 4 }}>
          Paste the config under <span className="mono">mcpServers</span> in
          your Claude Desktop config file, then replace
          {" "}<span className="mono">&lt;YOUR_TOKEN&gt;</span> with a token
          from the API tokens page.
        </div>
      </div>

      <div
        style={{
          display: "flex",
          gap: 16,
          alignItems: "baseline",
          fontSize: 12,
        }}
        className="muted"
      >
        <div>
          Exposed tools:{" "}
          <span className="mono" style={{ color: "var(--text)" }}>
            {allowedCount}
          </span>
          {totalCount != null && (
            <span> / {totalCount} catalogue</span>
          )}
        </div>
        <div style={{ marginLeft: "auto" }}>
          Last edited{" "}
          <span className="mono" style={{ color: "var(--text)" }}>
            {row && row.updated_by ? row.updated_by : "—"}
          </span>{" "}
          <span title={row && row.updated_at ? row.updated_at : ""}>
            {MC_relTime(row && row.updated_at)}
          </span>
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// MC_ToolsPanel — Panel 2
// ============================================================================

function MC_ToolsPanel({ exposure, available }) {
  const { apiFetch } = window.primerApi;

  const items = Array.isArray(available.data?.items) ? available.data.items : [];
  const row = exposure.data;
  const persistedAllowed = React.useMemo(
    () => new Set(Array.isArray(row && row.allowed_tools) ? row.allowed_tools : []),
    [row && row.allowed_tools],
  );

  // Local staged selection. Initialised from persisted set; re-syncs
  // whenever the persisted set changes (after a successful PUT or a
  // poll-driven refetch). We keep selection as a Set for O(1) lookups.
  const [draft, setDraft] = React.useState(persistedAllowed);
  React.useEffect(() => {
    setDraft(persistedAllowed);
  }, [persistedAllowed]);

  // Filter state.
  const [toolsetFilter, setToolsetFilter] = React.useState([]); // [] = no filter
  const [exposableOnly, setExposableOnly] = React.useState(false);
  const [allowedOnly, setAllowedOnly] = React.useState(false);

  // PUT state.
  const [saving, setSaving] = React.useState(false);
  const [saveError, setSaveError] = React.useState(null);

  // Dirty if the staged set differs from the persisted set.
  const isDirty = React.useMemo(() => {
    if (draft.size !== persistedAllowed.size) return true;
    for (const v of draft) {
      if (!persistedAllowed.has(v)) return true;
    }
    return false;
  }, [draft, persistedAllowed]);

  // Distinct toolset ids in catalogue order for the filter chip group.
  const toolsetIds = React.useMemo(() => {
    const seen = new Set();
    const out = [];
    for (const it of items) {
      if (!seen.has(it.toolset_id)) {
        seen.add(it.toolset_id);
        out.push(it.toolset_id);
      }
    }
    return out;
  }, [items]);

  const visibleItems = React.useMemo(() => {
    return items.filter((it) => {
      if (toolsetFilter.length > 0 && !toolsetFilter.includes(it.toolset_id)) {
        return false;
      }
      if (exposableOnly && !it.exposable) return false;
      if (allowedOnly && !draft.has(it.scoped_id)) return false;
      return true;
    });
  }, [items, toolsetFilter, exposableOnly, allowedOnly, draft]);

  const toggleScoped = (scoped_id, exposable) => {
    if (!exposable) return; // Non-exposable rows are not editable.
    setDraft((prev) => {
      const next = new Set(prev);
      if (next.has(scoped_id)) next.delete(scoped_id);
      else next.add(scoped_id);
      return next;
    });
  };

  const toggleToolsetFilter = (tsid) => {
    setToolsetFilter((prev) => prev.includes(tsid)
      ? prev.filter((x) => x !== tsid)
      : [...prev, tsid]);
  };

  const recommendSafeDefaults = () => {
    // Pre-select the conservative read-only set across exposable items.
    // Non-exposable tools are skipped (no point staging an id the
    // server will reject at PUT validation time).
    const next = new Set(draft);
    for (const it of items) {
      if (!it.exposable) continue;
      if (MC_isSafeDefault(it.scoped_id)) {
        next.add(it.scoped_id);
      }
    }
    setDraft(next);
  };

  const save = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const allowed_tools = Array.from(draft).sort();
      await apiFetch("PUT", "/mcp_exposure", { allowed_tools });
      exposure.refetch();
    } catch (err) {
      setSaveError(MC_extractError(err));
    } finally {
      setSaving(false);
    }
  };

  const resetDraft = () => setDraft(persistedAllowed);

  return (
    <div className="panel" style={{ padding: 0, overflow: "hidden" }}>
      {/* Header: title + filter chips + save/reset */}
      <div
        className="filter-bar"
        style={{ padding: "10px 12px", flexWrap: "wrap", gap: 10 }}
      >
        <span style={{ fontSize: 13, fontWeight: 600 }}>Exposed tools</span>
        <div
          style={{
            display: "flex",
            gap: 4,
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          {toolsetIds.map((tsid) => {
            const on = toolsetFilter.includes(tsid);
            return (
              <button
                key={tsid}
                type="button"
                className={`pill ${on ? "pill-claimed" : "pill-paused"}`}
                style={{ cursor: "pointer", fontSize: 10.5 }}
                onClick={() => toggleToolsetFilter(tsid)}
                title={on ? `Hide ${tsid}` : `Show only ${tsid}`}
              >
                {tsid}
              </button>
            );
          })}
        </div>
        <label
          className="muted text-sm"
          style={{ display: "flex", gap: 4, alignItems: "center", cursor: "pointer" }}
        >
          <input
            type="checkbox"
            checked={exposableOnly}
            onChange={(e) => setExposableOnly(e.target.checked)}
          />
          Exposable only
        </label>
        <label
          className="muted text-sm"
          style={{ display: "flex", gap: 4, alignItems: "center", cursor: "pointer" }}
        >
          <input
            type="checkbox"
            checked={allowedOnly}
            onChange={(e) => setAllowedOnly(e.target.checked)}
          />
          Allowed only
        </label>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn
            size="sm"
            kind="ghost"
            icon="zap"
            onClick={recommendSafeDefaults}
            data-testid="recommend-safe-defaults-btn"
            title="Pre-select a conservative read-only set (still requires Save)"
          >
            Recommend safe defaults
          </Btn>
          <Btn
            size="sm"
            kind="ghost"
            icon="refresh"
            onClick={resetDraft}
            disabled={!isDirty || saving}
          >
            Reset
          </Btn>
          <Btn
            size="sm"
            kind="primary"
            icon="check"
            onClick={save}
            disabled={!isDirty || saving}
            data-testid="save-allowed-btn"
          >
            {saving ? "Saving…" : "Save"}
          </Btn>
        </div>
      </div>

      {saveError && (
        <div style={{ padding: "0 12px 10px" }}>
          <Banner
            kind="error"
            title={saveError.code
              ? `Save failed (${saveError.code})`
              : "Save failed"}
            detail={saveError.message || ""}
          />
        </div>
      )}

      {available.loading && items.length === 0 && (
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>
          Loading catalogue…
        </div>
      )}
      {available.error && items.length === 0 && (
        <div style={{ padding: 12 }}>
          <Banner
            kind="error"
            title={available.error.title || "Couldn't load catalogue"}
            detail={available.error.detail || available.error.message}
            actions={<Btn size="sm" icon="refresh" onClick={available.refetch}>Retry</Btn>}
          />
        </div>
      )}
      {!available.loading && !available.error && visibleItems.length === 0 && items.length > 0 && (
        <div className="muted text-sm" style={{ padding: 30, textAlign: "center" }}>
          No tools match the current filters.
        </div>
      )}

      {visibleItems.length > 0 && (
        <table
          data-testid="mcp-tools-table"
          className="table"
          style={{ width: "100%", fontSize: 12 }}
        >
          <thead>
            <tr>
              <th style={{ textAlign: "left", padding: "8px 12px", width: 36 }}></th>
              <th style={{ textAlign: "left", padding: "8px 12px" }}>Tool</th>
              <th style={{ textAlign: "left", padding: "8px 12px" }}>Toolset</th>
              <th style={{ textAlign: "left", padding: "8px 12px" }}>Description</th>
              <th style={{ textAlign: "left", padding: "8px 12px" }}>Status</th>
            </tr>
          </thead>
          <tbody>
            {visibleItems.map((it) => {
              const staged = draft.has(it.scoped_id);
              const persisted = persistedAllowed.has(it.scoped_id);
              return (
                <tr
                  key={it.scoped_id}
                  data-testid={`tool-row-${it.scoped_id}`}
                  style={{
                    borderTop: "1px solid var(--border)",
                    opacity: it.exposable ? 1 : 0.7,
                  }}
                >
                  <td style={{ padding: "6px 12px" }}>
                    {it.exposable ? (
                      <input
                        type="checkbox"
                        checked={staged}
                        onChange={() => toggleScoped(it.scoped_id, true)}
                        aria-label={`Allow ${it.scoped_id}`}
                      />
                    ) : (
                      <Icon name="x-circle" size={14} className="muted" />
                    )}
                  </td>
                  <td style={{ padding: "6px 12px", fontWeight: 500 }}>
                    <span className="mono">{it.scoped_id}</span>
                    {persisted !== staged && (
                      <span
                        className="pill pill-paused"
                        style={{ marginLeft: 6, fontSize: 10 }}
                        title="Unsaved change"
                      >
                        unsaved
                      </span>
                    )}
                  </td>
                  <td style={{ padding: "6px 12px" }}>
                    <span className="pill pill-paused" style={{ fontSize: 10.5 }}>
                      {it.toolset_id}
                    </span>
                  </td>
                  <td
                    style={{
                      padding: "6px 12px",
                      maxWidth: 380,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={it.description || ""}
                  >
                    {it.description || <span className="muted">—</span>}
                  </td>
                  <td style={{ padding: "6px 12px" }}>
                    {it.exposable ? (
                      <span className="pill pill-claimed" style={{ fontSize: 10.5 }}>
                        exposable
                      </span>
                    ) : (
                      <span
                        className="pill pill-failed"
                        style={{ fontSize: 10.5 }}
                        title={`Blocked: ${MC_reasonLabel(it.reason)}`}
                      >
                        {MC_reasonLabel(it.reason)}
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ============================================================================
// Exports
// ============================================================================

window.MC_McpPage = MC_McpPage;
window.MC_EndpointPanel = MC_EndpointPanel;
window.MC_ToolsPanel = MC_ToolsPanel;
window.MC_isSafeDefault = MC_isSafeDefault;
