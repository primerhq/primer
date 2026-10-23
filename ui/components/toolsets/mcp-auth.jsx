/* global React, PK_Section, PK_Segmented, PK_Grid, PK_Field, PK_Fold, TS_KvEditor */

// Authentication form helpers shared by the toolset create and edit modal.
// Every exported name carries the TS_ prefix because Babel loads these files
// into one browser-global scope.
const TS_AUTH_MODES = [
  { value: "none", label: "None" },
  { value: "header", label: "Header" },
  { value: "oauth_browser", label: "OAuth (browser sign-in)" },
  { value: "oauth_client_credentials", label: "OAuth (client credentials)" },
];

function TS_authModeOf(httpConfig) {
  const cfg = httpConfig || {};
  if (cfg.oauth) {
    return cfg.oauth.grant === "client_credentials"
      ? "oauth_client_credentials"
      : "oauth_browser";
  }
  return cfg.headers && Object.keys(cfg.headers).length ? "header" : "none";
}

function TS_authPairs(headers) {
  return headers && typeof headers === "object"
    ? Object.entries(headers).map(function (entry) {
        return { key: entry[0], value: String(entry[1]) };
      })
    : [];
}

function TS_authFormOf(httpConfig) {
  const cfg = httpConfig || {};
  const oauth = cfg.oauth || {};
  const client = oauth.static_client || {};
  return {
    mode: TS_authModeOf(cfg),
    headers: TS_authPairs(cfg.headers),
    scopes: Array.isArray(oauth.scopes) ? oauth.scopes.join(" ") : "",
    clientId: client.client_id || "",
    clientSecret: client.client_secret || "",
    resource: oauth.resource_uri || "",
    tokenEndpoint: oauth.token_endpoint || "",
  };
}

function TS_authHeaders(pairs) {
  return Object.fromEntries(
    (pairs || [])
      .map(function (pair) { return [String(pair.key || "").trim(), pair.value || ""]; })
      .filter(function (entry) { return entry[0].length > 0; }),
  );
}

function TS_applyAuth(httpConfig, form) {
  const cfg = JSON.parse(JSON.stringify(httpConfig || {}));
  const next = form || { mode: "none", headers: [] };
  if (next.mode === "none") {
    cfg.oauth = null;
    cfg.headers = {};
    return cfg;
  }
  if (next.mode === "header") {
    cfg.oauth = null;
    cfg.headers = TS_authHeaders(next.headers);
    return cfg;
  }

  const oauth = Object.assign({}, cfg.oauth || {});
  const clientId = String(next.clientId || "").trim();
  oauth.grant = next.mode === "oauth_client_credentials"
    ? "client_credentials"
    : "authorization_code";
  oauth.scopes = String(next.scopes || "").trim().split(/\s+/).filter(Boolean);
  // An empty secret is null, never "": a public client is stored with a null secret and shown as an empty field, and an "" sent back is a
  // different secret, so an unchanged save would count as an OAuth change (an admin's signs the toolset out, a user's is refused).
  oauth.static_client = clientId
    ? { client_id: clientId, client_secret: next.clientSecret ? next.clientSecret : null }
    : null;
  oauth.resource_uri = String(next.resource || "").trim() || null;
  // The token endpoint applies to client credentials only, and its field is hidden for a browser sign-in: a switch from client credentials
  // that kept it would be refused, with no field to clear it in.
  oauth.token_endpoint = next.mode === "oauth_client_credentials"
    ? String(next.tokenEndpoint || "").trim() || null
    : null;
  cfg.oauth = oauth;
  cfg.headers = TS_authHeaders(next.headers);
  return cfg;
}

function TS_validateAuth(form) {
  const errors = {};
  if (form && form.mode === "oauth_client_credentials") {
    if (!String(form.clientId || "").trim()) {
      errors["oauth.static_client.client_id"] = "Required";
    }
    if (!String(form.clientSecret || "").trim()) {
      errors["oauth.static_client.client_secret"] = "Required";
    }
  }
  return errors;
}

