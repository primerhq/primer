/* global React, Icon, Btn, Modal, Banner, CardList, Card, relativeTime, fmtDate */

// Semantic-Search Provider (SSP) pages — wired to the real API.
//
// Endpoints (CLAUDE.md §3.2):
//   GET    /v1/ssp?limit=200          — list (5s poll)
//   GET    /v1/ssp/{id}               — detail
//   POST   /v1/ssp                    — create (422 → inline fieldErrors)
//   POST   /v1/ssp/{id}/invalidate    — drop cached adapter
//   DELETE /v1/ssp/{id}               — delete (409 if any Collection refs it)
//
// Cache keys:
//   ssp:list             — list page (and the sidebar count, via app.jsx)
//   ssp-detail:${id}     — per-row detail probe
//
// Babel-standalone shares the global scope across <script> tags so every
// top-level binding in this file is prefixed with SSP_ to avoid clashes
// with other components (IC_*, WS_*, TS_*, KN_*, ...).

const SSP_CACHE_LIST = "ssp:list";
const SSP_CACHE_DETAIL_PREFIX = "ssp-detail:";

function _sspToastErr(pushToast, fallbackTitle) {
  return (err) => {
    if (typeof pushToast !== "function") return;
    pushToast({
      kind: "error",
      title: err?.title || fallbackTitle,
      detail: err?.detail || err?.message,
      requestId: err?.requestId,
    });
  };
}

// Shared by the create/edit submit body AND the test-connect box below -
// both need the same provider-shaped config from the same form state, so
// this is the one place that builds it.
//
// includeAdvanced mirrors whether the operator ever opened the Advanced
// <details> section: when they never did, the config carries ONLY the
// fields /ssp/_types itself describes (hostname/port/username/password/
// database, or path for lance) - a genuinely minimal, core-fields-only
// submission the backend now defaults the rest for (the union fix
// Dev-Backend pinned specifically for a core-fields-only pgvectorscale
// create). Editing an existing row always includes them, since a saved
// row's real values must round-trip rather than silently reset to
// server defaults on every unrelated save.
function _sspBuildConfig(form, isLance, isScale, includeAdvanced) {
  if (isLance) {
    const config = { path: form.path };
    if (includeAdvanced) {
      config.hnsw_m = Number(form.hnsw_m) || 16;
      config.hnsw_ef_construction = Number(form.hnsw_ef_construction) || 64;
      config.hnsw_ef_search = 40;
      config.index_min_rows = Number(form.index_min_rows) || 1000;
    }
    return config;
  }
  const config = {
    hostname: form.hostname,
    port: Number(form.port) || 5432,
    username: form.username,
    password: form.password,
    database: form.database,
  };
  if (includeAdvanced) {
    config.db_schema = form.db_schema || "public";
    config.hnsw_m = Number(form.hnsw_m) || 16;
    config.hnsw_ef_construction = Number(form.hnsw_ef_construction) || 64;
    config.use_halfvec = !!form.use_halfvec;
    if (isScale) {
      config.enable_diskann = !!form.enable_diskann;
      if (form.enable_diskann) {
        config.diskann_num_neighbors = Number(form.diskann_num_neighbors) || 50;
        config.diskann_search_list_size = Number(form.diskann_search_list_size) || 100;
      }
    }
  }
  return config;
}

