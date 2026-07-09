/* global React, Icon, Btn, Modal, Banner */
// HarnessOutboundBuilder — four-step wizard for building an outbound harness
// from local entities. Spec B §11.2 / Plan B Phase 8.
//
// Steps:
//   1. Metadata     — name, slug, description, ref, subpath, git_url, git_token.
//   2. Entities     — pick agents / graphs / toolsets / collections to track.
//   3. Templatize   — for each tracked entity, render JSON tree + add override
//                     mappings via a per-leaf modal (override_path + widget).
//   4. Link & push  — review, POST /v1/harnesses (direction=outbound),
//                     POST /build, optionally POST /push. Polls until done.
//
// Globals prefixed HOB_ to avoid collisions.

// ============================================================================
// Constants
// ============================================================================

const HOB_SLUG_RE = /^[a-z][a-z0-9-]{1,63}$/;
const HOB_TEMPLATE_NAME_RE = /^[a-z][a-z0-9_-]{0,63}$/;

const HOB_ENTITY_KINDS = [
  { kind: "agent",      label: "Agents",      list: "/agents",      detail: "/agents" },
  { kind: "graph",      label: "Graphs",      list: "/graphs",      detail: "/graphs" },
  { kind: "toolset",    label: "Toolsets",    list: "/toolsets",    detail: "/toolsets" },
  { kind: "collection", label: "Collections", list: "/collections", detail: "/collections" },
];

const HOB_WIDGET_CHOICES = [
  { value: "",                         label: "none" },
  { value: "llm-provider-picker",      label: "llm-provider-picker" },
  { value: "embedding-provider-picker", label: "embedding-provider-picker" },
  { value: "ssp-picker",               label: "ssp-picker" },
  { value: "cross-encoder-picker",     label: "cross-encoder-picker" },
];

const HOB_MAX_TREE_DEPTH = 3;

// ============================================================================
// Utilities
// ============================================================================

function HOB_autoSlug(str) {
  return (str || "")
    .toLowerCase()
    .replace(/[^a-z0-9-]/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63);
}