function TS_authError(errors, suffix) {
  const all = errors || {};
  return all[suffix] || all["body.config.config." + suffix] || null;
}

// The error keys the section draws under a field in the form's mode, relative to the HTTP config as TS_authError reads them. The toolset form
// puts every other error in its banner (TS_shownErrorKeys in toolsets.jsx), so this list and the fields below must agree.
function TS_authErrorKeys(form) {
  const mode = form && form.mode;
  if (mode !== "oauth_browser" && mode !== "oauth_client_credentials") return [];
  return [
    "oauth.static_client.client_id",
    "oauth.static_client.client_secret",
    "oauth.scopes",
    mode === "oauth_client_credentials" ? "oauth.token_endpoint" : "oauth.resource_uri",
  ];
}

function TS_McpAuthSection({ form, onChange, errors, locked, isMobile }) {
  const change = function (patch) { onChange(Object.assign({}, form, patch)); };
  const isOAuth = form.mode === "oauth_browser" || form.mode === "oauth_client_credentials";
  const isClientCredentials = form.mode === "oauth_client_credentials";
  const clientIdError = TS_authError(errors, "oauth.static_client.client_id");
  const clientSecretError = TS_authError(errors, "oauth.static_client.client_secret");
  const scopesError = TS_authError(errors, "oauth.scopes");
  const resourceError = TS_authError(errors, "oauth.resource_uri");
  const endpointError = TS_authError(errors, "oauth.token_endpoint");
  // A folded field is not mounted (PK_Fold), so a refused resource is drawn outside the fold, where its error can be seen.
  const resourceField = (
    <PK_Field
      label="Resource"
      help="Defaults to the server URL"
      error={resourceError}
    >
      <input
        className="input mono"
        data-testid="ts-oauth-resource"
        value={form.resource}
        disabled={!!locked}
        onChange={function (event) { change({ resource: event.target.value }); }}
        style={{ width: "100%" }}
      />
    </PK_Field>
  );

  return (
    <PK_Section title="AUTHENTICATION" testid="ts-auth-section" cols={1}>
      {locked ? (
        <div className="field-help warn" data-testid="ts-auth-locked">
          Only an admin can change an OAuth toolset's URL or sign-in settings.
        </div>
      ) : null}
      <fieldset disabled={!!locked} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
        <PK_Segmented
          testid="ts-auth-mode"
          ariaLabel="Authentication"
          options={TS_AUTH_MODES}
          value={form.mode}
          onChange={function (mode) { change({ mode: mode }); }}
        />
        <PK_Grid cols={isMobile ? 1 : 2}>
          {form.mode === "header" ? (
            <TS_KvEditor
              testid="ts-auth-headers"
              label="Headers"
              hint="Sent on every request to the MCP server"
              pairs={form.headers}
              onChange={function (headers) { change({ headers: headers }); }}
              keyPlaceholder="Authorization"
              valuePlaceholder="Bearer ..."
            />
          ) : null}
          {isOAuth ? (
            <React.Fragment>
              <PK_Field
                label="Client id"
                required={isClientCredentials}
                help={isClientCredentials ? null : "Leave empty to register a client automatically"}
                error={clientIdError}
              >
                <input
                  className="input mono"
                  data-testid="ts-oauth-client-id"
                  value={form.clientId}
                  disabled={!!locked}
                  required={isClientCredentials}
                  onChange={function (event) { change({ clientId: event.target.value }); }}
                  style={{ width: "100%" }}
                />
              </PK_Field>
              <PK_Field
                label="Client secret"
                required={isClientCredentials}
                help={isClientCredentials ? null : "Only for a pre-registered confidential client"}
                error={clientSecretError}
              >
                <input
                  className="input mono"
                  type="password"
                  data-testid="ts-oauth-client-secret"
                  value={form.clientSecret}
                  disabled={!!locked}
                  required={isClientCredentials}
                  onChange={function (event) { change({ clientSecret: event.target.value }); }}
                  style={{ width: "100%" }}
                />
              </PK_Field>
              <PK_Field
                label="Scopes"
                help="Space-separated. Empty asks for the server's default scopes."
                error={scopesError}
              >
                <input
                  className="input mono"
                  data-testid="ts-oauth-scopes"
                  value={form.scopes}
                  disabled={!!locked}
                  onChange={function (event) { change({ scopes: event.target.value }); }}
                  style={{ width: "100%" }}
                />
              </PK_Field>
              {isClientCredentials ? (
                <PK_Field
                  label="Token endpoint"
                  help="Found from the server when empty"
                  error={endpointError}
                >
                  <input
                    className="input mono"
                    data-testid="ts-oauth-token-endpoint"
                    value={form.tokenEndpoint}
                    disabled={!!locked}
                    onChange={function (event) { change({ tokenEndpoint: event.target.value }); }}
                    style={{ width: "100%" }}
                  />
                </PK_Field>
              ) : resourceError ? (
                resourceField
              ) : (
                <PK_Fold label="More" testid="ts-oauth-more">
                  {resourceField}
                </PK_Fold>
              )}
              <TS_KvEditor
                testid="ts-auth-headers"
                label="Extra headers"
                hint="Sent on every request to the MCP server"
                pairs={form.headers}
                onChange={function (headers) { change({ headers: headers }); }}
                keyPlaceholder="X-Tenant"
                valuePlaceholder="value"
              />
            </React.Fragment>
          ) : null}
        </PK_Grid>
      </fieldset>
    </PK_Section>
  );
}

