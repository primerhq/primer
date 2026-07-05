// Auth screens — Register (first boot) and Login (subsequent boots).
//
// Loaded by ui/app.jsx via window.AuthGate, which decides which screen
// to render based on /v1/auth/status:
//   - has_user=false                      → <RegisterScreen />
//   - has_user=true, authenticated=false  → <LoginScreen />
//   - authenticated=true                  → main app
//
// Both screens POST to /v1/auth/{register,login} and reload on success
// (the backend's Set-Cookie carries the signed session token). Visual
// language matches the rest of the console — uses the .auth-* class
// system from styles.css; no inline style soup.

function AuthGate({ children }) {
  const [status, setStatus] = React.useState(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await window.primerApi.apiFetch("GET", "/auth/status", null, {});
        if (!cancelled) setStatus(r);
      } catch {
        if (!cancelled) setStatus({ has_user: false, authenticated: false });
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (status == null) {
    return (
      <div className="auth-shell">
        <div className="muted">Loading…</div>
      </div>
    );
  }
  if (status.authenticated) {
    // Provisioned accounts must set their own password before anything else.
    // (must_change_password + role are added to /auth/status in Task 3.)
    if (status.must_change_password) {
      return <ADM_MustChangePasswordScreen onDone={() => window.location.reload()} />;
    }
    // Restricted users are authenticated but have no console access yet.
    if (status.role === "restricted") {
      return <ADM_PendingAccessScreen username={status.username} />;
    }
    return children;
  }
  if (!status.has_user) return <RegisterScreen onDone={() => window.location.reload()} />;
  return <LoginScreen onDone={() => window.location.reload()} />;
}

function _AuthBrand() {
  return (
    <div className="auth-brand">
      <div className="mark">
        <svg viewBox="0 0 24 24" width="32" height="32" role="img" aria-label="primer">
          <polygon points="12,3 21,12 12,21 3,12" fill="currentColor" fillOpacity="0.18" />
          <polygon points="12,3 16.5,7.5 12,12 7.5,7.5" fill="currentColor" />
          <polygon points="16.5,7.5 21,12 16.5,16.5 12,12" fill="currentColor" fillOpacity="0.45" />
          <polygon points="12,12 16.5,16.5 12,21 7.5,16.5" fill="var(--accent)" />
          <polygon points="7.5,7.5 12,12 7.5,16.5 3,12" fill="currentColor" fillOpacity="0.45" />
        </svg>
      </div>
      <div className="name">primer</div>
    </div>
  );
}

function _InstancePill() {
  return (
    <div className="instance">
      <span className="dot" />
      <span>primer · {window.location.host}</span>
    </div>
  );
}

function _EyeIcon({ open }) {
  if (open) {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
        <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7z" />
        <circle cx="12" cy="12" r="3" />
      </svg>
    );
  }
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M2 12s4-7 10-7c2.5 0 4.7.8 6.5 2M22 12s-4 7-10 7c-2.5 0-4.7-.8-6.5-2" />
      <path d="M3 3l18 18" />
    </svg>
  );
}

function _PasswordField({ label, value, onChange, placeholder, autoComplete, hasErr, errMsg, autoFocus }) {
  const [show, setShow] = React.useState(false);
  return (
    <div className={"auth-field" + (hasErr ? " has-err" : "")}>
      <label>{label}</label>
      <div className="auth-pwd-row">
        <input
          className="mono"
          type={show ? "text" : "password"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder || "••••••••"}
          autoComplete={autoComplete}
          autoFocus={autoFocus}
        />
        <button
          type="button"
          className="toggle touch-target"
          aria-label={show ? "Hide password" : "Show password"}
          onClick={() => setShow((s) => !s)}
        >
          <_EyeIcon open={!show} />
        </button>
      </div>
      {hasErr && errMsg && <div className="field-err">{errMsg}</div>}
    </div>
  );
}

