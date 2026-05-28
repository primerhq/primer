// Auth screens — Register (first boot) and Login (subsequent boots).
//
// Loaded by ui/app.jsx via window.AuthGate, which decides which screen
// to render based on /v1/auth/status:
//   - has_user=false                  → <RegisterScreen />
//   - has_user=true, authenticated=false → <LoginScreen />
//   - authenticated=true              → main app
//
// Both screens POST to /v1/auth/{register,login} and reload on success
// (the backend's Set-Cookie carries the signed session token).

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
      <div style={_centerStyle}>
        <div className="muted">Loading…</div>
      </div>
    );
  }
  if (status.authenticated) return children;
  if (!status.has_user) return <RegisterScreen onDone={() => window.location.reload()} />;
  return <LoginScreen onDone={() => window.location.reload()} />;
}

function RegisterScreen({ onDone }) {
  const [username, setUsername] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    if (password !== confirm) {
      setError("passwords don't match");
      return;
    }
    if (password.length < 8) {
      setError("password must be at least 8 characters");
      return;
    }
    setBusy(true);
    try {
      await window.primerApi.apiFetch(
        "POST", "/auth/register",
        { username, password }, {},
      );
      onDone();
    } catch (err) {
      const detail = err?.detail ?? err?.message ?? "register failed";
      setError(typeof detail === "string" ? detail : JSON.stringify(detail));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={_centerStyle}>
      <form onSubmit={submit} style={_cardStyle}>
        <h2 style={{ margin: 0, fontWeight: 600 }}>Welcome to Primer</h2>
        <p className="muted" style={{ marginTop: 6, marginBottom: 18, fontSize: 13 }}>
          Create the operator account for this install. This is the only
          account; you can add more in a future release.
        </p>
        <_AuthField label="Username">
          <input
            className="input mono"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            autoComplete="username"
            placeholder="lowercase letters, digits, . _ -"
            style={_inputStyle}
          />
        </_AuthField>
        <_AuthField label="Password">
          <input
            className="input mono"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            placeholder="at least 8 characters"
            style={_inputStyle}
          />
        </_AuthField>
        <_AuthField label="Confirm password">
          <input
            className="input mono"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            style={_inputStyle}
          />
        </_AuthField>
        {error && <div className="err" style={_errStyle}>{error}</div>}
        <button
          type="submit"
          className="btn btn-primary"
          style={{ marginTop: 16, width: "100%" }}
          disabled={busy || !username || !password}
        >
          {busy ? "Creating…" : "Create account"}
        </button>
      </form>
    </div>
  );
}

function LoginScreen({ onDone }) {
  const [username, setUsername] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await window.primerApi.apiFetch(
        "POST", "/auth/login",
        { username, password }, {},
      );
      onDone();
    } catch (err) {
      const status = err?.status;
      if (status === 401) {
        setError("invalid username or password");
      } else {
        const detail = err?.detail ?? err?.message ?? "login failed";
        setError(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={_centerStyle}>
      <form onSubmit={submit} style={_cardStyle}>
        <h2 style={{ margin: 0, fontWeight: 600 }}>Sign in to Primer</h2>
        <p className="muted" style={{ marginTop: 6, marginBottom: 18, fontSize: 13 }}>
          Enter your operator credentials.
        </p>
        <_AuthField label="Username">
          <input
            className="input mono"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            autoComplete="username"
            style={_inputStyle}
          />
        </_AuthField>
        <_AuthField label="Password">
          <input
            className="input mono"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            style={_inputStyle}
          />
        </_AuthField>
        {error && <div className="err" style={_errStyle}>{error}</div>}
        <button
          type="submit"
          className="btn btn-primary"
          style={{ marginTop: 16, width: "100%" }}
          disabled={busy || !username || !password}
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

function _AuthField({ label, children }) {
  return (
    <label style={{ display: "block", marginTop: 12 }}>
      <div style={{ fontSize: 11, color: "var(--text-2)", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.04em" }}>
        {label}
      </div>
      {children}
    </label>
  );
}

const _centerStyle = {
  display: "flex",
  minHeight: "100vh",
  alignItems: "center",
  justifyContent: "center",
  background: "var(--bg)",
  padding: 20,
};

const _cardStyle = {
  background: "var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: 28,
  width: "100%",
  maxWidth: 380,
  boxShadow: "0 6px 24px rgba(0,0,0,0.10)",
};

const _inputStyle = { width: "100%", boxSizing: "border-box" };

const _errStyle = {
  marginTop: 14,
  padding: "8px 10px",
  borderRadius: 4,
  fontSize: 12,
  background: "oklch(0.7 0.2 25 / 0.10)",
  border: "1px solid oklch(0.7 0.2 25 / 0.30)",
  color: "oklch(0.7 0.2 25)",
};

window.AuthGate = AuthGate;
window.RegisterScreen = RegisterScreen;
window.LoginScreen = LoginScreen;