window.TS_AUTH_MODES = TS_AUTH_MODES;
window.TS_authModeOf = TS_authModeOf;
window.TS_authFormOf = TS_authFormOf;
window.TS_applyAuth = TS_applyAuth;
window.TS_validateAuth = TS_validateAuth;
window.TS_authErrorKeys = TS_authErrorKeys;
window.TS_McpAuthSection = TS_McpAuthSection;

const TS_OAUTH_RETURN_TEXT = {
  invalid_state: "The sign-in expired or was already used. Connect again.",
  access_denied: "The sign-in was cancelled at the provider.",
  exchange_failed: "The provider refused the sign-in. Check the client settings and connect again.",
  settings_changed: "The toolset's settings changed during the sign-in. Connect again.",
};

function TS_oauthReturnText(code) {
  return Object.prototype.hasOwnProperty.call(TS_OAUTH_RETURN_TEXT, code)
    ? TS_OAUTH_RETURN_TEXT[code]
    : "The sign-in did not complete. Connect again.";
}

// The provider's page opens in a new tab that cannot reach back to the console. The authorization server is named by the MCP server's
// metadata, so by whoever registered the toolset, and with `window.opener` its page could send this tab to a look-alike sign-in page
// (reverse tabnabbing). window.open with "noopener" answers null whether or not a tab opened, which would hide a blocked pop-up, so the tab
// opens blank, its opener is cut, and only then is it sent to the provider. A blocked pop-up still falls back to this tab.
function TS_openSignIn(url) {
  const opened = window.open("about:blank", "_blank");
  if (!opened) {
    if (window.location && typeof window.location.assign === "function") window.location.assign(url);
    return;
  }
  try {
    opened.opener = null;
  } catch (_error) { /* a browser that refuses the write gave the tab no opener to cut */ }
  opened.location = url;
}

