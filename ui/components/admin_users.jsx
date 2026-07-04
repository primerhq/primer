/* global React, Icon, Btn, Modal, Banner */
// Admin Users console page — RBAC user management (Spec §6/§12).
//
// Prefix ADM_ on every top-level name: the server-side JSX bundle
// (primer.api._jsx_bundle) flattens top-level const/let/function into ONE
// shared global scope, so any duplicate name across files clobbers the
// other. Every component is published on window.* at the bottom.
//
// CRUD over /v1/admin/users (require_admin — Task 11). The anti-lockout
// guard (refusing to delete / demote / disable / clear-password the last
// enabled admin) lives server-side; this page just surfaces the error
// envelope it returns. Real authorization is enforced by the backend —
// hiding this page from non-admins (Task 13) is COSMETIC only.
//
// Do NOT destructure window.primerApi at module top level (see
// providers.jsx) — read it inside each render.

// ============================================================================
// Constants
// ============================================================================

// Roles mirror primer/model/user.py::User.role (Literal, Task 2), ordered
// most -> least privileged. "restricted" users are parked on the
// PendingAccessScreen (Task 13) until an admin promotes them.
const ADM_ROLE_OPTIONS = [
  { value: "admin", label: "admin", description: "Full access incl. user management + provider config." },
  { value: "user", label: "user", description: "Standard operator: agents, graphs, chats, workspaces." },
  { value: "restricted", label: "restricted", description: "No console access until promoted." },
];

// ============================================================================
// Helpers
// ============================================================================