function HOB_autoTemplateName(str) {
  return (str || "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 63);
}

async function HOB_pollUntilDone(apiFetch, id, predicate, { maxMs = 120000, intervalMs = 1000 } = {}) {
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    const row = await apiFetch("GET", "/harnesses/" + encodeURIComponent(id));
    if (predicate(row)) return row;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error("Timed out waiting for harness operation to complete");
}

// ============================================================================
// HarnessOutboundBuilder — top-level component
// ============================================================================

function HarnessOutboundBuilder({ onClose, onCreated, initialStep, initialHarness }) {
  const { apiFetch } = window.primerApi;

  const [step, setStep] = React.useState(initialStep || 1);

  // Step 1 — metadata
  const [name, setName] = React.useState(initialHarness?.name || "");
  const [slug, setSlug] = React.useState(initialHarness?.slug || "");
  const [description, setDescription] = React.useState(initialHarness?.description || "");
  const [ref, setRef] = React.useState(initialHarness?.ref || "main");
  const [subpath, setSubpath] = React.useState(initialHarness?.subpath || "");
  const [gitUrl, setGitUrl] = React.useState(initialHarness?.git_url || "");
  const [gitToken, setGitToken] = React.useState("");
  const [slugError, setSlugError] = React.useState("");
  const [slugChecking, setSlugChecking] = React.useState(false);

  // Step 2 — tracked entities. Array of {kind, source_id, template_name, overrides}.
  const [tracked, setTracked] = React.useState(initialHarness?.tracked_entities || []);

  // Step 3 — payload cache per (kind:id) for the templatize tree.
  const [payloads, setPayloads] = React.useState({});

  // Step 4 — final create/push state.
  const [pushNow, setPushNow] = React.useState(false);
  const [createBusy, setCreateBusy] = React.useState(false);
  const [createError, setCreateError] = React.useState("");
  const [createdHarness, setCreatedHarness] = React.useState(initialHarness || null);

  const mountedRef = React.useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // Auto-slug
  const onNameChange = (v) => {
    setName(v);
    if (!slug || slug === HOB_autoSlug(name)) {
      setSlug(HOB_autoSlug(v));
    }
  };

  // Client-side slug uniqueness check — GET /v1/harnesses?slug=
  const debouncedSlugRef = React.useRef(null);
  React.useEffect(() => {
    if (!slug) { setSlugError(""); return; }
    if (!HOB_SLUG_RE.test(slug)) {
      setSlugError("Slug must be ^[a-z][a-z0-9-]{1,63}$");
      return;
    }
    setSlugError("");
    if (debouncedSlugRef.current) clearTimeout(debouncedSlugRef.current);
    debouncedSlugRef.current = setTimeout(async () => {
      setSlugChecking(true);
      try {
        const res = await apiFetch("GET", "/harnesses?slug=" + encodeURIComponent(slug));
        const exists = (res.items || []).some((row) => row.slug === slug && row.id !== createdHarness?.id);
        if (mountedRef.current && exists) setSlugError("Slug already in use");
      } catch (_err) {
        // Ignore — server-side check on submit will catch it.
      } finally {
        if (mountedRef.current) setSlugChecking(false);
      }
    }, 350);
    return () => { if (debouncedSlugRef.current) clearTimeout(debouncedSlugRef.current); };
  }, [slug]);

  const step1Valid = name && slug && gitUrl && !slugError;

  // Step 2 — unique template_names
  const templateNameSet = new Set();
  let dupTemplate = false;
  tracked.forEach((te) => {
    if (templateNameSet.has(te.template_name)) dupTemplate = true;
    templateNameSet.add(te.template_name);
  });
  const step2Valid = tracked.length > 0 && !dupTemplate && tracked.every(
    (te) => HOB_TEMPLATE_NAME_RE.test(te.template_name || "")
  );

  // ---- Step 4: Create harness, build, optionally push ----
  // When `initialHarness` was passed (edit-tracked-entities flow), we PUT
  // /tracked_entities on the existing row instead of creating a new harness.
  const doCreate = async () => {
    setCreateBusy(true);
    setCreateError("");
    try {
      let created;
      if (initialHarness?.id) {
        // Edit path — PUT /v1/harnesses/{id}/tracked_entities
        await apiFetch(
          "PUT",
          "/harnesses/" + encodeURIComponent(initialHarness.id) + "/tracked_entities",
          { tracked_entities: tracked },
        );
        if (!mountedRef.current) return;
        created = await apiFetch("GET", "/harnesses/" + encodeURIComponent(initialHarness.id));
      } else {
        const body = {
          name: name || slug,
          slug,
          direction: "outbound",
          git_url: gitUrl,
          ref: ref || "main",
          tracked_entities: tracked,
        };
        if (description) body.description = description;
        if (subpath) body.subpath = subpath;
        if (gitToken) body.git_token = gitToken;

        created = await apiFetch("POST", "/harnesses", body);
      }
      if (!mountedRef.current) return;
      setCreatedHarness(created);

      // Enqueue BUILD
      await apiFetch("POST", "/harnesses/" + encodeURIComponent(created.id) + "/build", {});
      if (!mountedRef.current) return;

      // Poll until BUILD is no longer pending
      let polled = await HOB_pollUntilDone(apiFetch, created.id, (row) => row.pending_operation == null);
      if (!mountedRef.current) return;

      if (polled.status === "ERROR" || polled.last_operation_error) {
        const err = polled.last_operation_error;
        setCreateError(typeof err === "string" ? err : (err?.code ? err.code + ": " + (err.message || "") : "Build failed"));
        setCreateBusy(false);
        return;
      }

      if (pushNow) {
        await apiFetch("POST", "/harnesses/" + encodeURIComponent(created.id) + "/push", {});
        if (!mountedRef.current) return;
        polled = await HOB_pollUntilDone(apiFetch, created.id, (row) => row.pending_operation == null);
        if (!mountedRef.current) return;
        if (polled.status === "ERROR" || polled.last_operation_error) {
          const err = polled.last_operation_error;
          setCreateError(typeof err === "string" ? err : (err?.code ? err.code + ": " + (err.message || "") : "Push failed"));
          setCreateBusy(false);
          return;
        }
      }

      onCreated(polled);
    } catch (err) {
      if (mountedRef.current) {
        const detail = err.detail || err.title || err.message || "Request failed";
        setCreateError(detail);
      }
    } finally {
      if (mountedRef.current) setCreateBusy(false);
    }
  };

  const stepTitle = {
    1: "Metadata",
    2: "Entities",
    3: "Templatize",
    4: "Link & push",
  }[step];

  return (
    <Modal
      title={`Build outbound harness — Step ${step}: ${stepTitle}`}
      onClose={onClose}
      footer={
        <div style={{ display: "flex", gap: 6, width: "100%" }}>
          <div data-testid="hob-step-indicator" style={{ display: "flex", gap: 4, fontSize: 11, color: "var(--text-3)", alignItems: "center" }}>
            <span className={step === 1 ? "" : "muted"}>Metadata</span>
            <span>/</span>
            <span className={step === 2 ? "" : "muted"}>Entities</span>
            <span>/</span>
            <span className={step === 3 ? "" : "muted"}>Templatize</span>
            <span>/</span>
            <span className={step === 4 ? "" : "muted"}>Link</span>
          </div>
          <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
            <Btn kind="ghost" onClick={onClose} disabled={createBusy}>Cancel</Btn>
            {step > 1 && (
              <Btn kind="ghost" onClick={() => setStep(step - 1)} disabled={createBusy}>Back</Btn>
            )}
            {step === 1 && (
              <Btn kind="primary" icon="chevron-right" onClick={() => setStep(2)} disabled={!step1Valid || slugChecking}>Next</Btn>
            )}
            {step === 2 && (
              <Btn kind="primary" icon="chevron-right" onClick={() => setStep(3)} disabled={!step2Valid}>Next</Btn>
            )}
            {step === 3 && (
              <Btn kind="primary" icon="chevron-right" onClick={() => setStep(4)}>Next</Btn>
            )}
            {step === 4 && (
              <Btn kind="primary" icon="check" onClick={doCreate} disabled={createBusy}>
                {createBusy ? "Creating…" : "Create"}
              </Btn>
            )}
          </div>
        </div>
      }
    >
      {step === 1 && (
        <HOB_Step1Metadata
          name={name} onNameChange={onNameChange}
          slug={slug} onSlugChange={(v) => setSlug(v)}
          slugError={slugError} slugChecking={slugChecking}
          description={description} onDescriptionChange={setDescription}
          ref_={ref} onRefChange={setRef}
          subpath={subpath} onSubpathChange={setSubpath}
          gitUrl={gitUrl} onGitUrlChange={setGitUrl}
          gitToken={gitToken} onGitTokenChange={setGitToken}
        />
      )}

      {step === 2 && (
        <HOB_Step2Entities
          tracked={tracked}
          onTrackedChange={setTracked}
          dupTemplate={dupTemplate}
        />
      )}

      {step === 3 && (
        <HOB_Step3Templatize
          tracked={tracked}
          onTrackedChange={setTracked}
          payloads={payloads}
          onPayloadsChange={setPayloads}
        />
      )}

      {step === 4 && (
        <HOB_Step4Link
          name={name}
          slug={slug}
          description={description}
          ref_={ref}
          subpath={subpath}
          gitUrl={gitUrl}
          tracked={tracked}
          pushNow={pushNow}
          onPushNowChange={setPushNow}
          createError={createError}
        />
      )}
    </Modal>
  );
}

// ============================================================================
// Step 1 — Metadata
// ============================================================================

function HOB_Step1Metadata({
  name, onNameChange,
  slug, onSlugChange, slugError, slugChecking,
  description, onDescriptionChange,
  ref_, onRefChange,
  subpath, onSubpathChange,
  gitUrl, onGitUrlChange,
  gitToken, onGitTokenChange,
}) {
  return (
    <>
      <div className="field">
        <label className="field-label" htmlFor="hob-name">Name</label>
        <input
          id="hob-name"
          className="input"
          value={name}
          onChange={(e) => onNameChange(e.target.value)}
          placeholder="My outbound harness"
          style={{ width: "100%" }}
        />
      </div>
      <div className="field">
        <label className="field-label" htmlFor="hob-slug">
          Slug <span className="hint">unique across harnesses</span>
        </label>
        <input
          id="hob-slug"
          className="input mono"
          value={slug}
          onChange={(e) => onSlugChange(e.target.value)}
          placeholder="my-outbound-harness"
          style={{ width: "100%" }}
        />
        {slugChecking && <div className="field-help muted">Checking availability…</div>}
        {slugError && <div className="field-help" style={{ color: "var(--red)" }}>{slugError}</div>}
      </div>
      <div className="field">
        <label className="field-label" htmlFor="hob-description">Description <span className="hint">optional</span></label>
        <input
          id="hob-description"
          className="input"
          value={description}
          onChange={(e) => onDescriptionChange(e.target.value)}
          style={{ width: "100%" }}
        />
      </div>
      <div className="field">
        <label className="field-label" htmlFor="hob-git-url">Git URL <span className="hint">HTTPS</span></label>
        <input
          id="hob-git-url"
          className="input mono"
          value={gitUrl}
          onChange={(e) => onGitUrlChange(e.target.value)}
          placeholder="https://github.com/org/repo"
          style={{ width: "100%" }}
        />
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
        <div className="field">
          <label className="field-label" htmlFor="hob-ref">Ref <span className="hint">branch / tag</span></label>
          <input
            id="hob-ref"
            className="input mono"
            value={ref_}
            onChange={(e) => onRefChange(e.target.value)}
            placeholder="main"
            style={{ width: "100%" }}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="hob-subpath">Subpath <span className="hint">optional</span></label>
          <input
            id="hob-subpath"
            className="input mono"
            value={subpath}
            onChange={(e) => onSubpathChange(e.target.value)}
            placeholder="harness/"
            style={{ width: "100%" }}
          />
        </div>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="hob-git-token">
          Git token <span className="hint">optional · stored encrypted · used for push auth</span>
        </label>
        <input
          id="hob-git-token"
          className="input"
          type="password"
          value={gitToken}
          onChange={(e) => onGitTokenChange(e.target.value)}
          style={{ width: "100%" }}
        />
      </div>
    </>
  );
}

// ============================================================================
// Step 2 — Pick entities
// ============================================================================

function HOB_Step2Entities({ tracked, onTrackedChange, dupTemplate }) {
  const { useResource, apiFetch } = window.primerApi;

  // Parallel fetches for the four entity kinds.
  const results = HOB_ENTITY_KINDS.map(({ kind, label, list }) => {
    // eslint-disable-next-line react-hooks/rules-of-hooks
    const res = useResource(
      "hob-list:" + kind,
      (signal) => apiFetch("GET", list + "?limit=200", null, { signal }),
      { pollMs: null }
    );
    return { kind, label, list, res };
  });

  // Index tracked entities by `${kind}:${source_id}` for quick lookup.
  const trackedKey = (kind, id) => kind + ":" + id;
  const trackedIndex = new Map(tracked.map((te) => [trackedKey(te.kind, te.source_id), te]));

  const toggle = (kind, row) => {
    const key = trackedKey(kind, row.id);
    if (trackedIndex.has(key)) {
      onTrackedChange(tracked.filter((te) => trackedKey(te.kind, te.source_id) !== key));
    } else {
      const defaultName = HOB_autoTemplateName(row.name || row.slug || row.id);
      onTrackedChange([
        ...tracked,
        { kind, source_id: row.id, template_name: defaultName, overrides: [] },
      ]);
    }
  };

  const setTemplateName = (kind, sourceId, value) => {
    onTrackedChange(tracked.map((te) => (
      te.kind === kind && te.source_id === sourceId
        ? { ...te, template_name: value }
        : te
    )));
  };

  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="field-help">
        Pick local agents, graphs, toolsets, and collections to track. Entities already
        managed by an inbound harness are greyed out — you can't track them outbound.
        Set each tracked entity's <span className="mono">template_name</span> to a unique
        slug — this becomes its filename in the bundle.
      </div>

      {dupTemplate && (
        <Banner kind="error" title="Template name collision" detail="Two tracked entities share the same template_name. Names must be unique." />
      )}

      {results.map(({ kind, label, res }) => {
        const items = res.data?.items ?? [];
        return (
          <div key={kind} className="panel" data-testid={"hob-entity-section-" + kind}>
            <div className="panel-h">
              <Icon name="box" size={13} />
              <span>{label}</span>
              {res.loading && <span className="muted text-sm">…</span>}
              {!res.loading && <span className="muted text-sm" style={{ marginLeft: "auto" }}>{items.length}</span>}
            </div>
            <div className="panel-body" style={{ padding: "4px 0" }}>
              {items.length === 0 && !res.loading && (
                <div className="muted text-sm" style={{ padding: "8px 14px" }}>none</div>
              )}
              {items.map((row) => {
                const key = trackedKey(kind, row.id);
                const te = trackedIndex.get(key);
                const selected = !!te;
                const managed = row.harness_id != null;
                return (
                  <div
                    key={row.id}
                    style={{
                      borderBottom: "1px solid var(--border)",
                      padding: "6px 14px",
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      opacity: managed ? 0.5 : 1,
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={selected}
                      disabled={managed}
                      onChange={() => toggle(kind, row)}
                      aria-label={`Track ${row.id}`}
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 12.5, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {row.name || row.slug || row.id}
                      </div>
                      <div className="mono muted text-sm" style={{ fontSize: 10.5 }}>{row.id}</div>
                    </div>
                    {managed && (
                      <span className="pill pill-paused" style={{ fontSize: 10 }} title={"managed by harness " + row.harness_id}>
                        managed
                      </span>
                    )}
                    {selected && (
                      <input
                        className="input mono"
                        value={te.template_name}
                        onChange={(e) => setTemplateName(kind, row.id, e.target.value)}
                        placeholder="template_name"
                        style={{ width: 180, fontSize: 11.5 }}
                        aria-label={`template_name for ${row.id}`}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ============================================================================
// Step 3 — Templatize
// ============================================================================

function HOB_Step3Templatize({ tracked, onTrackedChange, payloads, onPayloadsChange }) {
  const { apiFetch } = window.primerApi;

  // Modal state — what field is being templatized right now.
  // {kind, source_id, field_path, defaultValue} or null.
  const [modalTarget, setModalTarget] = React.useState(null);

  // Lazy-fetch payload for any tracked entity we don't yet have.
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      for (const te of tracked) {
        const key = te.kind + ":" + te.source_id;
        if (payloads[key]) continue;
        const path = `/${te.kind}s/${encodeURIComponent(te.source_id)}`;
        try {
          const data = await apiFetch("GET", path);
          if (cancelled) return;
          onPayloadsChange((prev) => ({ ...prev, [key]: data }));
        } catch (err) {
          if (cancelled) return;
          onPayloadsChange((prev) => ({ ...prev, [key]: { __error: err.detail || err.message || "fetch failed" } }));
        }
      }
    })();
    return () => { cancelled = true; };
  }, [tracked]);

  const addMapping = (kind, sourceId, fieldPath, overridePath, widget, defaultValue) => {
    onTrackedChange(tracked.map((te) => {
      if (te.kind !== kind || te.source_id !== sourceId) return te;
      // Remove any existing mapping at the same field_path, then append.
      const filtered = (te.overrides || []).filter((m) => m.field_path !== fieldPath);
      const mapping = {
        field_path: fieldPath,
        override_path: overridePath,
        default: defaultValue,
      };
      if (widget) mapping.widget = widget;
      return { ...te, overrides: [...filtered, mapping] };
    }));
    setModalTarget(null);
  };

  const removeMapping = (kind, sourceId, fieldPath) => {
    onTrackedChange(tracked.map((te) => {
      if (te.kind !== kind || te.source_id !== sourceId) return te;
      return { ...te, overrides: (te.overrides || []).filter((m) => m.field_path !== fieldPath) };
    }));
  };

  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="field-help">
        Click <strong>Templatize</strong> on any field to make it configurable at install time.
        Set <span className="mono">override_path</span> (where the value lives in the harness's
        overrides) and optionally pick a <span className="mono">widget</span> for the install UI.
        Existing mappings appear as blue badges — click to remove.
      </div>

      {tracked.map((te) => {
        const key = te.kind + ":" + te.source_id;
        const payload = payloads[key];
        const overrides = te.overrides || [];
        return (
          <div key={key} className="panel" data-testid={"hob-tree-" + te.template_name}>
            <div className="panel-h">
              <span style={{ fontWeight: 600 }}>{te.template_name}</span>
              <span className="mono muted text-sm" style={{ fontSize: 11 }}>{te.kind} · {te.source_id}</span>
              <span className="muted text-sm" style={{ marginLeft: "auto" }}>{overrides.length} mappings</span>
            </div>
            <div className="panel-body" style={{ padding: "4px 0" }}>
              {!payload && <div className="muted text-sm" style={{ padding: "8px 14px" }}>Loading…</div>}
              {payload?.__error && (
                <div className="muted text-sm" style={{ padding: "8px 14px", color: "var(--red)" }}>
                  {payload.__error}
                </div>
              )}
              {payload && !payload.__error && (
                <HOB_JsonTree
                  data={payload}
                  parentPath=""
                  depth={0}
                  overrides={overrides}
                  onTemplatize={(fieldPath, defaultValue) =>
                    setModalTarget({ kind: te.kind, source_id: te.source_id, field_path: fieldPath, default: defaultValue })
                  }
                  onRemoveMapping={(fieldPath) => removeMapping(te.kind, te.source_id, fieldPath)}
                />
              )}
            </div>
          </div>
        );
      })}

      {modalTarget && (
        <HOB_TemplatizeModal
          target={modalTarget}
          onClose={() => setModalTarget(null)}
          onAdd={(overridePath, widget) =>
            addMapping(modalTarget.kind, modalTarget.source_id, modalTarget.field_path, overridePath, widget, modalTarget.default)
          }
        />
      )}
    </div>
  );
}

// ----------------------------------------------------------------------------
// HOB_JsonTree — depth-limited JSON renderer with per-leaf "Templatize" button
// ----------------------------------------------------------------------------

function HOB_JsonTree({ data, parentPath, depth, overrides, onTemplatize, onRemoveMapping }) {
  if (data == null) return null;

  // Find an existing mapping for a given field_path.
  const mappingFor = (path) => overrides.find((m) => m.field_path === path);

  // Leaf primitive — render as a row.
  const isPrimitive = (v) => v == null || typeof v === "string" || typeof v === "number" || typeof v === "boolean";

  if (isPrimitive(data)) {
    const path = parentPath || "/";
    const mapping = mappingFor(path);
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 14px" }}>
        <span className="mono muted text-sm" style={{ fontSize: 11, minWidth: 200 }}>{path}</span>
        <span className="mono text-sm" style={{ fontSize: 11, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {JSON.stringify(data)}
        </span>
        {mapping ? (
          <span
            className="pill pill-claimed"
            style={{ fontSize: 10, textDecoration: "line-through", cursor: "pointer", background: "var(--blue-dim)" }}
            onClick={() => onRemoveMapping(path)}
            title={`override_path=${mapping.override_path} · click to remove`}
          >
            {mapping.override_path}
          </span>
        ) : (
          <Btn size="sm" kind="ghost" onClick={() => onTemplatize(path, data)}>Templatize</Btn>
        )}
      </div>
    );
  }

  if (Array.isArray(data)) {
    if (depth >= HOB_MAX_TREE_DEPTH) {
      return (
        <div className="muted text-sm" style={{ padding: "4px 14px", fontSize: 11 }}>
          {parentPath} → [array, depth limit reached]
        </div>
      );
    }
    return (
      <HOB_TreeGroup label={parentPath || "(root)"} count={data.length}>
        {data.map((item, i) => (
          <HOB_JsonTree
            key={i}
            data={item}
            parentPath={parentPath + "/" + i}
            depth={depth + 1}
            overrides={overrides}
            onTemplatize={onTemplatize}
            onRemoveMapping={onRemoveMapping}
          />
        ))}
      </HOB_TreeGroup>
    );
  }

  // Object
  const keys = Object.keys(data);
  if (depth === 0) {
    // Root: render fields directly without a wrapping group.
    return (
      <>
        {keys.map((k) => (
          <HOB_JsonTree
            key={k}
            data={data[k]}
            parentPath={parentPath + "/" + k}
            depth={depth + 1}
            overrides={overrides}
            onTemplatize={onTemplatize}
            onRemoveMapping={onRemoveMapping}
          />
        ))}
      </>
    );
  }
  if (depth >= HOB_MAX_TREE_DEPTH) {
    return (
      <div className="muted text-sm" style={{ padding: "4px 14px", fontSize: 11 }}>
        {parentPath} → {`{${keys.length} fields, depth limit reached}`}
      </div>
    );
  }
  return (
    <HOB_TreeGroup label={parentPath} count={keys.length}>
      {keys.map((k) => (
        <HOB_JsonTree
          key={k}
          data={data[k]}
          parentPath={parentPath + "/" + k}
          depth={depth + 1}
          overrides={overrides}
          onTemplatize={onTemplatize}
          onRemoveMapping={onRemoveMapping}
        />
      ))}
    </HOB_TreeGroup>
  );
}

function HOB_TreeGroup({ label, count, children }) {
  const [open, setOpen] = React.useState(true);
  return (
    <div style={{ borderLeft: "2px solid var(--border)", marginLeft: 14, paddingLeft: 6 }}>
      <div
        onClick={() => setOpen((v) => !v)}
        style={{ cursor: "pointer", padding: "2px 0", fontSize: 11, color: "var(--text-3)" }}
      >
        {open ? "▾" : "▸"} <span className="mono">{label}</span> <span className="muted">({count})</span>
      </div>
      {open && <div>{children}</div>}
    </div>
  );
}

// ----------------------------------------------------------------------------
// HOB_TemplatizeModal — add a mapping for one field
// ----------------------------------------------------------------------------

function HOB_TemplatizeModal({ target, onClose, onAdd }) {
  // Default override_path: derive from field_path /a/b/c → a.b.c
  const seedOverride = (target.field_path || "").replace(/^\//, "").replace(/\//g, ".");
  const [overridePath, setOverridePath] = React.useState(seedOverride);
  const [widget, setWidget] = React.useState("");
  const [error, setError] = React.useState("");

  const submit = () => {
    if (!overridePath) { setError("override_path is required"); return; }
    if (!/^[a-zA-Z_][a-zA-Z0-9_.-]*$/.test(overridePath)) {
      setError("override_path must be a dotted identifier path");
      return;
    }
    onAdd(overridePath, widget || null);
  };

  return (
    <Modal
      title="Templatize field"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" onClick={submit}>Add mapping</Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">Field</label>
        <div className="mono text-sm">{target.field_path}</div>
      </div>
      <div className="field">
        <label className="field-label" htmlFor="hob-override-path">override_path</label>
        <input
          id="hob-override-path"
          className="input mono"
          value={overridePath}
          onChange={(e) => { setOverridePath(e.target.value); setError(""); }}
          placeholder="llm.provider_id"
          style={{ width: "100%" }}
        />
        {error && <div className="field-help" style={{ color: "var(--red)" }}>{error}</div>}
      </div>
      <div className="field">
        <label className="field-label" htmlFor="hob-widget">widget</label>
        <select
          id="hob-widget"
          className="select"
          value={widget}
          onChange={(e) => setWidget(e.target.value)}
          style={{ width: "100%" }}
        >
          {HOB_WIDGET_CHOICES.map((w) => (
            <option key={w.value} value={w.value}>{w.label}</option>
          ))}
        </select>
      </div>
      <div className="field">
        <label className="field-label">Default value</label>
        <div className="mono text-sm" style={{ background: "var(--bg)", padding: "6px 8px", border: "1px solid var(--border)", borderRadius: 4, fontSize: 11 }}>
          {JSON.stringify(target.default)}
        </div>
      </div>
    </Modal>
  );
}

// ============================================================================
// Step 4 — Link & push
// ============================================================================

function HOB_Step4Link({ name, slug, description, ref_, subpath, gitUrl, tracked, pushNow, onPushNowChange, createError }) {
  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="panel">
        <div className="panel-h"><Icon name="git-commit" size={13} /><span>Summary</span></div>
        <div className="panel-body" style={{ padding: "8px 14px" }}>
          <dl className="kv" style={{ gridTemplateColumns: "150px 1fr", rowGap: 4 }}>
            <dt>Name</dt><dd>{name}</dd>
            <dt>Slug</dt><dd className="mono">{slug}</dd>
            {description && <><dt>Description</dt><dd>{description}</dd></>}
            <dt>Direction</dt><dd className="mono">outbound</dd>
            <dt>Git URL</dt><dd className="mono">{gitUrl}</dd>
            <dt>Ref</dt><dd className="mono">{ref_}</dd>
            {subpath && <><dt>Subpath</dt><dd className="mono">{subpath}</dd></>}
            <dt>Tracked</dt><dd>{tracked.length} {tracked.length === 1 ? "entity" : "entities"}</dd>
          </dl>
        </div>
      </div>

      <div className="panel">
        <div className="panel-h"><Icon name="box" size={13} /><span>Tracked entities</span></div>
        <div className="panel-body" style={{ padding: "4px 0" }}>
          {tracked.map((te) => (
            <div key={te.kind + ":" + te.source_id} style={{ padding: "6px 14px", borderBottom: "1px solid var(--border)", display: "flex", gap: 8, fontSize: 12 }}>
              <span className="mono" style={{ minWidth: 90 }}>{te.kind}</span>
              <span className="mono">{te.template_name}</span>
              <span className="mono muted text-sm" style={{ marginLeft: "auto", fontSize: 10.5 }}>
                {(te.overrides || []).length} mappings
              </span>
            </div>
          ))}
        </div>
      </div>

      <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12.5 }}>
        <input
          type="checkbox"
          checked={pushNow}
          onChange={(e) => onPushNowChange(e.target.checked)}
        />
        Push now after build
      </label>

      {createError && <Banner kind="error" title="Operation failed" detail={createError} />}
    </div>
  );
}

// ============================================================================
// Exports
// ============================================================================

window.HarnessOutboundBuilder = HarnessOutboundBuilder;