function TS_takeOAuthReturn(location, history) {
  if (!location || !location.search) return null;
  const entries = location.search.replace(/^\?/, "").split("&").filter(Boolean);
  const kept = [];
  const found = {};
  entries.forEach(function (entry) {
    const split = entry.indexOf("=");
    const rawKey = split >= 0 ? entry.slice(0, split) : entry;
    const rawValue = split >= 0 ? entry.slice(split + 1) : "";
    let key = rawKey;
    let value = rawValue;
    try { key = decodeURIComponent(rawKey.replace(/\+/g, " ")); } catch (_error) { /* keep raw */ }
    try { value = decodeURIComponent(rawValue.replace(/\+/g, " ")); } catch (_error) { /* keep raw */ }
    if (key === "mcp_oauth" || key === "mcp_oauth_toolset" || key === "mcp_oauth_error") {
      found[key] = value;
    } else {
      kept.push(entry);
    }
  });
  if (!found.mcp_oauth || !found.mcp_oauth_toolset) return null;
  const next = (location.pathname || "")
    + (kept.length ? "?" + kept.join("&") : "")
    + (location.hash || "");
  if (history && typeof history.replaceState === "function") {
    history.replaceState(null, "", next);
  }
  return {
    toolsetId: found.mcp_oauth_toolset,
    outcome: found.mcp_oauth === "connected" ? "connected" : "error",
    code: found.mcp_oauth_error || null,
  };
}

function TS_authExpiryText(expiresAt) {
  if (!expiresAt) return "no expiry";
  const minutes = Math.max(0, Math.round((new Date(expiresAt).getTime() - Date.now()) / 60000));
  return "expires in " + minutes + " min";
}