// {code, message} out of an ApiError envelope. The server wraps 4xx as
// {detail: {error, message}} (anti-lockout / validation). Mirrors
// AT_extractError in api_tokens.jsx.
function ADM_extractError(err) {
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

// Pill colour per role: admin=green, restricted=red, user=grey.
function ADM_roleClass(role) {
  if (role === "admin") return "pill-claimed";
  if (role === "restricted") return "pill-failed";
  return "pill-paused";
}

// ============================================================================
// ADM_AdminUsersPage — list view + create / edit / delete entry points
// ============================================================================

function ADM_AdminUsersPage() {
  const { useResource, apiFetch } = window.primerApi;
  const [createOpen, setCreateOpen] = React.useState(false);
  const [editUser, setEditUser] = React.useState(null);    // user | null
  const [deleteUser, setDeleteUser] = React.useState(null); // user | null

  const list = useResource(
    "admin-users:list",
    (signal) => apiFetch("GET", "/admin/users", null, { signal }),
    { pollMs: 10000 },
  );

  const items = list.data?.items ?? [];

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <span style={{ fontSize: 13, fontWeight: 600 }}>Users</span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
          <Btn
            size="sm"
            kind="primary"
            icon="plus"
            data-testid="create-user-btn"
            onClick={() => setCreateOpen(true)}
          >
            Create user
          </Btn>
        </div>
      </div>

      {list.loading && items.length === 0 && (
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
      )}
      {list.error && items.length === 0 && (
        <Banner
          kind="error"
          title={list.error.title || "Couldn't load users"}
          detail={list.error.detail || list.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
        />
      )}
      {!list.loading && !list.error && items.length === 0 && (
        <div className="empty" style={{ padding: "40px 20px" }}>
          <div className="ico-wrap"><Icon name="user" size={22} /></div>
          <div className="head">No users yet</div>
          <div className="sub">Create an account to grant console access.</div>
          <div className="actions">
            <Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>Create user</Btn>
          </div>
        </div>
      )}

      {items.length > 0 && (
        <div
          data-testid="admin-users-table"
          className="panel"
          style={{ padding: 0, overflow: "hidden" }}
        >
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Username</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Email</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Role</th>
                <th style={{ textAlign: "left", padding: "8px 12px" }}>Status</th>
                <th style={{ textAlign: "right", padding: "8px 12px" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((u) => (
                <ADM_UserRow
                  key={u.id}
                  user={u}
                  onEdit={() => setEditUser(u)}
                  onDelete={() => setDeleteUser(u)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {createOpen && (
        <ADM_CreateUserDialog
          onClose={() => setCreateOpen(false)}
          onCreated={() => { setCreateOpen(false); list.refetch(); }}
        />
      )}
      {editUser && (
        <ADM_EditUserDialog
          user={editUser}
          onClose={() => setEditUser(null)}
          onSaved={() => { setEditUser(null); list.refetch(); }}
        />
      )}
      {deleteUser && (
        <ADM_DeleteUserDialog
          user={deleteUser}
          onClose={() => setDeleteUser(null)}
          onDeleted={() => { setDeleteUser(null); list.refetch(); }}
        />
      )}
    </div>
  );
}

// ============================================================================
// ADM_UserRow — one row of the table.
// ============================================================================

function ADM_UserRow({ user, onEdit, onDelete }) {
  return (
    <tr
      data-testid={`admin-user-row-${user.id}`}
      style={{ borderTop: "1px solid var(--border)" }}
    >
      <td style={{ padding: "8px 12px", fontWeight: 600 }}>
        <span className="mono">{user.username}</span>
      </td>
      <td style={{ padding: "8px 12px" }}>
        {user.email ? user.email : <span className="muted">—</span>}
      </td>
      <td style={{ padding: "8px 12px" }}>
        <span className={`pill ${ADM_roleClass(user.role)}`} style={{ fontSize: 10.5 }}>
          {user.role}
        </span>
      </td>
      <td style={{ padding: "8px 12px" }}>
        {user.disabled
          ? <span className="pill pill-failed" style={{ fontSize: 10.5 }}>disabled</span>
          : <span className="pill pill-claimed" style={{ fontSize: 10.5 }}>enabled</span>}
        {user.must_change_password && (
          <span className="pill pill-paused" style={{ fontSize: 10.5, marginLeft: 4 }}>must change pw</span>
        )}
      </td>
      <td style={{ padding: "8px 12px", textAlign: "right", whiteSpace: "nowrap" }}>
        <Btn
          size="sm"
          kind="ghost"
          icon="edit"
          onClick={onEdit}
          data-testid={`edit-user-btn-${user.id}`}
        >
          Edit
        </Btn>
        <Btn
          size="sm"
          kind="danger"
          icon="trash"
          onClick={onDelete}
          data-testid={`delete-user-btn-${user.id}`}
          style={{ marginLeft: 6 }}
        >
          Delete
        </Btn>
      </td>
    </tr>
  );
}

// ============================================================================
// ADM_CreateUserDialog — POST /v1/admin/users
// ============================================================================

function ADM_CreateUserDialog({ onClose, onCreated }) {
  const { apiFetch } = window.primerApi;
  const [username, setUsername] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [role, setRole] = React.useState("user");
  const [busy, setBusy] = React.useState(false);
  const [submitError, setSubmitError] = React.useState(null);

  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // Password min length mirrors RegisterBody (Task 2/§6): >= 8 chars. The
  // server flags must_change_password=true when a password is given.
  const canSubmit = !busy && !!username.trim() && password.length >= 8;

  const submit = async () => {
    setSubmitError(null);
    setBusy(true);
    try {
      const body = {
        username: username.trim(),
        email: email.trim() || null,
        password,
        role,
      };
      await apiFetch("POST", "/admin/users", body);
      if (!mountedRef.current) return;
      onCreated && onCreated();
    } catch (err) {
      if (!mountedRef.current) return;
      setSubmitError(ADM_extractError(err));
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };

  return (
    <Modal
      title="Create user"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn
            kind="primary"
            icon="check"
            onClick={submit}
            disabled={!canSubmit}
            data-testid="create-user-submit"
          >
            {busy ? "Creating…" : "Create user"}
          </Btn>
        </>
      }
    >
      <div data-testid="adm-create-form">
        <div className="field">
          <label className="field-label" htmlFor="adm-username">Username</label>
          <input
            id="adm-username"
            className="input mono"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="lowercase letters, digits, . _ -"
            style={{ width: "100%" }}
            autoFocus
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="adm-email">Email <span className="hint">optional</span></label>
          <input
            id="adm-email"
            className="input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="operator@example.com"
            style={{ width: "100%" }}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="adm-password">Password</label>
          <input
            id="adm-password"
            className="input mono"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="at least 8 characters"
            style={{ width: "100%" }}
          />
          <div className="field-help muted text-sm" style={{ marginTop: 4 }}>
            The user must change this on first sign-in.
          </div>
        </div>
        <div className="field">
          <label className="field-label">Role</label>
          <div data-testid="adm-role-options">
            {ADM_ROLE_OPTIONS.map((opt) => (
              <label
                key={opt.value}
                className="row"
                style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "6px 0", cursor: "pointer" }}
              >
                <input
                  type="radio"
                  name="adm-create-role"
                  checked={role === opt.value}
                  onChange={() => setRole(opt.value)}
                />
                <div>
                  <div style={{ fontWeight: 600 }}>{opt.label}</div>
                  <div className="muted text-sm">{opt.description}</div>
                </div>
              </label>
            ))}
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
// ADM_EditUserDialog — PUT /v1/admin/users/{id}
//
// Sends the FULL user row (primer's generic PUT requires body.id, see
// _crud.py:363) with edited role / email / disabled merged in. An optional
// new password re-flags must_change_password server-side. Anti-lockout
// violations (demoting / disabling the last admin) come back as a 4xx
// envelope and render in the banner.
// ============================================================================

function ADM_EditUserDialog({ user, onClose, onSaved }) {
  const { apiFetch } = window.primerApi;
  const [email, setEmail] = React.useState(user.email || "");
  const [role, setRole] = React.useState(user.role || "user");
  const [disabled, setDisabled] = React.useState(!!user.disabled);
  const [password, setPassword] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [submitError, setSubmitError] = React.useState(null);

  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const canSubmit = !busy && (password === "" || password.length >= 8);

  const submit = async () => {
    setSubmitError(null);
    setBusy(true);
    try {
      const body = {
        ...user,
        email: email.trim() || null,
        role,
        disabled,
      };
      if (password) body.password = password;
      await apiFetch("PUT", "/admin/users/" + encodeURIComponent(user.id), body);
      if (!mountedRef.current) return;
      onSaved && onSaved();
    } catch (err) {
      if (!mountedRef.current) return;
      setSubmitError(ADM_extractError(err));
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };

  return (
    <Modal
      title={`Edit user · ${user.username}`}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn
            kind="primary"
            icon="check"
            onClick={submit}
            disabled={!canSubmit}
            data-testid="edit-user-submit"
          >
            {busy ? "Saving…" : "Save changes"}
          </Btn>
        </>
      }
    >
      <div data-testid="adm-edit-form">
        <div className="field">
          <label className="field-label" htmlFor="adm-edit-email">Email <span className="hint">optional</span></label>
          <input
            id="adm-edit-email"
            className="input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="operator@example.com"
            style={{ width: "100%" }}
          />
        </div>
        <div className="field">
          <label className="field-label">Role</label>
          <div data-testid="adm-edit-role-options">
            {ADM_ROLE_OPTIONS.map((opt) => (
              <label
                key={opt.value}
                className="row"
                style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "6px 0", cursor: "pointer" }}
              >
                <input
                  type="radio"
                  name="adm-edit-role"
                  checked={role === opt.value}
                  onChange={() => setRole(opt.value)}
                />
                <div>
                  <div style={{ fontWeight: 600 }}>{opt.label}</div>
                  <div className="muted text-sm">{opt.description}</div>
                </div>
              </label>
            ))}
          </div>
        </div>
        <div className="field">
          <label className="row" style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
            <input type="checkbox" checked={disabled} onChange={(e) => setDisabled(e.target.checked)} />
            <span>Disabled <span className="muted text-sm">— blocks sign-in without deleting the account.</span></span>
          </label>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="adm-edit-password">
            Reset password <span className="hint">optional · leave blank to keep current</span>
          </label>
          <input
            id="adm-edit-password"
            className="input mono"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="at least 8 characters"
            style={{ width: "100%" }}
          />
          <div className="field-help muted text-sm" style={{ marginTop: 4 }}>
            Setting a new password forces a change on the user's next sign-in.
          </div>
        </div>
        {submitError && (
          <Banner
            kind="error"
            title={submitError.code ? `Save failed (${submitError.code})` : "Save failed"}
            detail={submitError.message || ""}
          />
        )}
      </div>
    </Modal>
  );
}

// ============================================================================
// ADM_DeleteUserDialog — DELETE /v1/admin/users/{id}
// ============================================================================

function ADM_DeleteUserDialog({ user, onClose, onDeleted }) {
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
      await apiFetch("DELETE", "/admin/users/" + encodeURIComponent(user.id));
      if (!mountedRef.current) return;
      onDeleted && onDeleted();
    } catch (err) {
      if (!mountedRef.current) return;
      setError(ADM_extractError(err));
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };

  return (
    <Modal
      title={`Delete user · ${user.username}`}
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
            data-testid="delete-user-confirm-btn"
          >
            {busy ? "Deleting…" : "Delete user"}
          </Btn>
        </>
      }
    >
      <div data-testid="adm-delete-confirm">
        <p>This permanently removes <span className="mono">{user.username}</span>. Their sessions stay for audit.</p>
        <ul>
          <li>Any active browser session for this user is invalidated on their next request.</li>
          <li>The server refuses this if it would remove the last enabled admin.</li>
          <li>This action cannot be undone.</li>
        </ul>
        {error && (
          <Banner
            kind="error"
            title={error.code ? `Delete failed (${error.code})` : "Delete failed"}
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

window.ADM_AdminUsersPage = ADM_AdminUsersPage;
window.ADM_UserRow = ADM_UserRow;
window.ADM_CreateUserDialog = ADM_CreateUserDialog;
window.ADM_EditUserDialog = ADM_EditUserDialog;
window.ADM_DeleteUserDialog = ADM_DeleteUserDialog;
