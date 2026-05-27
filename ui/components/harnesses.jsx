/* global React, Icon, Btn, Modal, Banner, StatusPill, JsonSchemaForm, validateSchema */
// Harnesses list + detail + registration wizard.
// Prefix HR_ to avoid global name collisions.

// ============================================================================
// Constants
// ============================================================================

const HR_STATUS_COLORS = {
  DRAFT:     "var(--text-3)",
  READY:     "var(--blue)",
  INSTALLED: "var(--green)",
  OUTDATED:  "var(--amber)",
  ERROR:     "var(--red)",
};

const HR_STATUS_PILL_CLASS = {
  DRAFT:     "pill-paused",
  READY:     "pill-claimed",
  INSTALLED: "pill-ended",
  OUTDATED:  "pill-paused",
  ERROR:     "pill-failed",
};

const HR_SLUG_RE = /^[a-z][a-z0-9-]{1,63}$/;

// The five entity endpoints the detail view cross-queries to list managed objects.
const HR_MANAGED_ENDPOINTS = [
  { label: "Agents",      path: "/agents"     },
  { label: "Graphs",      path: "/graphs"     },
  { label: "Collections", path: "/collections" },
  { label: "Documents",   path: "/documents"  },
  { label: "Toolsets",    path: "/toolsets"   },
];

// ============================================================================
// HarnessesPage — router shim
// ============================================================================

function HarnessesPage({ harnessId }) {
  const { useRouter } = window.matrixApi;
  const { params } = useRouter();
  const id = harnessId || params.id;
  if (id) return <HarnessDetail id={id} />;
  return <HarnessList />;
}

// ============================================================================
// Status badge
// ============================================================================

function HR_StatusBadge({ status }) {
  if (!status) return null;
  const cls = HR_STATUS_PILL_CLASS[status] || "pill-paused";
  return (
    <span className={`pill ${cls}`}>
      <span className="dot" style={{ background: HR_STATUS_COLORS[status] }}></span>
      {status}
    </span>
  );
}

// ============================================================================
// Outdated chips
// ============================================================================

function HR_OutdatedChips({ harness }) {
  const chips = [];
  if (harness.commits_ahead) {
    chips.push(
      <span key="commits" className="pill pill-paused" title="Newer commits available on the remote ref" style={{ fontSize: 10.5 }}>
        commits ahead
      </span>
    );
  }
  if (harness.overrides_dirty) {
    chips.push(
      <span key="overrides" className="pill pill-paused" title="Overrides changed since last install" style={{ fontSize: 10.5 }}>
        overrides dirty
      </span>
    );
  }
  if (harness.schema_missing_input) {
    chips.push(
      <span key="schema" className="pill pill-failed" title="overrides_schema requires fields not yet filled in" style={{ fontSize: 10.5 }}>
        missing inputs
      </span>
    );
  }
  if (chips.length === 0) return null;
  return <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 4 }}>{chips}</div>;
}

// ============================================================================
// HarnessList
// ============================================================================