function TS_McpAuthPanel({ toolsetId, pushToast, compact }) {
  const api = window.primerApi;
  const status = api.useResource(
    "toolset-auth:" + toolsetId,
    function (signal) {
      return api.apiFetch(
        "GET",
        "/toolsets/" + encodeURIComponent(toolsetId) + "/oauth/status",
        null,
        { signal: signal },
      );
    },
    { pollMs: 0, deps: [toolsetId] },
  );
  const auth = api.useResource(
    "auth-status",
    function (signal) { return api.apiFetch("GET", "/auth/status", null, { signal: signal }); },
    { pollMs: 0 },
  );
  const viewport = typeof api.useViewport === "function" ? api.useViewport() : { isMobile: false };
  const [actionError, setActionError] = React.useState(null);
  const pollStarted = React.useRef(0);
  const pollDone = React.useRef(false);
  const push = typeof pushToast === "function" ? pushToast : api.toastPush;
  const invalidates = [
    "toolset-auth:" + toolsetId,
    "toolset-tools:" + toolsetId,
    "toolsets:catalogue",
    "nv-plat:toolsets",
  ];

  function connected() {
    if (pollDone.current) return;
    pollDone.current = true;
    status.refetch();
    if (typeof push === "function") push({ kind: "success", title: "Connected", detail: toolsetId });
  }

  function poll() {
    if (pollDone.current || Date.now() - pollStarted.current >= 600000) return;
    api.apiFetch("GET", "/toolsets/" + encodeURIComponent(toolsetId) + "/oauth/status")
      .then(function (next) {
        if (next && next.state === "connected") connected();
        else window.setTimeout(poll, 2000);
      })
      .catch(function () { window.setTimeout(poll, 2000); });
  }

  const start = api.useMutation(
    function () {
      return api.apiFetch("POST", "/toolsets/" + encodeURIComponent(toolsetId) + "/oauth/start");
    },
    {
      invalidates: invalidates,
      onSuccess: function (answer) {
        setActionError(null);
        if (answer && answer.authorize_url) {
          pollDone.current = false;
          pollStarted.current = Date.now();
          TS_openSignIn(answer.authorize_url);
          window.setTimeout(poll, 2000);
        } else {
          connected();
        }
      },
      onError: function (error) {
        const message = (error && (error.detail || error.message || error.title)) || "Could not connect.";
        setActionError(message);
        if (typeof push === "function") push({ kind: "error", title: "Connect failed", detail: message });
      },
    },
  );
  const disconnect = api.useMutation(
    function () {
      return api.apiFetch("POST", "/toolsets/" + encodeURIComponent(toolsetId) + "/oauth/disconnect");
    },
    {
      invalidates: invalidates,
      onSuccess: function () {
        setActionError(null);
        status.refetch();
        if (typeof push === "function") push({ kind: "success", title: "Disconnected", detail: toolsetId });
      },
      onError: function (error) {
        const message = (error && (error.detail || error.message || error.title)) || "Could not disconnect.";
        setActionError(message);
        if (typeof push === "function") push({ kind: "error", title: "Disconnect failed", detail: message });
      },
    },
  );

  async function askDisconnect() {
    const ok = await window.confirmDialog({
      title: "Disconnect " + toolsetId + "?",
      message: "Every agent that uses this toolset loses access to it until an admin connects it again.",
      confirmLabel: "Disconnect",
      danger: true,
    });
    if (ok) {
      try { await disconnect.mutate(); } catch (_error) { /* shown inline and as a toast */ }
    }
  }

  function beginStart() {
    pollDone.current = false;
    start.mutate().catch(function () {});
  }

  if (status.loading && !status.data) {
    return <div className="muted text-sm" data-testid="ts-auth-panel">Checking sign-in...</div>;
  }
  if (status.error && !status.data) {
    return (
      <div data-testid="ts-auth-panel">
        <Banner
          kind="error"
          title="Could not read sign-in status"
          detail={status.error.detail || status.error.message}
          actions={<Btn size="sm" onClick={status.refetch}>Retry</Btn>}
        />
      </div>
    );
  }
  const value = status.data || {};
  if (value.state === "not_applicable") return null;
  const isAdmin = !!(auth.data && auth.data.role === "admin");
  const hasConnection = !!value.connected_at;
  let stateText = "Needs sign-in";
  if (value.state === "connected") stateText = "Connected " + TS_authExpiryText(value.expires_at);
  else if (value.state === "error") stateText = "Error" + (value.reason ? " " + value.reason : "");
  else if (value.reason) stateText += " " + value.reason;

  return (
    <div data-testid="ts-auth-panel" style={compact ? { marginTop: 8 } : { marginBottom: 12 }}>
      <div className="text-sm" data-testid="ts-auth-status">{stateText}</div>
      {actionError ? <div className="field-help warn" data-testid="ts-auth-action-error">{actionError}</div> : null}
      <div
        data-testid="ts-auth-actions"
        data-layout={viewport.isMobile ? "stacked" : "inline"}
        style={{ display: "flex", flexDirection: viewport.isMobile ? "column" : "row", gap: 8, marginTop: 8 }}
      >
        <Btn
          size="sm"
          kind="secondary"
          data-testid={hasConnection ? "ts-auth-reconnect" : "ts-auth-connect"}
          disabled={!isAdmin}
          busy={start.loading}
          onClick={beginStart}
        >
          {hasConnection ? "Reconnect" : "Connect"}
        </Btn>
        {hasConnection ? (
          <Btn
            size="sm"
            kind="danger"
            data-testid="ts-auth-disconnect"
            disabled={!isAdmin}
            busy={disconnect.loading}
            onClick={askDisconnect}
          >Disconnect</Btn>
        ) : null}
      </div>
      {!isAdmin ? (
        <div className="field-help" data-testid="ts-auth-admin-note">Only an admin can connect this toolset.</div>
      ) : null}
    </div>
  );
}

function TS_cardAuthStatus(row) {
  const state = row && row.auth && row.auth.state;
  if (state === "connected") return { label: "Connected", tone: "ok" };
  if (state === "needs_sign_in") return { label: "Needs sign-in", tone: "warn" };
  if (state === "error") return { label: "Error", tone: "error" };
  return null;
}

window.TS_OAUTH_RETURN_TEXT = TS_OAUTH_RETURN_TEXT;
window.TS_oauthReturnText = TS_oauthReturnText;
window.TS_openSignIn = TS_openSignIn;
window.TS_takeOAuthReturn = TS_takeOAuthReturn;
window.TS_McpAuthPanel = TS_McpAuthPanel;
window.TS_cardAuthStatus = TS_cardAuthStatus;