function _ServerBanner({ title, detail, requestId }) {
  if (!title) return null;
  return (
    <div className="auth-banner">
      <div style={{ marginTop: 1 }}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="12" cy="12" r="9" />
          <path d="M9 9l6 6M15 9l-6 6" />
        </svg>
      </div>
      <div style={{ flex: 1 }}>
        <div className="title">{title}</div>
        {detail && <div className="detail">{detail}</div>}
        {requestId && <div className="req-id">request-id {requestId}</div>}
      </div>
    </div>
  );
}

function _extractServerError(err) {
  if (!err) return { title: "Request failed", detail: null, requestId: null };
  const status = err.status;
  if (status === 401) {
    return { title: "Invalid username or password", detail: null, requestId: err.requestId || null };
  }
  if (status === 409) {
    return { title: "Account already exists", detail: err.detail || "Sign in instead.", requestId: err.requestId || null };
  }
  const detail = typeof err.detail === "string" ? err.detail : (err.detail ? JSON.stringify(err.detail) : err.message);
  return {
    title: err.title || "Request failed",
    detail,
    requestId: err.requestId || null,
  };
}

function RegisterScreen({ onDone }) {
  const [username, setUsername] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [server, setServer] = React.useState(null);
  const [fieldErrs, setFieldErrs] = React.useState({});

  const submit = async (e) => {
    e.preventDefault();
    setServer(null);
    const next = {};
    if (!username.trim()) next.username = "username is required";
    if (password.length < 8) next.password = "value must have at least 8 characters";
    if (password !== confirm) next.confirm = "passwords don't match";
    setFieldErrs(next);
    if (Object.keys(next).length > 0) return;

    setBusy(true);
    try {
      await window.primerApi.apiFetch("POST", "/auth/register", { username, password }, {});
      onDone();
    } catch (err) {
      setServer(_extractServerError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-shell">
      <div className="auth-wrap">
        <_AuthBrand />
        <div className="auth-card">
          <div className="auth-h">
            <h1 className="title">Create the operator account</h1>
            <div className="sub">
              This is the only account; SSO and additional users land in a later release.
            </div>
            <_InstancePill />
          </div>
          <form className="auth-body" onSubmit={submit} noValidate>
            <_ServerBanner {...(server || {})} />

            <div className={"auth-field" + (fieldErrs.username ? " has-err" : "")}>
              <label htmlFor="username">Username</label>
              <input
                id="username"
                className="mono"
                value={username}
                onChange={(e) => { setUsername(e.target.value); if (fieldErrs.username) setFieldErrs((p) => ({ ...p, username: undefined })); }}
                autoFocus
                autoComplete="username"
                placeholder="lowercase letters, digits, . _ -"
              />
              {fieldErrs.username && <div className="field-err">{fieldErrs.username}</div>}
            </div>

            <_PasswordField
              label="Password"
              value={password}
              onChange={(v) => { setPassword(v); if (fieldErrs.password) setFieldErrs((p) => ({ ...p, password: undefined })); }}
              placeholder="at least 8 characters"
              autoComplete="new-password"
              hasErr={!!fieldErrs.password}
              errMsg={fieldErrs.password}
            />

            <_PasswordField
              label="Confirm password"
              value={confirm}
              onChange={(v) => { setConfirm(v); if (fieldErrs.confirm) setFieldErrs((p) => ({ ...p, confirm: undefined })); }}
              autoComplete="new-password"
              hasErr={!!fieldErrs.confirm}
              errMsg={fieldErrs.confirm}
            />

            <button
              type="submit"
              className="auth-submit touch-target"
              disabled={busy || !username || !password || !confirm}
              style={{ marginTop: 6 }}
            >
              {busy ? (<><span className="spinner" /><span>Creating…</span></>) : <span>Create account</span>}
            </button>
          </form>
        </div>
        <_AuthFooter />
      </div>
    </div>
  );
}

function _AuthFooter() {
  // /v1/health is unauth-readable and returns version info; fall back
  // to a static string if the probe fails (e.g. cold-boot on first
  // navigation before the route is wired). Keeps the mock's visual
  // structure without depending on links we don't actually have.
  const [version, setVersion] = React.useState(null);
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await window.primerApi.apiFetch("GET", "/health", null, {});
        if (!cancelled && r?.version) setVersion(r.version);
      } catch {
        /* leave null; we'll render without a version */
      }
    })();
    return () => { cancelled = true; };
  }, []);
  return (
    <div className="auth-foot">
      <span>primer console</span>
      {version && <><span className="sep">·</span><span>v{version}</span></>}
    </div>
  );
}

