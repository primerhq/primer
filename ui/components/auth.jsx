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
  if (status.authenticated) return children;
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
        </div>
        <_AuthFooter />
      </div>
    </div>
  );
}

window.AuthGate = AuthGate;
window.RegisterScreen = RegisterScreen;
window.LoginScreen = LoginScreen;