function HarnessList() {
  const { useResource, useRouter, apiFetch } = window.matrixApi;
  const { navigate } = useRouter();
  const [registerOpen, setRegisterOpen] = React.useState(false);

  const list = useResource(
    "harnesses:list",
    (signal) => apiFetch("GET", "/harnesses?limit=200", null, { signal }),
    { pollMs: null }
  );

  const items = list.data?.items ?? [];

  const onCreated = (harness) => {
    setRegisterOpen(false);
    list.refetch();
    navigate("/harnesses/" + harness.id);
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <span style={{ fontSize: 13, fontWeight: 600 }}>Harnesses</span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setRegisterOpen(true)}>Register</Btn>
        </div>
      </div>

      {list.loading && items.length === 0 && (
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
      )}
      {list.error && items.length === 0 && (
        <Banner
          kind="error"
          title={list.error.title || "Couldn't load harnesses"}
          detail={list.error.detail || list.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
        />
      )}
      {!list.loading && !list.error && items.length === 0 && (
        <div className="empty" style={{ padding: "40px 20px" }}>
          <div className="ico-wrap"><Icon name="box" size={22} /></div>
          <div className="head">No harnesses registered</div>
          <div className="sub">
            Harnesses let you version-control Agents, Graphs, Collections, Documents, and Toolsets
            in a Git repository and deploy them as a single unit.
          </div>
          <div className="actions">
            <Btn kind="primary" icon="plus" onClick={() => setRegisterOpen(true)}>Register harness</Btn>
          </div>
        </div>
      )}

      {items.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 12 }}>
          {items.map((h) => (
            <div
              key={h.id}
              className="panel"
              style={{ cursor: "pointer", transition: "border-color 0.15s" }}
              onClick={() => navigate("/harnesses/" + h.id)}
              onMouseEnter={(e) => e.currentTarget.style.borderColor = "var(--accent)"}
              onMouseLeave={(e) => e.currentTarget.style.borderColor = ""}
            >
              <div className="panel-body" style={{ padding: "12px 14px" }}>
                <div style={{ display: "flex", alignItems: "flex-start", gap: 8, marginBottom: 6 }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600, fontSize: 13.5, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {h.name || h.slug}
                    </div>
                    <div className="mono muted text-sm" style={{ fontSize: 11 }}>{h.slug}</div>
                  </div>
                  <HR_StatusBadge status={h.status} />
                </div>

                {h.description && (
                  <div className="muted text-sm" style={{ marginBottom: 6, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {h.description}
                  </div>
                )}

                <div className="muted text-sm" style={{ fontSize: 11.5, display: "flex", gap: 10 }}>
                  <span><span style={{ color: "var(--text-3)" }}>ref </span><span className="mono">{h.ref || "main"}</span></span>
                  {h.resolved_commit && (
                    <span className="mono" title={h.resolved_commit}>{h.resolved_commit.slice(0, 8)}</span>
                  )}
                </div>

                <HR_OutdatedChips harness={h} />

                {h.last_operation_error && (
                  <div
                    className="muted text-sm"
                    style={{ marginTop: 6, color: "var(--red)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 11 }}
                    title={h.last_operation_error}
                  >
                    {h.last_operation_error}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {registerOpen && (
        <HarnessRegisterDialog
          onClose={() => setRegisterOpen(false)}
          onCreated={onCreated}
        />
      )}
    </div>
  );
}

// ============================================================================
// HarnessDetail
// ============================================================================

function HarnessDetail({ id }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.matrixApi;
  const { navigate } = useRouter();

  const [confirmUninstall, setConfirmUninstall] = React.useState(false);

  const detail = useResource(
    "harness-detail:" + id,
    (signal) => apiFetch("GET", "/harnesses/" + encodeURIComponent(id), null, { signal }),
    {
      pollMs: null,
      deps: [id],
    }
  );

  // Poll every 1s while pending_operation is set
  const polling = detail.data?.pending_operation != null;
  React.useEffect(() => {
    if (!polling) return undefined;
    const timer = setInterval(() => detail.refetch(), 1000);
    return () => clearInterval(timer);
  }, [polling]);

  const fetchMut = useMutation(
    () => apiFetch("POST", "/harnesses/" + encodeURIComponent(id) + "/fetch", {}),
    {
      onSuccess: () => detail.refetch(),
      onError: (err) => {
        // 409 = already pending, just refresh
        detail.refetch();
      },
    }
  );
  const syncMut = useMutation(
    () => apiFetch("POST", "/harnesses/" + encodeURIComponent(id) + "/sync", {}),
    {
      onSuccess: () => detail.refetch(),
      onError: () => detail.refetch(),
    }
  );
  const uninstallMut = useMutation(
    () => apiFetch("DELETE", "/harnesses/" + encodeURIComponent(id)),
    {
      onSuccess: () => navigate("/harnesses"),
      onError: () => detail.refetch(),
    }
  );

  if (detail.loading && !detail.data) {
    return (
      <div className="col" style={{ gap: 14 }}>
        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("/harnesses")}>Back</Btn>
        </div>
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
      </div>
    );
  }
  if (detail.error && !detail.data) {
    return (
      <div className="col" style={{ gap: 14 }}>
        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("/harnesses")}>Back</Btn>
        </div>
        <Banner
          kind="error"
          title={detail.error.title || "Couldn't load harness"}
          detail={detail.error.detail || detail.error.message}
          actions={<Btn size="sm" icon="chevron-left" onClick={() => navigate("/harnesses")}>Back to list</Btn>}
        />
      </div>
    );
  }

  const h = detail.data;
  const isPending = !!h.pending_operation;
  const canFetch = !isPending;
  const canSync = !isPending && (h.status === "INSTALLED" || h.status === "OUTDATED");
  const canUninstall = !isPending;

  return (
    <div className="col" style={{ gap: 14 }}>
      {/* Action bar */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, justifyContent: "space-between" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontWeight: 600, fontSize: 14 }}>{h.name || h.slug}</span>
          <span className="mono muted text-sm">{h.slug}</span>
          <HR_StatusBadge status={h.status} />
          {isPending && (
            <span className="pill pill-claimed">
              <span className="dot"></span>
              {h.pending_operation}…
            </span>
          )}
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <Btn
            size="sm"
            kind="ghost"
            icon="refresh"
            disabled={!canFetch || fetchMut.loading}
            onClick={() => fetchMut.mutate().catch(() => {})}
            title="Enqueue FETCH — pulls the latest commit and updates metadata"
          >
            {fetchMut.loading ? "Fetching…" : "Fetch"}
          </Btn>
          <Btn
            size="sm"
            kind="ghost"
            icon="refresh"
            disabled={!canSync || syncMut.loading}
            onClick={() => syncMut.mutate().catch(() => {})}
            title="Enqueue SYNC — re-apply the installed harness from the fetched commit"
          >
            {syncMut.loading ? "Syncing…" : "Sync"}
          </Btn>
          <Btn
            size="sm"
            kind="danger"
            icon="trash"
            disabled={!canUninstall}
            onClick={() => setConfirmUninstall(true)}
          >
            Uninstall
          </Btn>
          <Btn size="sm" kind="ghost" icon="chevron-left" onClick={() => navigate("/harnesses")}>Back</Btn>
        </div>
      </div>

      {/* Metadata panel */}
      <div className="panel">
        <div className="panel-h"><Icon name="git-commit" size={13} /><span>Metadata</span></div>
        <div className="panel-body" style={{ padding: "8px 14px" }}>
          <dl className="kv" style={{ gridTemplateColumns: "180px 1fr", rowGap: 4 }}>
            <dt>Git URL</dt>
            <dd className="mono">{h.git_url}</dd>
            <dt>Ref</dt>
            <dd className="mono">{h.ref || "main"}</dd>
            {h.subpath && <><dt>Subpath</dt><dd className="mono">{h.subpath}</dd></>}
            {h.resolved_commit && <><dt>Resolved commit</dt><dd className="mono">{h.resolved_commit}</dd></>}
            {h.available_commit && h.available_commit !== h.resolved_commit && (
              <><dt>Available commit</dt><dd className="mono" style={{ color: "var(--amber)" }}>{h.available_commit}</dd></>
            )}
            {h.description && <><dt>Description</dt><dd>{h.description}</dd></>}
          </dl>

          <HR_OutdatedChips harness={h} />

          {h.last_operation_error && (
            <div
              className="panel"
              style={{ marginTop: 10, background: "var(--red-dim)", borderColor: "oklch(0.7 0.2 25 / 0.3)" }}
            >
              <div className="panel-body" style={{ padding: "8px 12px" }}>
                <div style={{ fontWeight: 600, fontSize: 12, color: "var(--red)", marginBottom: 4 }}>Last operation error</div>
                <pre style={{ fontSize: 11, color: "var(--text-2)", margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
                  {h.last_operation_error}
                </pre>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Managed objects */}
      <HR_ManagedObjects harnessId={h.id} slug={h.slug} />

      {/* Confirm uninstall */}
      {confirmUninstall && (
        <Modal
          title={`Uninstall ${h.name || h.slug}?`}
          danger
          onClose={() => setConfirmUninstall(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setConfirmUninstall(false)}>Cancel</Btn>
              <Btn
                kind="danger"
                icon="trash"
                disabled={uninstallMut.loading}
                onClick={async () => {
                  setConfirmUninstall(false);
                  try { await uninstallMut.mutate(); } catch (_e) {}
                }}
              >
                {uninstallMut.loading ? "Uninstalling…" : "Uninstall"}
              </Btn>
            </>
          }
        >
          <ul>
            <li>Enqueues UNINSTALL — the worker cascades-deletes all managed entities.</li>
            <li>The harness row is removed once the worker finishes.</li>
            <li>This action cannot be undone.</li>
          </ul>
        </Modal>
      )}
    </div>
  );
}

// ============================================================================
// HR_ManagedObjects — cross-query the 5 entity endpoints for this harness_id
// ============================================================================

function HR_ManagedObjects({ harnessId, slug }) {
  const { useResource, apiFetch } = window.matrixApi;

  const results = HR_MANAGED_ENDPOINTS.map(({ label, path }) => {
    // Rules of Hooks require a fixed number of hooks per render, so we
    // call useResource for each endpoint unconditionally.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    const res = useResource(
      `harness-managed:${harnessId}:${path}`,
      (signal) => apiFetch("GET", path + "?limit=200", null, { signal }),
      { pollMs: null, deps: [harnessId] }
    );
    const filtered = (res.data?.items ?? []).filter((row) => row.harness_id === harnessId);
    return { label, path, res, filtered };
  });

  const totalManaged = results.reduce((acc, r) => acc + r.filtered.length, 0);

  return (
    <div className="panel">
      <div className="panel-h">
        <Icon name="box" size={13} />
        <span>Managed objects</span>
        {totalManaged > 0 && (
          <span className="muted text-sm" style={{ marginLeft: "auto" }}>{totalManaged} total</span>
        )}
      </div>
      <div className="panel-body" style={{ padding: "4px 0" }}>
        {results.map(({ label, path, res, filtered }) => (
          <div key={path} style={{ borderBottom: "1px solid var(--border)", padding: "8px 14px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: filtered.length > 0 ? 6 : 0 }}>
              <span style={{ fontWeight: 500, fontSize: 12.5, minWidth: 110 }}>{label}</span>
              {res.loading && <span className="muted text-sm">…</span>}
              {!res.loading && filtered.length === 0 && (
                <span className="muted text-sm">none</span>
              )}
              {!res.loading && filtered.length > 0 && (
                <span className="pill pill-ended" style={{ fontSize: 10.5 }}>{filtered.length}</span>
              )}
            </div>
            {filtered.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                {filtered.map((row) => (
                  <span key={row.id} className="mono" style={{ fontSize: 11, color: "var(--text-2)", background: "var(--bg-0)", border: "1px solid var(--border)", borderRadius: 4, padding: "2px 6px" }}>
                    {row.id}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ============================================================================
// HarnessRegisterDialog — two-step wizard
// ============================================================================

function HarnessRegisterDialog({ onClose, onCreated }) {
  const { apiFetch } = window.matrixApi;

  const [step, setStep] = React.useState(1);

  // Step 1 fields
  const [name, setName] = React.useState("");
  const [slug, setSlug] = React.useState("");
  const [ref, setRef] = React.useState("main");
  const [subpath, setSubpath] = React.useState("");
  const [gitUrl, setGitUrl] = React.useState("");
  const [gitToken, setGitToken] = React.useState("");

  // Step 1 state
  const [slugError, setSlugError] = React.useState("");
  const [fetchError, setFetchError] = React.useState("");
  const [step1Busy, setStep1Busy] = React.useState(false);
  const [harness, setHarness] = React.useState(null);    // created harness row

  // Step 2 fields — overrides
  const [overrides, setOverrides] = React.useState({});
  const [overridesErrors, setOverridesErrors] = React.useState([]);

  // Step 2 state
  const [installError, setInstallError] = React.useState("");
  const [step2Busy, setStep2Busy] = React.useState(false);

  // Unmount guard
  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // Auto-slug from name
  const onNameChange = (v) => {
    setName(v);
    if (!slug || slug === HR_autoSlug(name)) {
      setSlug(HR_autoSlug(v));
    }
  };

  const validateSlug = (v) => {
    if (!v) return "Slug is required";
    if (!HR_SLUG_RE.test(v)) return "Slug must be ^[a-z][a-z0-9-]{1,63}$";
    if (v.includes("__")) return "Slug must not contain __";
    return "";
  };

  // ---- Step 1: Create row + enqueue FETCH + poll until READY or ERROR ----
  const doStep1 = async () => {
    const slugErr = validateSlug(slug);
    if (slugErr) { setSlugError(slugErr); return; }
    setSlugError("");
    setFetchError("");
    setStep1Busy(true);
    try {
      // 1. Create DRAFT
      const body = { name: name || slug, slug, git_url: gitUrl, ref: ref || "main" };
      if (subpath) body.subpath = subpath;
      if (gitToken) body.git_token = gitToken;
      const created = await apiFetch("POST", "/harnesses", body);
      if (!mountedRef.current) return;

      // 2. Enqueue FETCH
      await apiFetch("POST", "/harnesses/" + encodeURIComponent(created.id) + "/fetch", {});
      if (!mountedRef.current) return;

      // 3. Poll until status != DRAFT and pending_operation is null
      const polled = await HR_pollUntilDone(apiFetch, created.id, (row) => {
        // Done when: no pending_operation AND status is no longer DRAFT
        return row.pending_operation == null && row.status !== "DRAFT";
      });
      if (!mountedRef.current) return;

      if (polled.status === "ERROR") {
        setFetchError(polled.last_operation_error || "Fetch failed");
        setStep1Busy(false);
        return;
      }

      setHarness(polled);
      // Seed overrides with any default values from the schema
      if (polled.overrides && typeof polled.overrides === "object") {
        setOverrides(polled.overrides);
      }
      setStep(2);
    } catch (err) {
      if (mountedRef.current) {
        setFetchError(err.detail || err.title || err.message || "Request failed");
      }
    } finally {
      if (mountedRef.current) setStep1Busy(false);
    }
  };

  // ---- Step 2: PUT overrides + POST install + poll until INSTALLED or ERROR ----
  const doStep2 = async () => {
    if (!harness) return;
    const schema = harness.overrides_schema;
    const errs = schema ? validateSchema(schema, overrides) : [];
    if (errs.length > 0) { setOverridesErrors(errs); return; }
    setOverridesErrors([]);
    setInstallError("");
    setStep2Busy(true);
    try {
      // 1. PUT overrides
      await apiFetch("PUT", "/harnesses/" + encodeURIComponent(harness.id) + "/overrides", overrides);
      if (!mountedRef.current) return;

      // 2. POST install
      await apiFetch("POST", "/harnesses/" + encodeURIComponent(harness.id) + "/install", {});
      if (!mountedRef.current) return;

      // 3. Poll until installed or error
      const polled = await HR_pollUntilDone(apiFetch, harness.id, (row) => {
        return row.pending_operation == null &&
          (row.status === "INSTALLED" || row.status === "ERROR");
      });
      if (!mountedRef.current) return;

      if (polled.status === "ERROR") {
        setInstallError(polled.last_operation_error || "Install failed");
        setStep2Busy(false);
        return;
      }

      onCreated(polled);
    } catch (err) {
      if (mountedRef.current) {
        setInstallError(err.detail || err.title || err.message || "Request failed");
      }
    } finally {
      if (mountedRef.current) setStep2Busy(false);
    }
  };

  const schema = harness?.overrides_schema;
  const step2Valid = !schema || validateSchema(schema, overrides).length === 0;

  return (
    <Modal
      title={step === 1 ? "Register harness — Step 1: Source" : "Register harness — Step 2: Overrides"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          {step === 1 && (
            <Btn
              kind="primary"
              icon="refresh"
              onClick={doStep1}
              disabled={step1Busy || !gitUrl}
            >
              {step1Busy ? "Fetching…" : "Fetch"}
            </Btn>
          )}
          {step === 2 && (
            <>
              <Btn kind="ghost" onClick={() => setStep(1)} disabled={step2Busy}>Back</Btn>
              <Btn
                kind="primary"
                icon="check"
                onClick={doStep2}
                disabled={step2Busy || !step2Valid}
              >
                {step2Busy ? "Installing…" : "Create"}
              </Btn>
            </>
          )}
        </>
      }
    >
      {step === 1 && (
        <>
          <div className="field">
            <label className="field-label" htmlFor="hr-name">Name</label>
            <input
              id="hr-name"
              className="input"
              value={name}
              onChange={(e) => onNameChange(e.target.value)}
              placeholder="My harness"
              style={{ width: "100%" }}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="hr-slug">Slug <span className="hint">used as id prefix for managed entities</span></label>
            <input
              id="hr-slug"
              className="input mono"
              value={slug}
              onChange={(e) => { setSlug(e.target.value); setSlugError(validateSlug(e.target.value)); }}
              placeholder="my-harness"
              style={{ width: "100%" }}
            />
            {slugError && <div className="field-help" style={{ color: "var(--red)" }}>{slugError}</div>}
          </div>
          <div className="field">
            <label className="field-label" htmlFor="hr-git-url">Git URL <span className="hint">HTTPS only</span></label>
            <input
              id="hr-git-url"
              className="input mono"
              value={gitUrl}
              onChange={(e) => setGitUrl(e.target.value)}
              placeholder="https://github.com/org/repo"
              style={{ width: "100%" }}
            />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
            <div className="field">
              <label className="field-label" htmlFor="hr-ref">Ref <span className="hint">branch / tag / SHA</span></label>
              <input
                id="hr-ref"
                className="input mono"
                value={ref}
                onChange={(e) => setRef(e.target.value)}
                placeholder="main"
                style={{ width: "100%" }}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="hr-subpath">Subpath <span className="hint">optional</span></label>
              <input
                id="hr-subpath"
                className="input mono"
                value={subpath}
                onChange={(e) => setSubpath(e.target.value)}
                placeholder="harness/"
                style={{ width: "100%" }}
              />
            </div>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="hr-token">Git token <span className="hint">optional · stored encrypted</span></label>
            <input
              id="hr-token"
              className="input"
              type="password"
              value={gitToken}
              onChange={(e) => setGitToken(e.target.value)}
              style={{ width: "100%" }}
            />
          </div>
          {fetchError && (
            <Banner kind="error" title="Fetch failed" detail={fetchError} />
          )}
        </>
      )}

      {step === 2 && (
        <>
          <div className="field-help" style={{ marginBottom: 12 }}>
            Harness <span className="mono">{harness?.slug}</span> fetched successfully
            {harness?.available_commit && <> · commit <span className="mono">{harness.available_commit.slice(0, 8)}</span></>}.
            Fill in the overrides below, then click Create to install.
          </div>

          {schema ? (
            <JsonSchemaForm
              schema={schema}
              value={overrides}
              onChange={setOverrides}
              errors={overridesErrors}
            />
          ) : (
            <div className="muted text-sm" style={{ padding: "12px 0" }}>
              This harness has no overrides schema — no configuration required.
            </div>
          )}

          {overridesErrors.length > 0 && (
            <Banner
              kind="error"
              title="Validation errors"
              detail={overridesErrors.map((e) => `${e.path}: ${e.message}`).join("; ")}
            />
          )}
          {installError && (
            <Banner kind="error" title="Install failed" detail={installError} />
          )}
        </>
      )}
    </Modal>
  );
}

// ============================================================================
// Utilities
// ============================================================================

function HR_autoSlug(str) {
  return (str || "")
    .toLowerCase()
    .replace(/[^a-z0-9-]/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63);
}

async function HR_pollUntilDone(apiFetch, id, predicate, { maxMs = 120000, intervalMs = 1000 } = {}) {
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    const row = await apiFetch("GET", "/harnesses/" + encodeURIComponent(id));
    if (predicate(row)) return row;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error("Timed out waiting for harness operation to complete");
}

// ============================================================================
// Exports
// ============================================================================

window.HarnessesPage = HarnessesPage;
window.HarnessList = HarnessList;
window.HarnessDetail = HarnessDetail;
window.HarnessRegisterDialog = HarnessRegisterDialog;