// Platform wave P4 item 3: test-connect for a DRAFT config, wired to the
// real POST /ssp/_test (no schema/filesystem mutation server-side - see
// that route's own docstring). Probes exactly what Save would submit
// (same includeAdvanced gate _sspBuildConfig uses for the real submit
// body), not a separately-typed shape.
function SSP_TestConnectBox({ form, isLance, includeAdvanced }) {
  const { apiFetch } = window.primerApi;
  const [busy, setBusy] = React.useState(false);
  const [result, setResult] = React.useState(null);

  const run = async () => {
    setBusy(true);
    setResult(null);
    try {
      const isScale = form.provider === "pgvectorscale";
      const out = await apiFetch("POST", "/ssp/_test", {
        provider: form.provider,
        config: _sspBuildConfig(form, isLance, isScale, includeAdvanced),
      });
      setResult(out);
    } catch (err) {
      setResult({ ok: false, error: (err && (err.detail || err.message)) || String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="field" data-testid="ssp-test-connect-draft">
      <label className="field-label">Test connect</label>
      {/* Exact copy per the P4 brief: SSP reports reachability only, no
          model list (unlike the LLM/embedding families' probe panel). */}
      <div className="banner banner-info" style={{ fontSize: 11.5, padding: "6px 10px", marginBottom: 8 }}>
        <Icon name="info" size={11} className="ico" />
        <div>test-connect available — this kind reports reachability, not a model list.</div>
      </div>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <Btn kind="ghost" size="sm" disabled={busy} onClick={run}
          data-testid="ssp-test-connect-draft-run">
          {busy ? "Testing…" : "Test connection"}
        </Btn>
        {result ? (
          result.ok ? (
            <span className="mono text-sm" style={{ color: "var(--green)" }}
              data-testid="ssp-test-connect-draft-ok">reachable</span>
          ) : (
            <span className="mono text-sm" style={{ color: "var(--red)" }}
              data-testid="ssp-test-connect-draft-error">{result.error}</span>
          )
        ) : null}
      </div>
      <div className="field-help">
        Probes the config above as typed - no schema or filesystem is
        created or modified.
      </div>
    </div>
  );
}

function _sspAgeSec(iso) {
  if (!iso) return null;
  if (iso instanceof Date) return (Date.now() - iso.getTime()) / 1000;
  return (Date.now() - new Date(iso).getTime()) / 1000;
}

// ----------------------------------------------------------------------
// List page
// ----------------------------------------------------------------------

function SSPListPage({ onOpen, pushToast }) {
  const { useRouter, useViewport, usePagedList, useResource, apiFetch, Pager } = window.primerApi;
  const { navigate } = useRouter();
  const { isMobile } = useViewport();

  const [createOpen, setCreateOpen] = React.useState(false);
  // Platform wave P4 item 3: which kind was PICKED via the register
  // dropdown before Create opened - threaded into SSPCreateModal as its
  // initial provider so "kind decides the form up front" holds for SSP
  // too, same as every other family's register flow.
  const [createKind, setCreateKind] = React.useState(null);
  const [textQuery, setTextQuery] = React.useState("");
  const [backendFilter, setBackendFilter] = React.useState("");
  const filterFocused = React.useRef(false);

  // Platform wave P4: the backend filter's kind list came from a
  // hardcoded pgvector/pgvectorscale pair that didn't even include
  // lance - sourcing it from /ssp/_types (same served data the create
  // modal now also uses) fixes that gap for free instead of hand-
  // maintaining a second copy of the enum.
  const sspTypes = useResource(
    "ssp:types",
    (signal) => apiFetch("GET", "/ssp/_types", null, { signal }),
    { pollMs: null },
  );
  const sspTypeMap = sspTypes.data || {};
  const sspKinds = Object.keys(sspTypeMap);

  // Server-side offset pagination (bug #19); text + backend filters narrow
  // the current page and reset to page 0 via resetKey.
  const list = usePagedList({
    key: SSP_CACHE_LIST,
    path: "/ssp",
    pageSize: 50,
    pollMs: 5000,
    pauseWhile: () => filterFocused.current,
    resetKey: textQuery + "|" + backendFilter,
  });

  const items = list.items;

  const filtered = React.useMemo(() => {
    let arr = items;
    if (textQuery) {
      const q = textQuery.toLowerCase();
      arr = arr.filter((p) =>
        (p.id || "").toLowerCase().includes(q) ||
        (p.config?.hostname || "").toLowerCase().includes(q) ||
        (p.config?.database || "").toLowerCase().includes(q)
      );
    }
    if (backendFilter) arr = arr.filter((p) => p.provider === backendFilter);
    return arr;
  }, [items, textQuery, backendFilter]);

  const openRow = (id) => {
    if (typeof onOpen === "function") onOpen(id);
    else navigate(`/ssp/${encodeURIComponent(id)}`);
  };

  // Empty state — show a friendly create prompt when there are no providers.
  // Matches the visual contract used by the other provider pages
  // (LLM / Embedding / Cross-Encoder): the empty-state card on its own,
  // no preamble Banner. Operators get the call-to-action below; the
  // implication that downstream features need a provider is conveyed by
  // the empty-state copy rather than a separate warning.
  //
  // Important: both the empty-state and populated branches MUST render
  // the modal at the same React tree position so that polled refetches
  // (which briefly flip ``list.loading`` true→false) don't unmount and
  // re-mount the modal, wiping its internal form state. We achieve
  // that by computing the body once and always rendering ``modal`` as
  // a single fragment sibling at the bottom of the return.
  const isEmpty = !list.loading && items.length === 0 && !list.error;

  const openCreate = (kind) => { setCreateKind(kind); setCreateOpen(true); };
  const closeCreate = () => { setCreateOpen(false); setCreateKind(null); };

  const modal = createOpen ? (
    <SSPCreateModal
      onClose={closeCreate}
      pushToast={pushToast}
      initialProvider={createKind}
    />
  ) : null;

  if (isEmpty) {
    return (
      <>
        <div className="panel">
          <div className="empty">
            <div className="ico-wrap"><Icon name="subsystem" size={22} /></div>
            <div className="head">No semantic search providers</div>
            <div className="sub">
              A SemanticSearchProvider is the database (pgvector or pgvectorscale) that stores vector indexes for your collections.
            </div>
            {/* Platform wave P4 item 3: routed through the same
                PC_RegisterDropdown (provider-catalog.jsx) every other
                family's create flow uses, sourced from /ssp/_types -
                "kind decides the form up front" for SSP too. */}
            <div className="actions">
              <PC_RegisterDropdown klass={{ plural: "ssp" }} onPick={openCreate} />
              <Btn kind="ghost" icon="external">Read the docs</Btn>
            </div>
          </div>
        </div>
        {modal}
      </>
    );
  }

  return (
    <>
      <div className={"col" + (isMobile ? " ssp-mobile-stack" : "")} style={{ gap: 14 }}>
        <div className="filter-bar">
          <div className="input-icon">
            <Icon name="search" size={13} className="icon" />
            <input
              className="input"
              placeholder="Filter providers…"
              value={textQuery}
              onChange={(e) => setTextQuery(e.target.value)}
              onFocus={() => { filterFocused.current = true; }}
              onBlur={() => { filterFocused.current = false; }}
            />
          </div>
          <div className="sep-v" />
          <select
            className="select"
            value={backendFilter}
            onChange={(e) => setBackendFilter(e.target.value)}
          >
            <option value="">all backends</option>
            {sspKinds.map((k) => (
              <option key={k} value={k}>{(sspTypeMap[k] || {}).label || k}</option>
            ))}
          </select>
          <span className="muted text-sm tabular" style={{ marginLeft: "auto" }}>
            <span className="mono" style={{ color: "var(--green)" }}>● live</span> · /v1/ssp every 5s
          </span>
          <PC_RegisterDropdown klass={{ plural: "ssp" }} onPick={openCreate} />
        </div>

        {list.error && items.length === 0 ? (
          <Banner
            kind="error"
            title={list.error.title || "Couldn't load providers"}
            detail={list.error.detail || list.error.message}
            actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
          />
        ) : isMobile ? (
          <CardList
            items={filtered}
            empty={`No providers match the current filter${textQuery ? ` "${textQuery}"` : ""}.`}
            renderCard={(p) => (
              <Card
                title={p.id}
                subtitle={p.config?.hostname || ""}
                pill={<BackendBadge kind={p.provider} />}
                meta={`${p.config?.db_schema || "public"} · ${p.config?.database || "—"}`}
                onClick={() => openRow(p.id)}
              />
            )}
          />
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Backend</th>
                  <th>Host</th>
                  <th>Schema</th>
                  <th>Database</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 ? (
                  <tr><td colSpan={6} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
                    No providers match the current filter{textQuery ? ` "${textQuery}"` : ""}.
                    {" · "}<a
                      onClick={() => { setTextQuery(""); setBackendFilter(""); }}
                      style={{ cursor: "pointer", color: "var(--accent)" }}
                    >Clear filters</a>
                  </td></tr>
                ) : filtered.map((p) => (
                  <tr key={p.id} onClick={() => openRow(p.id)} style={{ cursor: "pointer" }}>
                    <td className="mono">{p.id}</td>
                    <td><BackendBadge kind={p.provider} /></td>
                    <td className="mono muted text-sm">
                      {p.config?.hostname || "—"}
                      {p.config?.port ? <span style={{ color: "var(--text-4)" }}>:{p.config.port}</span> : null}
                    </td>
                    <td className="mono muted text-sm">{p.config?.db_schema || "public"}</td>
                    <td className="mono muted text-sm">{p.config?.database || "—"}</td>
                    <td style={{ textAlign: "right", paddingRight: 12 }}>
                      <Icon name="chevron-right" size={12} className="muted" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pager pager={list} label="providers" />
      </div>
      {modal}
    </>
  );
}

// Platform wave P4: /ssp/_types has no color field (it's a form-shape
// contract, not a presentation one), so a per-kind color stays a small
// hardcoded UI-only map here - but distinguishing all three real kinds
// instead of lumping pgvectorscale and lance into one "not pgvector"
// bucket fixes a real (if minor) miscoloring.
const SSP_BADGE_COLORS = {
  pgvector: "var(--blue)",
  pgvectorscale: "var(--violet)",
  lance: "var(--amber)",
};

function BackendBadge({ kind }) {
  const color = SSP_BADGE_COLORS[kind] || "var(--text-3)";
  return (
    <span className="pill" style={{ background: "var(--bg-2)", color, border: "1px solid var(--border)" }}>
      <span className="dot" style={{ background: color }}></span>
      <span className="mono text-sm">{kind}</span>
    </span>
  );
}

// ----------------------------------------------------------------------
// Create modal
// ----------------------------------------------------------------------

function SSPCreateModal({ onClose, pushToast, existing, initialProvider }) {
  // Same modal handles create + edit. In edit mode: locked id +
  // provider, password blanked so the "**********" redaction never
  // round-trips, PUT-replaces.
  const isEdit = !!existing;
  const { useMutation, useRouter, useResource, apiFetch } = window.primerApi;
  const { navigate } = useRouter();

  // Platform wave P4: same /ssp/_types source the list filter uses -
  // kills the hardcoded pgvector/pgvectorscale/lance <option> triplet
  // below. The lance-missing capability hint stays separate: that's a
  // runtime extras check (useCapabilities), not a form-shape fact
  // _types describes.
  const sspTypes = useResource(
    "ssp:types",
    (signal) => apiFetch("GET", "/ssp/_types", null, { signal }),
    { pollMs: null },
  );
  const sspTypeMap = sspTypes.data || {};
  const sspKinds = Object.keys(sspTypeMap);

  const _initialForm = () => {
    if (!isEdit) {
      return {
        // Platform wave P4 item 3: the register dropdown picks the kind
        // BEFORE this modal opens ("kind decides the form up front",
        // same as every other family) - initialProvider carries that
        // pick. Falls back to pgvector only if the modal is somehow
        // opened without one.
        id: "", provider: initialProvider || "pgvector",
        hostname: "", port: 5432, database: "primer",
        username: "", password: "", db_schema: "public",
        path: "",
        hnsw_m: 16, hnsw_ef_construction: 64,
        use_halfvec: false,
        enable_diskann: false, diskann_num_neighbors: 50, diskann_search_list_size: 100,
        index_min_rows: 1000,
      };
    }
    const c = existing.config || {};
    const rawPassword = c.password;
    return {
      id: existing.id,
      provider: existing.provider,
      hostname: c.hostname || "",
      port: c.port ?? 5432,
      database: c.database || "primer",
      username: c.username || "",
      // Blank-on-prefill: rawPassword is the "**********" redaction;
      // operator must re-enter to save (validated below).
      password: typeof rawPassword === "string" && /^\*{6,}$/.test(rawPassword) ? "" : (rawPassword || ""),
      db_schema: c.db_schema || "public",
      path: c.path || "",
      hnsw_m: c.hnsw_m ?? 16,
      hnsw_ef_construction: c.hnsw_ef_construction ?? 64,
      use_halfvec: !!c.use_halfvec,
      enable_diskann: !!c.enable_diskann,
      diskann_num_neighbors: c.diskann_num_neighbors ?? 50,
      diskann_search_list_size: c.diskann_search_list_size ?? 100,
      index_min_rows: c.index_min_rows ?? 1000,
    };
  };
  const [form, setForm] = React.useState(_initialForm);
  const [fieldErrors, setFieldErrors] = React.useState({});
  // Platform wave P4 item 3: whether the operator ever opened the
  // Advanced section this session. An edit always includes those
  // fields regardless (a saved row's real values must round-trip), a
  // create only includes them once the operator has actually looked -
  // an untouched create genuinely submits config_fields only.
  const [advancedOpened, setAdvancedOpened] = React.useState(isEdit);

  const update = (k, v) => setForm((f) => ({ ...f, [k]: v }));
  const isScale = form.provider === "pgvectorscale";
  const isLance = form.provider === "lance";
  // lance is the one backend behind an optional extra; pgvector and
  // pgvectorscale are core (they only need a reachable Postgres).
  const caps = window.primerApi.useCapabilities();
  const lanceMissing = !window.primerApi.extraInstalled(caps, "lance");
  const isPostgresFamily = form.provider === "pgvector" || form.provider === "pgvectorscale";

  const create = useMutation(
    (body) => isEdit
      ? apiFetch("PUT", `/ssp/${encodeURIComponent(existing.id)}`, body)
      : apiFetch("POST", "/ssp", body),
    {
      invalidates: isEdit
        ? [SSP_CACHE_LIST, `ssp-detail:${existing?.id}`]
        : [SSP_CACHE_LIST],
      onSuccess: (row) => {
        onClose();
        if (pushToast) {
          pushToast({
            kind: isEdit ? "info" : "success",
            title: isEdit ? "Provider updated" : "Provider created",
            detail: `${row?.id || existing?.id} (${row?.provider || existing?.provider})`,
          });
        }
        if (!isEdit) navigate(`/ssp/${encodeURIComponent(row.id)}`);
      },
      onError: (err) => {
        if (err?.status === 422 && Array.isArray(err.fieldErrors)) {
          const next = {};
          for (const fe of err.fieldErrors) {
            // Pydantic loc is like ["body", "config", "hostname"] — strip
            // the leading "body" and join the rest.
            const loc = (fe.loc || []).filter((seg) => seg !== "body");
            next[loc.join(".")] = fe.msg;
            // Also key by the bare last segment so the simple field rows
            // below can find their error without knowing the full path.
            if (loc.length > 0) next[loc[loc.length - 1]] = fe.msg;
          }
          setFieldErrors(next);
        } else {
          _sspToastErr(pushToast, "Create failed")(err);
        }
      },
    }
  );

  const submit = () => {
    // Client-side guard for required fields, scoped per backend.
    const errs = {};
    if (!form.id) errs.id = "value is required";
    if (isLance) {
      if (!form.path) errs.path = "value is required";
    } else {
      if (!form.hostname) errs.hostname = "value is required";
      if (!form.username) errs.username = "value is required";
      if (!form.password) errs.password = "value is required";
      if (!form.database) errs.database = "value is required";
    }
    if (Object.keys(errs).length > 0) {
      setFieldErrors(errs);
      return;
    }
    setFieldErrors({});

    const config = _sspBuildConfig(form, isLance, isScale, advancedOpened);
    const body = { id: form.id, provider: form.provider, config };
    create.mutate(body).catch(() => { /* onError already handled */ });
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal"
        style={{ width: 540 }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-h">
          <span className="title">{isEdit ? `Edit SSP · ${existing.id}` : "New Semantic Search provider"}</span>
          <button className="close" onClick={onClose}><Icon name="x" size={14} /></button>
        </div>
        <div className="modal-b">
          <FieldRow label="id" hint={isEdit ? "locked — id cannot change after create" : "must be unique"} err={fieldErrors.id}>
            <input
              className="input mono"
              value={form.id}
              onChange={(e) => update("id", e.target.value)}
              placeholder="pg-prod-main"
              disabled={isEdit}
              style={{ width: "100%" }}
            />
          </FieldRow>
          <FieldRow label="backend" hint={isEdit ? "locked — recreate to change backend" : undefined}>
            <select className="select mono" value={form.provider} onChange={(e) => update("provider", e.target.value)} disabled={isEdit} style={{ width: "100%" }}>
              {sspKinds.map((k) => (
                <option key={k} value={k}>
                  {(sspTypeMap[k] || {}).label || k}
                  {k === "lance" && lanceMissing ? " (not installed)" : ""}
                </option>
              ))}
            </select>
            {isLance && lanceMissing && (
              <div className="field-help" style={{ color: "var(--amber)" }}
                data-capability-hint="lance">
                {window.primerApi.capabilityHint("lance")}
              </div>
            )}
          </FieldRow>

          {isPostgresFamily && (<>
            <Section label="Connection" />
            <FieldRow label="hostname" err={fieldErrors.hostname}>
              <input className="input mono" value={form.hostname} onChange={(e) => update("hostname", e.target.value)} placeholder="pg-prod.internal" style={{ width: "100%" }} />
            </FieldRow>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 100px", gap: 10 }}>
              <FieldRow label="database" err={fieldErrors.database}>
                <input className="input mono" value={form.database} onChange={(e) => update("database", e.target.value)} style={{ width: "100%" }} />
              </FieldRow>
              <FieldRow label="port" err={fieldErrors.port}>
                <input className="input mono" type="number" value={form.port} onChange={(e) => update("port", +e.target.value)} style={{ width: "100%" }} />
              </FieldRow>
            </div>
            <FieldRow label="username" err={fieldErrors.username}>
              <input className="input mono" value={form.username} onChange={(e) => update("username", e.target.value)} placeholder="primer_rw" style={{ width: "100%" }} />
            </FieldRow>
            {/* Platform wave P1a item 5: the reference's "DSN secret —
                masked on read" copy assumes a single DSN connection
                string; the real SemanticSearchProvider config is
                structured fields (hostname/port/database/username/
                password), not one combined DSN - "password" is the
                actual secret field here, so the label pattern applies
                to it directly rather than inventing a DSN field the
                backend does not have. */}
            <FieldRow label="password secret — masked on read" hint="SecretStr · stored encrypted" err={fieldErrors.password}>
              <input className="input mono" type="password" value={form.password} onChange={(e) => update("password", e.target.value)} placeholder="•••••••••" style={{ width: "100%" }} />
            </FieldRow>
          </>)}

          {isLance && (<>
            <Section label="Filesystem" />
            <FieldRow label="path" hint="absolute directory; created on first use" err={fieldErrors.path}>
              <input
                className="input mono"
                data-testid="ssp-lance-path"
                value={form.path}
                onChange={(e) => update("path", e.target.value)}
                placeholder={`~/.primer/lance/${form.id || "<id>"}/`}
                style={{ width: "100%" }}
              />
            </FieldRow>
          </>)}

          {/* Platform wave P4 item 3: /ssp/_types' own docstring
              (routers/semantic_search.py's _postgres_family_fields)
              calls HNSW/DiskANN/vector-type/db_schema advanced tuning
              with sane defaults, deliberately left off the minimal
              form contract - same treatment the LLM family gives its
              Limits block. Collapsed by default rather than deleted:
              every field here is real, working, currently-shipped
              configurability, so nothing is lost - it's just not part
              of the default view, AND (via advancedOpened below) not
              part of the default submit body either: an untouched
              Advanced section sends a genuinely core-fields-only
              config, matching the backend's own confirmed
              core-fields-only-create case (the union fix Dev-Backend
              pinned). */}
          <details data-testid="ssp-advanced-details" open={isEdit}
            onToggle={(e) => { if (e.target.open) setAdvancedOpened(true); }}>
            <summary className="mono text-sm muted" style={{ cursor: "pointer", marginBottom: 8 }}>
              Advanced (optional - sane defaults apply)
            </summary>
            {isPostgresFamily && (
              <FieldRow label="schema" err={fieldErrors.db_schema}>
                <input className="input mono" value={form.db_schema} onChange={(e) => update("db_schema", e.target.value)} style={{ width: "100%" }} />
              </FieldRow>
            )}
            <Section label="HNSW knobs" />
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              <FieldRow label="M" hint="graph degree" err={fieldErrors.hnsw_m}>
                <input className="input mono" type="number" value={form.hnsw_m} onChange={(e) => update("hnsw_m", +e.target.value)} style={{ width: "100%" }} />
              </FieldRow>
              <FieldRow label="ef_construction" hint="build-time accuracy" err={fieldErrors.hnsw_ef_construction}>
                <input className="input mono" type="number" value={form.hnsw_ef_construction} onChange={(e) => update("hnsw_ef_construction", +e.target.value)} style={{ width: "100%" }} />
              </FieldRow>
            </div>

            {isPostgresFamily && (<>
              <Section label="Vector type" />
              <label style={{ display: "inline-flex", alignItems: "center", gap: 8, marginBottom: 6, fontSize: 12.5 }}>
                <input type="checkbox" checked={form.use_halfvec} onChange={(e) => update("use_halfvec", e.target.checked)} />
                <span>Use halfvec (half-precision)</span>
              </label>
              <div style={{ fontSize: 11.5, color: "var(--text-4)", marginBottom: 10 }}>
                Store vectors as half-precision (halfvec), supporting up to 4000 dimensions. Enable for embedding models above 2000 dims, e.g. text-embedding-3-large (3072). Off uses the standard vector type (max 2000).
              </div>
            </>)}

            {isPostgresFamily && (<>
              <Section label="DiskANN" sub="pgvectorscale only" />
              {!isScale && (
                <div className="banner banner-info" style={{ fontSize: 11.5, padding: "6px 10px", marginBottom: 10 }}>
                  <Icon name="info" size={11} className="ico" />
                  <div>Switch backend to <span className="mono">pgvectorscale</span> to enable DiskANN.</div>
                </div>
              )}
              <fieldset disabled={!isScale} style={{ border: 0, padding: 0, margin: 0, opacity: isScale ? 1 : 0.4 }}>
                <label style={{ display: "inline-flex", alignItems: "center", gap: 8, marginBottom: 10, fontSize: 12.5 }}>
                  <input type="checkbox" checked={form.enable_diskann} onChange={(e) => update("enable_diskann", e.target.checked)} />
                  <span>enable DiskANN index</span>
                </label>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                  <FieldRow label="num_neighbors" err={fieldErrors.diskann_num_neighbors}>
                    <input className="input mono" type="number" value={form.diskann_num_neighbors} onChange={(e) => update("diskann_num_neighbors", +e.target.value)} disabled={!form.enable_diskann} style={{ width: "100%" }} />
                  </FieldRow>
                  <FieldRow label="search_list_size" err={fieldErrors.diskann_search_list_size}>
                    <input className="input mono" type="number" value={form.diskann_search_list_size} onChange={(e) => update("diskann_search_list_size", +e.target.value)} disabled={!form.enable_diskann} style={{ width: "100%" }} />
                  </FieldRow>
                </div>
              </fieldset>
            </>)}
          </details>

          <SSP_TestConnectBox form={form} isLance={isLance} includeAdvanced={advancedOpened} />
        </div>
        <div className="modal-f">
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon={isEdit ? "check" : "plus"} onClick={submit} disabled={create.loading}>
            {create.loading ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save changes" : "Create")}
          </Btn>
        </div>
      </div>
    </div>
  );
}

function FieldRow({ label, hint, err, children }) {
  return (
    <div className="field">
      <label className="field-label">
        {label}
        {hint && <span className="hint">{hint}</span>}
      </label>
      {children}
      {err && <div className="field-help" style={{ color: "var(--red)" }}>
        <Icon name="x-circle" size={11} style={{ verticalAlign: -1, marginRight: 3 }} />
        {err}
      </div>}
    </div>
  );
}

function Section({ label, sub }) {
  return (
    <div style={{ borderBottom: "1px dashed var(--border)", marginBottom: 12, paddingBottom: 4, marginTop: 6 }}>
      <span className="mono" style={{ fontSize: 10.5, fontWeight: 600, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</span>
      {sub && <span className="muted text-sm" style={{ marginLeft: 8, fontSize: 11 }}>· {sub}</span>}
    </div>
  );
}

// ----------------------------------------------------------------------
// Detail page
// ----------------------------------------------------------------------

function SSPDetail({ sspId, pushToast }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();

  const [tab, setTab] = React.useState("overview");
  const [showDelete, setShowDelete] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState(null);
  const [editing, setEditing] = React.useState(false);

  const detailKey = SSP_CACHE_DETAIL_PREFIX + sspId;
  const detail = useResource(
    detailKey,
    (signal) => apiFetch("GET", `/ssp/${encodeURIComponent(sspId)}`, null, { signal }),
    { deps: [sspId] }
  );

  // Collections that reference this SSP — used by the "Collections" tab
  // and the delete-confirmation 409 preview. /v1/collections is a CRUD
  // list endpoint; we pull up to 500 and filter client-side. (Server
  // returns the 409 if we still try to delete, so this is just the UX
  // preview — the source of truth is the backend cascade-block hook.)
  const collections = useResource(
    `ssp-detail:${sspId}:collections`,
    (signal) => apiFetch("GET", "/collections?limit=200", null, { signal }),
    { deps: [sspId] }
  );
  const referencingCollections = React.useMemo(() => {
    const items = collections.data?.items ?? [];
    return items.filter((c) => c.search_provider_id === sspId);
  }, [collections.data, sspId]);

  // Platform wave P4: test-connect for the SAVED row, wired to the real
  // GET /ssp/{id}/_test - reads the stored (unredacted) config server-
  // side, so this never sends the "**********" password mask the
  // detail GET would carry (see that route's own docstring for why the
  // draft variant above can't serve this).
  const [testResult, setTestResult] = React.useState(null);
  const testConn = useMutation(
    () => apiFetch("GET", `/ssp/${encodeURIComponent(sspId)}/_test`),
    {
      onSuccess: (out) => setTestResult(out),
      onError: (err) => setTestResult({
        ok: false, error: (err && (err.detail || err.message)) || String(err),
      }),
    }
  );

  const invalidate = useMutation(
    () => apiFetch("POST", `/ssp/${encodeURIComponent(sspId)}/invalidate`),
    {
      invalidates: [detailKey],
      onSuccess: () => {
        if (pushToast) {
          pushToast({
            kind: "success",
            title: "Cache dropped",
            detail: `POST /v1/ssp/${sspId}/invalidate → 204. Next call resolves a fresh connection.`,
          });
        }
      },
      onError: _sspToastErr(pushToast, "Invalidate failed"),
    }
  );

  const del = useMutation(
    () => apiFetch("DELETE", `/ssp/${encodeURIComponent(sspId)}`),
    {
      invalidates: [SSP_CACHE_LIST],
      onSuccess: () => {
        if (pushToast) {
          pushToast({
            kind: "warning",
            title: "Provider deleted",
            detail: `DELETE /v1/ssp/${sspId} → 204`,
          });
        }
        setShowDelete(false);
        navigate("/ssp");
      },
      onError: (err) => {
        if (err?.status === 409) {
          setDeleteError(err.detail || "Cannot delete — referenced by a Collection.");
        } else {
          setShowDelete(false);
          _sspToastErr(pushToast, "Delete failed")(err);
        }
      },
    }
  );

  if (detail.loading && !detail.data) {
    return (
      <div className="panel">
        <div className="panel-body" style={{ padding: 18 }}>
          <span className="muted text-sm">Loading provider…</span>
        </div>
      </div>
    );
  }

  if (detail.error && !detail.data) {
    return (
      <Banner
        kind="error"
        title={detail.error.title || `Couldn't load ${sspId}`}
        detail={detail.error.detail || detail.error.message}
        actions={<Btn size="sm" icon="refresh" onClick={detail.refetch}>Retry</Btn>}
      />
    );
  }

  const p = detail.data;
  if (!p) return null;

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="panel">
        <div className="panel-body" style={{ padding: "14px 18px", display: "flex", alignItems: "center", gap: 14 }}>
          <BackendBadge kind={p.provider} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{p.id}</div>
            <div className="muted text-sm mono" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {p.provider === "lance"
                ? <>{p.config?.path}</>
                : <>
                    {p.config?.username}@{p.config?.hostname}:{p.config?.port}/{p.config?.database}
                    {p.config?.db_schema ? ` · schema ${p.config.db_schema}` : ""}
                  </>}
            </div>
          </div>
          <Btn
            size="sm"
            kind="ghost"
            icon="check-circle"
            data-testid="ssp-test-connect-saved-run"
            onClick={() => testConn.mutate()}
            disabled={testConn.loading}
          >
            {testConn.loading ? "Testing…" : "Test connect"}
          </Btn>
          <Btn
            size="sm"
            kind="ghost"
            icon="refresh"
            onClick={() => invalidate.mutate()}
            disabled={invalidate.loading}
          >
            {invalidate.loading ? "Invalidating…" : "Invalidate"}
          </Btn>
          <Btn size="sm" kind="secondary" icon="edit" onClick={() => setEditing(true)}>Edit</Btn>
          <Btn size="sm" kind="danger" icon="trash" onClick={() => { setDeleteError(null); setShowDelete(true); }}>Delete</Btn>
        </div>
        {testResult ? (
          <div className="panel-body" style={{ padding: "0 18px 12px" }}>
            {testResult.ok ? (
              <span className="mono text-sm" style={{ color: "var(--green)" }}
                data-testid="ssp-test-connect-saved-ok">reachable</span>
            ) : (
              <span className="mono text-sm" style={{ color: "var(--red)" }}
                data-testid="ssp-test-connect-saved-error">{testResult.error}</span>
            )}
          </div>
        ) : null}

        <div style={{ display: "flex", alignItems: "center", borderTop: "1px solid var(--border)", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {[
            { id: "overview", label: "Overview", icon: "info" },
            { id: "config", label: "Config", icon: "settings" },
            { id: "collections", label: "Collections", icon: "collection", count: referencingCollections.length },
          ].map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              style={{
                background: "none", border: "none",
                padding: "10px 14px", cursor: "pointer",
                color: tab === t.id ? "var(--text)" : "var(--text-3)",
                fontSize: 12.5, fontWeight: tab === t.id ? 600 : 400,
                borderBottom: tab === t.id ? "2px solid var(--accent)" : "2px solid transparent",
                marginBottom: -1,
                display: "inline-flex", alignItems: "center", gap: 6,
              }}
            >
              <Icon name={t.icon} size={13} />
              {t.label}
              {t.count != null && t.count > 0 && <span className="count" style={{ marginLeft: 4 }}>{t.count}</span>}
            </button>
          ))}
        </div>

        <div style={{ padding: 18 }}>
          {tab === "overview" && <SSPOverview p={p} />}
          {tab === "config" && <SSPConfig p={p} />}
          {tab === "collections" && <SSPCollections collections={referencingCollections} sspId={sspId} />}
        </div>
      </div>

      {showDelete && (
        <Modal
          title={`Delete ${sspId}?`}
          danger
          onClose={() => { setShowDelete(false); setDeleteError(null); }}
          footer={
            <>
              <Btn kind="ghost" onClick={() => { setShowDelete(false); setDeleteError(null); }}>Cancel</Btn>
              <Btn
                kind="danger"
                icon="trash"
                disabled={referencingCollections.length > 0 || del.loading}
                onClick={() => del.mutate().catch(() => { /* onError handled */ })}
              >
                {del.loading ? "Deleting…" : "Delete provider"}
              </Btn>
            </>
          }
        >
          {deleteError ? (
            <>
              <strong style={{ color: "var(--red)" }}>409 Conflict</strong> — {deleteError}
            </>
          ) : referencingCollections.length > 0 ? (
            <>
              <strong style={{ color: "var(--red)" }}>409 Conflict</strong> — this provider is referenced by{" "}
              <strong>{referencingCollections.length}</strong> collection{referencingCollections.length === 1 ? "" : "s"}:
              <ul>
                {referencingCollections.slice(0, 6).map((c) => <li key={c.id} className="mono">{c.id}</li>)}
              </ul>
              Reassign or delete those collections first.
            </>
          ) : (
            <>
              No collections reference this provider. Deletion is safe.
              <ul>
                <li>
                  {p.provider === "lance"
                    ? <>The LanceDB datasets in <span className="mono">{p.config?.path}</span> are <strong>not</strong> removed.</>
                    : <>The vector tables in <span className="mono">{p.config?.db_schema || "public"}</span> are <strong>not</strong> dropped.</>}
                </li>
                <li>The provider row is removed; the cached connection is closed.</li>
              </ul>
            </>
          )}
        </Modal>
      )}
      {editing && (
        <SSPCreateModal
          existing={p}
          pushToast={pushToast}
          onClose={() => setEditing(false)}
        />
      )}
    </div>
  );
}

function SSPOverview({ p }) {
  if (p.provider === "lance") {
    return (
      <dl className="kv" style={{ gridTemplateColumns: "200px 1fr" }}>
        <dt>id</dt><dd className="mono">{p.id}</dd>
        <dt>backend</dt><dd><BackendBadge kind={p.provider} /></dd>
        <dt>path</dt><dd className="mono">{p.config?.path}</dd>
        <dt>hnsw_m</dt><dd className="mono">{p.config?.hnsw_m ?? 16}</dd>
        <dt>hnsw_ef_construction</dt><dd className="mono">{p.config?.hnsw_ef_construction ?? 64}</dd>
        <dt>hnsw_ef_search</dt><dd className="mono">{p.config?.hnsw_ef_search ?? 40}</dd>
        <dt>index_min_rows</dt><dd className="mono">{p.config?.index_min_rows ?? 1000}</dd>
      </dl>
    );
  }
  return (
    <dl className="kv" style={{ gridTemplateColumns: "200px 1fr" }}>
      <dt>id</dt><dd className="mono">{p.id}</dd>
      <dt>backend</dt><dd><BackendBadge kind={p.provider} /></dd>
      <dt>host</dt><dd className="mono">{p.config?.hostname}:{p.config?.port}</dd>
      <dt>database</dt><dd className="mono">{p.config?.database}</dd>
      <dt>schema</dt><dd className="mono">{p.config?.db_schema || "public"}</dd>
      <dt>distance_metric</dt><dd className="mono">{p.config?.distance_metric || "cosine"}</dd>
      <dt>vector_type</dt><dd className="mono">{p.config?.use_halfvec ? "halfvec" : "vector"}</dd>
      <dt>hnsw_m</dt><dd className="mono">{p.config?.hnsw_m ?? 16}</dd>
      <dt>hnsw_ef_construction</dt><dd className="mono">{p.config?.hnsw_ef_construction ?? 64}</dd>
    </dl>
  );
}

function SSPConfig({ p }) {
  // Server already redacts SecretStr fields to "**********" — we just
  // render whatever the API returned. Showing the raw JSON keeps the
  // shape visible (operators can copy-paste into a debug ticket).
  return (
    <div>
      <div className="muted text-sm mb-2">Server returns this redacted config — the password is never sent over the wire.</div>
      <div className="code-block" style={{ maxHeight: 360, overflow: "auto", whiteSpace: "pre", fontFamily: "var(--font-mono, monospace)", fontSize: 12 }}>
        {JSON.stringify({ id: p.id, provider: p.provider, config: p.config }, null, 2)}
      </div>
    </div>
  );
}

function SSPCollections({ collections, sspId }) {
  if (collections.length === 0) {
    return (
      <div className="empty" style={{ padding: 20 }}>
        <div className="ico-wrap"><Icon name="collection" size={18} /></div>
        <div className="head">No collections bound</div>
        <div className="sub">No collections are using this provider yet. Create one and select <span className="mono">{sspId}</span>.</div>
      </div>
    );
  }
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>ID</th>
          <th>Description</th>
          <th>Embedding</th>
        </tr>
      </thead>
      <tbody>
        {collections.map((c) => (
          <tr key={c.id}>
            <td className="mono">{c.id}</td>
            <td className="muted">{c.description || c.desc || <span style={{ color: "var(--text-4)" }}>—</span>}</td>
            <td className="mono muted text-sm">
              {c.embedding_provider_id || c.embedding_provider || "—"}
              {(c.embedding_model || c.model) ? (
                <span style={{ color: "var(--text-4)" }}> · {c.embedding_model || c.model}</span>
              ) : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

window.SSPListPage = SSPListPage;
window.SSPDetail = SSPDetail;
window.BackendBadge = BackendBadge;