// Lists enabled SSO providers (Layer 2 OIDC) below the username/password
// form on LoginScreen. Fetches GET /auth/sso/providers on mount; renders
// nothing (no divider, no buttons) when the list is empty or the fetch
// fails, so a console with no SSO providers configured looks exactly as
// before. Each button does a full-page navigation (not an apiFetch call)
// to the backend's OIDC redirect endpoint, which 302s to the provider.
function _SsoButtons() {
  const [providers, setProviders] = React.useState([]);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await window.primerApi.apiFetch("GET", "/auth/sso/providers", null, {});
        if (!cancelled && Array.isArray(r)) setProviders(r);
      } catch {
        /* no providers configured, or the endpoint isn't reachable yet —
           degrade to the plain username/password form. */
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (!providers.length) return null;

  return (
    <div className="auth-sso">
      <div className="auth-or"><span>— or —</span></div>
      {providers.map((p) => (
        <button
          key={p.id}
          type="button"
          className="auth-sso-btn touch-target"
          onClick={() => { window.location.href = "/v1/auth/sso/" + encodeURIComponent(p.id) + "/login"; }}
        >
          Sign in with {p.name}
        </button>
      ))}
    </div>
  );
}

function LoginScreen({ onDone }) {
  const [username, setUsername] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [remember, setRemember] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [server, setServer] = React.useState(null);
  const [fieldErrs, setFieldErrs] = React.useState({});

  const submit = async (e) => {
    e.preventDefault();
    setServer(null);
    const next = {};
    if (!username.trim()) next.username = "username is required";
    if (!password) next.password = "password is required";
    setFieldErrs(next);
    if (Object.keys(next).length > 0) return;

    setBusy(true);
    try {
      await window.primerApi.apiFetch(
        "POST", "/auth/login",
        { username, password, remember }, {},
      );
      onDone();
    } catch (err) {
      setServer(_extractServerError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-shell">
      <div className="auth-wrap">
        <_AuthBrand />
        <div className="auth-card">
          <div className="auth-h">
            <h1 className="title">Sign in to your console</h1>
            <div className="sub">Operator credentials only — no public accounts.</div>
            <_InstancePill />
          </div>
          <form className="auth-body" onSubmit={submit} noValidate>
            <_ServerBanner {...(server || {})} />

            <div className={"auth-field" + (fieldErrs.username ? " has-err" : "")}>
              <label htmlFor="login-username">Username</label>
              <input
                id="login-username"
                className="mono"
                value={username}
                onChange={(e) => { setUsername(e.target.value); if (fieldErrs.username) setFieldErrs((p) => ({ ...p, username: undefined })); }}
                autoFocus
                autoComplete="username"
                placeholder="your operator handle"
              />
              {fieldErrs.username && <div className="field-err">{fieldErrs.username}</div>}
            </div>

            <_PasswordField
              label="Password"
              value={password}
              onChange={(v) => { setPassword(v); if (fieldErrs.password) setFieldErrs((p) => ({ ...p, password: undefined })); }}
              autoComplete="current-password"
              hasErr={!!fieldErrs.password}
              errMsg={fieldErrs.password}
            />

            <label className="auth-remember">
              <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
              <span>Keep me signed in on this device</span>
            </label>

            <button
              type="submit"
              className="auth-submit touch-target"
              disabled={busy || !username || !password}
            >
              {busy ? (<><span className="spinner" /><span>Signing in…</span></>) : <span>Sign in</span>}
            </button>
          </form>
          <_SsoButtons />
        </div>
        <_AuthFooter />
      </div>
    </div>
  );
}

// Shown after login when /v1/auth/status reports must_change_password
// (admin-provisioned accounts — role/must_change_password added to the
// status payload in Task 3). POSTs the new password to
// /v1/auth/change-password (Task 4), which verifies the current password,
// rehashes, and clears the flag; on success we reload into the main app.
function ADM_MustChangePasswordScreen({ onDone }) {
  const [current, setCurrent] = React.useState("");
  const [next, setNext] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [server, setServer] = React.useState(null);
  const [fieldErrs, setFieldErrs] = React.useState({});

  const submit = async (e) => {
    e.preventDefault();
    setServer(null);
    const errs = {};
    if (!current) errs.current = "current password is required";
    if (next.length < 8) errs.next = "value must have at least 8 characters";
    if (next !== confirm) errs.confirm = "passwords don't match";
    setFieldErrs(errs);
    if (Object.keys(errs).length > 0) return;

    setBusy(true);
    try {
      await window.primerApi.apiFetch(
        "POST", "/auth/change-password",
        { current_password: current, new_password: next }, {},
      );
      onDone();
    } catch (err) {
      setServer(_extractServerError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-shell">
      <div className="auth-wrap">
        <_AuthBrand />
        <div className="auth-card">
          <div className="auth-h">
            <h1 className="title">Choose a new password</h1>
            <div className="sub">
              Your account was provisioned with a temporary password. Set your own to continue.
            </div>
            <_InstancePill />
          </div>
          <form className="auth-body" onSubmit={submit} noValidate>
            <_ServerBanner {...(server || {})} />
            <_PasswordField
              label="Current password"
              value={current}
              onChange={(v) => { setCurrent(v); if (fieldErrs.current) setFieldErrs((p) => ({ ...p, current: undefined })); }}
              autoComplete="current-password"
              hasErr={!!fieldErrs.current}
              errMsg={fieldErrs.current}
              autoFocus
            />
            <_PasswordField
              label="New password"
              value={next}
              onChange={(v) => { setNext(v); if (fieldErrs.next) setFieldErrs((p) => ({ ...p, next: undefined })); }}
              placeholder="at least 8 characters"
              autoComplete="new-password"
              hasErr={!!fieldErrs.next}
              errMsg={fieldErrs.next}
            />
            <_PasswordField
              label="Confirm new password"
              value={confirm}
              onChange={(v) => { setConfirm(v); if (fieldErrs.confirm) setFieldErrs((p) => ({ ...p, confirm: undefined })); }}
              autoComplete="new-password"
              hasErr={!!fieldErrs.confirm}
              errMsg={fieldErrs.confirm}
            />
            <button
              type="submit"
              className="auth-submit touch-target"
              disabled={busy || !current || !next || !confirm}
              style={{ marginTop: 6 }}
            >
              {busy ? (<><span className="spinner" /><span>Updating…</span></>) : <span>Update password</span>}
            </button>
          </form>
        </div>
        <_AuthFooter />
      </div>
    </div>
  );
}

// Shown when /v1/auth/status reports role === "restricted": the user is
// authenticated but has no console access yet. Offers only a sign-out.
function ADM_PendingAccessScreen({ username }) {
  const onLogout = async () => {
    try { await window.primerApi.apiFetch("POST", "/auth/logout", null, {}); } catch {}
    window.location.reload();
  };
  return (
    <div className="auth-shell">
      <div className="auth-wrap">
        <_AuthBrand />
        <div className="auth-card">
          <div className="auth-h">
            <h1 className="title">Access pending</h1>
            <div className="sub">
              {username ? <>Signed in as <span className="mono">{username}</span>. </> : null}
              Your account doesn't have console access yet. An administrator must grant you a
              role before you can continue.
            </div>
            <_InstancePill />
          </div>
          <div className="auth-body">
            <button type="button" className="auth-submit touch-target" onClick={onLogout}>
              <span>Sign out</span>
            </button>
          </div>
        </div>
        <_AuthFooter />
      </div>
    </div>
  );
}

window.AuthGate = AuthGate;
window.RegisterScreen = RegisterScreen;
window.LoginScreen = LoginScreen;
window.ADM_MustChangePasswordScreen = ADM_MustChangePasswordScreen;
window.ADM_PendingAccessScreen = ADM_PendingAccessScreen;
