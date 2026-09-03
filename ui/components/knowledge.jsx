/* global React, Icon, Btn, StatusPill, Modal, Banner, CardList, Card, Fab, relativeTime */
// ============================================================================
// Collections (v2): a collection is a wiki of text documents arranged as a
// slug-path tree. Grep and read work with no embedder configured; semantic
// search is an opt-in block managed from the search-settings drawer.
//
// The page component name and its window export are PINNED: S8's overlay
// host binds window.CollectionsPage. The flat DocumentsPage is gone with
// the flat document surface.
// ============================================================================

function KN_EmptyState({ ico, head, sub, cta }) {
  return (
    <div className="empty col" style={{ gap: 8, alignItems: "center", padding: 24 }}>
      <Icon name={ico || "book"} />
      <div className="empty-head">{head}</div>
      {sub ? <div className="muted text-sm">{sub}</div> : null}
      {cta || null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tree
// ---------------------------------------------------------------------------

function KN_TreeNode({ cid, node, depth, expanded, toggle, selectedPath, onSelect, children }) {
  const isOpen = !!expanded[node.path];
  return (
    <div className="kn-tree-node">
      <div
        className={`kn-tree-row${selectedPath === node.path ? " selected" : ""}`}
        style={{ paddingLeft: 8 + depth * 14 }}
        onClick={() => onSelect(node.path)}
      >
        {node.has_children ? (
          <span
            className="kn-tree-caret"
            onClick={(e) => { e.stopPropagation(); toggle(node.path); }}
          >
            <Icon name={isOpen ? "chevron-down" : "chevron-right"} />
          </span>
        ) : (
          <span className="kn-tree-caret placeholder" />
        )}
        <span className="kn-tree-label">{node.title || node.slug}</span>
      </div>
      {isOpen ? children : null}
    </div>
  );
}

function KN_Tree({ cid, parent, depth, expanded, toggle, selectedPath, onSelect, reloadKey }) {
  const { useResource, apiFetch } = window.primerApi;
  const nodes = useResource(
    `kn:tree:${cid}:${parent}:${reloadKey}`,
    (signal) => apiFetch(
      "GET",
      `/collections/${encodeURIComponent(cid)}/docs?parent=${encodeURIComponent(parent)}&depth=1`,
      null,
      { signal },
    ),
    { pollMs: null },
  );
  const items = nodes.data?.nodes ?? [];
  if (nodes.loading && !nodes.data) {
    return <div className="muted text-sm" style={{ paddingLeft: 8 + depth * 14 }}>Loading…</div>;
  }
  return (
    <>
      {items.map((n) => (
        <KN_TreeNode
          key={n.path}
          cid={cid}
          node={n}
          depth={depth}
          expanded={expanded}
          toggle={toggle}
          selectedPath={selectedPath}
          onSelect={onSelect}
        >
          <KN_Tree
            cid={cid}
            parent={n.path}
            depth={depth + 1}
            expanded={expanded}
            toggle={toggle}
            selectedPath={selectedPath}
            onSelect={onSelect}
            reloadKey={reloadKey}
          />
        </KN_TreeNode>
      ))}
    </>
  );
}

// ---------------------------------------------------------------------------
// Document pane
// ---------------------------------------------------------------------------

function KN_DocumentPane({ collection, path, readOnly, pushToast, onChanged, onSelect }) {
  const { useResource, apiFetch } = window.primerApi;
  const cid = collection.id;
  const [draft, setDraft] = React.useState(null);
  const [title, setTitle] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [childOpen, setChildOpen] = React.useState(false);
  const [moveOpen, setMoveOpen] = React.useState(false);

  const doc = useResource(
    `kn:doc:${cid}:${path}`,
    (signal) => apiFetch(
      "GET",
      `/collections/${encodeURIComponent(cid)}/docs?path=${encodeURIComponent(path)}`,
      null,
      { signal },
    ),
    { pollMs: null },
  );

  React.useEffect(() => {
    if (doc.data) {
      setDraft(doc.data.body ?? "");
      setTitle(doc.data.document?.title || "");
    }
  }, [doc.data, path]);

  if (doc.error) {
    return (
      <Banner kind="error" title="Could not read document">
        {String(doc.error.detail || doc.error.message || doc.error)}
      </Banner>
    );
  }
  if (!doc.data) return <div className="muted">Loading…</div>;

  const body = doc.data.body ?? "";
  const looksMarkdown = body.startsWith("#") || /(^|\n)(#{1,6} |\* |- |\d+\. |```)/.test(body);

  const save = async () => {
    setBusy(true);
    try {
      await apiFetch(
        "PATCH",
        `/collections/${encodeURIComponent(cid)}/docs?path=${encodeURIComponent(path)}`,
        { body: draft, title: title || null },
      );
      pushToast && pushToast({
        kind: "success", title: "Document saved", detail: path,
      });
      onChanged && onChanged();
    } catch (err) {
      pushToast && pushToast({
        kind: "error", title: "Save failed",
        detail: err?.detail || err?.message,
      });
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    const kids = (doc.data.children || []).length;
    const msg = kids
      ? `Delete ${path} and its ${kids} descendant document(s)?`
      : `Delete ${path}?`;
    // The themed dialog, not the browser's. Every destructive action in
    // this console routes through ConfirmHost; a native confirm() is a
    // modal the app cannot style, cannot test and, in a headless
    // browser, is dismissed for it, so the delete silently did nothing.
    const ok = await window.confirmDialog({
      title: "Delete document", message: msg,
      confirmLabel: "Delete", danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      await apiFetch(
        "DELETE",
        `/collections/${encodeURIComponent(cid)}/docs?path=${encodeURIComponent(path)}`
        + `&recursive=${kids ? "true" : "false"}`,
      );
      pushToast && pushToast({
        kind: "success", title: "Document deleted", detail: path,
      });
      onSelect(null);
      onChanged && onChanged();
    } catch (err) {
      pushToast && pushToast({
        kind: "error", title: "Delete failed",
        detail: err?.detail || err?.message,
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="col" style={{ gap: 12, minWidth: 0 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <div className="col" style={{ gap: 2, minWidth: 0 }}>
          <div className="mono text-sm muted">{path}</div>
          {readOnly ? (
            <div className="row" style={{ gap: 6, alignItems: "center" }}>
              <Icon name="lock" />
              <span className="muted text-sm">
                System collection: regenerated from platform state, read-only.
              </span>
            </div>
          ) : null}
        </div>
        {readOnly ? null : (
          <div className="row" style={{ gap: 6 }}>
            <Btn icon="plus" kind="ghost" onClick={() => setChildOpen(true)}>New child</Btn>
            <Btn icon="move" kind="ghost" onClick={() => setMoveOpen(true)}>Move</Btn>
            <Btn icon="trash" kind="ghost" onClick={remove} disabled={busy}>Delete</Btn>
            <Btn icon="check" onClick={save} disabled={busy}>Save</Btn>
          </div>
        )}
      </div>

      {readOnly ? (
        <div className="kn-doc-body">
          {looksMarkdown && window.renderMarkdown
            ? <div dangerouslySetInnerHTML={{ __html: window.renderMarkdown(body) }} />
            : <pre className="mono">{body}</pre>}
        </div>
      ) : (
        <>
          <input
            className="input"
            value={title}
            placeholder="Title (defaults to the slug)"
            onChange={(e) => setTitle(e.target.value)}
          />
          <textarea
            className="input mono"
            style={{ minHeight: 320 }}
            value={draft ?? ""}
            onChange={(e) => setDraft(e.target.value)}
          />
        </>
      )}

      {doc.data.children?.length ? (
        <div className="col" style={{ gap: 4 }}>
          <div className="muted text-sm">Children</div>
          {doc.data.children.map((c) => (
            <a key={c.path} className="mono text-sm" onClick={() => onSelect(c.path)}>
              {c.title || c.slug}
            </a>
          ))}
        </div>
      ) : null}

      {childOpen ? (
        <KN_NewDocumentModal
          collection={collection}
          parent={path}
          pushToast={pushToast}
          onClose={() => setChildOpen(false)}
          onCreated={(p) => { setChildOpen(false); onChanged && onChanged(); onSelect(p); }}
        />
      ) : null}
      {moveOpen ? (
        <KN_MoveModal
          collection={collection}
          path={path}
          pushToast={pushToast}
          onClose={() => setMoveOpen(false)}
          onMoved={(p) => { setMoveOpen(false); onChanged && onChanged(); onSelect(p); }}
        />
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Modals: new document, move, import
// ---------------------------------------------------------------------------

function KN_NewDocumentModal({ collection, parent, pushToast, onClose, onCreated }) {
  const { apiFetch } = window.primerApi;
  const [slug, setSlug] = React.useState("");
  const [title, setTitle] = React.useState("");
  const [body, setBody] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      const res = await apiFetch(
        "POST",
        `/collections/${encodeURIComponent(collection.id)}/docs`,
        { parent: parent || "", slug, title: title || null, body },
      );
      pushToast && pushToast({
        kind: "success", title: "Document created", detail: res.document.path,
      });
      onCreated(res.document.path);
    } catch (err) {
      pushToast && pushToast({
        kind: "error", title: "Create failed",
        detail: err?.detail || err?.message,
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="New document" onClose={onClose}>
      <div className="col" style={{ gap: 10 }}>
        <div className="muted text-sm">
          Under <span className="mono">{parent || "(root)"}</span>
        </div>
        <input
          className="input mono"
          placeholder="slug (lowercase letters, digits, hyphens)"
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
        />
        <input
          className="input"
          placeholder="Title (optional)"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <textarea
          className="input mono"
          style={{ minHeight: 200 }}
          placeholder="# Body"
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
        <div className="row" style={{ justifyContent: "flex-end", gap: 6 }}>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn onClick={submit} disabled={busy || !slug}>Create</Btn>
        </div>
      </div>
    </Modal>
  );
}

function KN_MoveModal({ collection, path, pushToast, onClose, onMoved }) {
  const { apiFetch } = window.primerApi;
  const [newParent, setNewParent] = React.useState("");
  const [newSlug, setNewSlug] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      const res = await apiFetch(
        "POST",
        `/collections/${encodeURIComponent(collection.id)}/docs/move`,
        { path, new_parent: newParent, new_slug: newSlug || null },
      );
      pushToast && pushToast({
        kind: "success", title: "Document moved", detail: res.document.path,
      });
      onMoved(res.document.path);
    } catch (err) {
      pushToast && pushToast({
        kind: "error", title: "Move failed",
        detail: err?.detail || err?.message,
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={`Move ${path}`} onClose={onClose}>
      <div className="col" style={{ gap: 10 }}>
        <input
          className="input mono"
          placeholder="New parent path ('' = root)"
          value={newParent}
          onChange={(e) => setNewParent(e.target.value)}
        />
        <input
          className="input mono"
          placeholder="New slug (optional)"
          value={newSlug}
          onChange={(e) => setNewSlug(e.target.value)}
        />
        <div className="muted text-sm">
          The subtree moves with it and document ids are preserved.
        </div>
        <div className="row" style={{ justifyContent: "flex-end", gap: 6 }}>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn onClick={submit} disabled={busy}>Move</Btn>
        </div>
      </div>
    </Modal>
  );
}

function KN_ImportModal({ collection, pushToast, onClose, onDone }) {
  const { apiFetch } = window.primerApi;
  const [parent, setParent] = React.useState("");
  const [conflict, setConflict] = React.useState("fail");
  const [report, setReport] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const fileRef = React.useRef(null);

  const submit = async () => {
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await apiFetch(
        "POST",
        `/collections/${encodeURIComponent(collection.id)}/import`
        + `?parent=${encodeURIComponent(parent)}&conflict=${conflict}`,
        form,
      );
      setReport(res);
      onDone && onDone();
    } catch (err) {
      pushToast && pushToast({
        kind: "error", title: "Import failed",
        detail: err?.detail || err?.message,
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Import a zip archive" onClose={onClose}>
      <div className="col" style={{ gap: 10 }}>
        <input type="file" accept=".zip,application/zip" ref={fileRef} />
        <input
          className="input mono"
          placeholder="Import under (blank = root)"
          value={parent}
          onChange={(e) => setParent(e.target.value)}
        />
        <select className="input" value={conflict} onChange={(e) => setConflict(e.target.value)}>
          <option value="fail">On conflict: fail</option>
          <option value="skip">On conflict: skip</option>
          <option value="overwrite">On conflict: overwrite</option>
        </select>
        {report ? (
          <div className="col" style={{ gap: 4 }}>
            <div className="text-sm">Created {report.created.length}, skipped {report.skipped.length}, overwritten {report.overwritten.length}</div>
            {report.rejected?.length ? (
              <div className="col" style={{ gap: 2 }}>
                <div className="muted text-sm">Rejected</div>
                {report.rejected.map((r) => (
                  <div key={r.file} className="mono text-sm">{r.file}: {r.reason}</div>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
        <div className="row" style={{ justifyContent: "flex-end", gap: 6 }}>
          <Btn kind="ghost" onClick={onClose}>Close</Btn>
          <Btn onClick={submit} disabled={busy}>Import</Btn>
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Grep
// ---------------------------------------------------------------------------

function KN_GrepBox({ collection, onJump }) {
  const { apiFetch } = window.primerApi;
  const [q, setQ] = React.useState("");
  const [pathPrefix, setPathPrefix] = React.useState("");
  const [res, setRes] = React.useState(null);
  const [busy, setBusy] = React.useState(false);

  const run = async () => {
    if (!q) return;
    setBusy(true);
    try {
      const out = await apiFetch(
        "GET",
        `/collections/${encodeURIComponent(collection.id)}/grep`
        + `?q=${encodeURIComponent(q)}`
        + (pathPrefix ? `&path_prefix=${encodeURIComponent(pathPrefix)}` : ""),
      );
      setRes(out);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="col" style={{ gap: 6 }}>
      <div className="row" style={{ gap: 6 }}>
        <input
          className="input mono"
          placeholder="grep (regex)"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") run(); }}
        />
        <input
          className="input mono"
          style={{ maxWidth: 180 }}
          placeholder="path prefix"
          value={pathPrefix}
          onChange={(e) => setPathPrefix(e.target.value)}
        />
        <Btn icon="search" onClick={run} disabled={busy}>Grep</Btn>
      </div>
      {res ? (
        <div className="col" style={{ gap: 2 }}>
          {res.hits.length === 0 ? <div className="muted text-sm">No matches.</div> : null}
          {res.hits.map((h) => (
            <a
              key={`${h.path}:${h.line}`}
              className="mono text-sm"
              onClick={() => onJump(h.path, h.line)}
            >
              {h.path}:{h.line} — {h.excerpt}
            </a>
          ))}
          {res.truncated ? (
            <div className="muted text-sm">Results truncated; narrow the pattern.</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Search ladder + indexing strip
// ---------------------------------------------------------------------------

// uiv2 Wave 2: presentational only. The mockup shows 5 selectable-
// looking chips (auto/semantic/fulltext/literal/regex), but only two
// are REAL, independently switchable backend paths today: semantic
// search (opt-in, PUT /collections/{id}/search) and grep (regex or a
// literal pattern - the SAME endpoint, just a different query, not two
// distinct modes). No server-side "fulltext" or "auto" concept exists
// (grepped primer/api/routers/collections.py - no such route/param) -
// these chips are not clickable and do not claim to switch anything;
// they show which tier is CURRENTLY active (semantic once ready, else
// the always-available grep tier - "documents stay greppable regardless
// of indexing state"), using the mockup's own vocabulary without
// inventing a mode-switch that doesn't exist server-side. Flagged in
// the PR body.
function KN_SearchLadder({ activeRung }) {
  const rungs = ["auto", "semantic", "fulltext", "literal", "regex"];
  return (
    <div className="row" style={{ gap: 6, flexWrap: "wrap" }} data-testid="kn-search-ladder">
      <span className="muted text-sm" style={{ marginRight: 2 }}>search ladder</span>
      {rungs.map((r) => (
        <span key={r}
          className={"chip" + (r === activeRung ? " active" : "")}
          style={{ pointerEvents: "none" }}
          data-testid={`kn-ladder-${r}`}>
          {r}
        </span>
      ))}
    </div>
  );
}

// uiv2 Wave 2 (a-9/a-10): re-homed from a standalone "Semantic search"
// block below the tree into the top-of-overlay indexing strip the
// mockup specs - same fetch/enable/disable logic KN_SearchSettings
// always had, restyled into the strip layout with the ladder beside it.
// The enablement form (a real, must-not-drop capability per the delta)
// folds into the strip's own disabled-state branch rather than living
// as a separate section.
function KN_IndexingStrip({ collection, status, embedProviders, sspProviders, pushToast, onChanged }) {
  const { apiFetch, useCapabilities, capabilityHint } = window.primerApi;
  const caps = useCapabilities();
  const cid = collection.id;
  const [embedder, setEmbedder] = React.useState("");
  const [model, setModel] = React.useState("");
  const [ssp, setSsp] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const enable = async () => {
    setBusy(true);
    try {
      await apiFetch("PUT", `/collections/${encodeURIComponent(cid)}/search`, {
        embedder: { provider_id: embedder, model },
        vector_store_provider_id: ssp,
      });
      pushToast && pushToast({ kind: "success", title: "Search enabled" });
      onChanged && onChanged();
    } catch (err) {
      pushToast && pushToast({
        kind: "error",
        title: "Could not enable search",
        detail: err?.detail || err?.message,
      });
    } finally {
      setBusy(false);
    }
  };

  const disable = async () => {
    setBusy(true);
    try {
      await apiFetch("DELETE", `/collections/${encodeURIComponent(cid)}/search`);
      onChanged && onChanged();
    } finally {
      setBusy(false);
    }
  };

  const st = status.data;
  const ready = st?.state === "ready";
  const activeRung = ready ? "semantic" : "regex";
  const embedRows = embedProviders?.data?.items ?? [];
  const sspRows = sspProviders?.data?.items ?? [];
  // An embedded (lance) vector store needs the extra installed on the
  // server; picking one that is missing fails at enable time with a 409
  // naming it, so say so at the point of choice instead.
  const selectedSspRow = sspRows.find((p) => p.id === ssp);
  const selectedSspMissing =
    !!selectedSspRow &&
    selectedSspRow.provider === "lance" &&
    caps.data?.extras?.lance === false;

  return (
    <div className="col" style={{ gap: 8, padding: "10px 14px", borderBottom: "1px solid var(--border)" }}
      data-testid="kn-indexing-strip">
      <div className="row" style={{ gap: 10, alignItems: "center", justifyContent: "space-between", flexWrap: "wrap" }}>
        <div className="row" style={{ gap: 8, alignItems: "center" }}>
          <span className="muted text-sm" style={{ textTransform: "uppercase", fontSize: 10.5, letterSpacing: "0.06em" }}>
            Semantic indexing
          </span>
          {st ? <StatusPill status={ready ? "ok" : st.state === "error" ? "error" : "paused"} /> : null}
          <span className="mono text-sm">
            {st ? st.state : "…"}
            {st && st.state !== "disabled" ? ` — ${st.documents_indexed}/${st.documents_total} docs` : ""}
          </span>
        </div>
        <KN_SearchLadder activeRung={activeRung} />
      </div>

      {st?.state === "error" ? (
        <Banner kind="error" title="Indexing failed">{st.error}</Banner>
      ) : null}

      {st?.state === "disabled" ? (
        <div className="col" style={{ gap: 6 }}>
          <div className="muted text-sm">
            Grep and the document tree work without this. Enable it for
            meaning-based search.
          </div>
          {embedRows.length === 0 || sspRows.length === 0 ? (
            <Banner kind="info" title="Providers required">
              Register an embedding provider and a semantic search provider
              first; enabling without them returns a conflict naming the
              missing id.
            </Banner>
          ) : null}
          <select className="input" value={embedder} onChange={(e) => setEmbedder(e.target.value)}>
            <option value="">Embedding provider…</option>
            {embedRows.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
          </select>
          <input
            className="input mono"
            placeholder="Embedding model"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          />
          <select className="input" value={ssp} onChange={(e) => setSsp(e.target.value)}>
            <option value="">Vector store…</option>
            {sspRows.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
          </select>
          {selectedSspMissing ? (
            <div className="field-help" style={{ color: "var(--amber)" }}
              data-capability-hint="lance">
              {capabilityHint("lance")}
            </div>
          ) : null}
          <Btn size="sm" onClick={enable} disabled={busy || !embedder || !model || !ssp}>
            Enable search
          </Btn>
        </div>
      ) : (
        <Btn kind="ghost" size="sm" onClick={disable} disabled={busy}>Disable search</Btn>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Collection detail
// ---------------------------------------------------------------------------

function KN_CollectionDetail({ collection, embedProviders, sspProviders, cerProviders, pushToast, onBack }) {
  const { useResource, apiFetch } = window.primerApi;
  const [expanded, setExpanded] = React.useState({});
  const [selectedPath, setSelectedPath] = React.useState(null);
  const [reloadKey, setReloadKey] = React.useState(0);
  const [importOpen, setImportOpen] = React.useState(false);
  const [rootNewOpen, setRootNewOpen] = React.useState(false);
  const readOnly = !!collection.system;
  const cid = collection.id;

  // Lifted from the old KN_SearchSettings (uiv2 Wave 2): the header
  // facts row needs the same search-status fetch the indexing strip
  // does, so it is fetched once here and threaded to both rather than
  // each fetching its own copy.
  const status = useResource(
    `kn:search-status:${cid}:${reloadKey}`,
    (signal) => apiFetch("GET", `/collections/${encodeURIComponent(cid)}/search`, null, { signal }),
    { pollMs: null },
  );
  const st = status.data;

  const toggle = (path) =>
    setExpanded((cur) => ({ ...cur, [path]: !cur[path] }));
  const bump = () => setReloadKey((k) => k + 1);

  const jump = (path) => {
    setSelectedPath(path);
    const parts = path.split("/");
    const opens = {};
    for (let i = 1; i < parts.length; i += 1) {
      opens[parts.slice(0, i).join("/")] = true;
    }
    setExpanded((cur) => ({ ...cur, ...opens }));
  };

  // uiv2 Wave 2 ("Collection — {name}" header row, type/state/doc-count
  // facts inline, Import zip + close): doc count is only shown when the
  // search-status fetch actually serves one (documents_total, once
  // search is configured) - a collection with search disabled has no
  // cheap accurate total-document count available (the tree endpoint is
  // one level at a time), so it is omitted rather than guessed.
  const title = (
    <h1 className="page-title" style={{ font: "inherit", margin: 0, display: "inline" }}>
      Collection — {cid}
      <span className="pc-modal-chip mono text-sm muted" style={{ marginLeft: 10, marginBottom: 0, verticalAlign: "middle" }}>
        {readOnly ? "read-only" : st ? (st.state === "ready" ? "semantic" : st.state) : "…"}
      </span>
      {st && st.state !== "disabled" ? (
        <span className="muted text-sm" style={{ marginLeft: 8 }}>{st.documents_total} documents</span>
      ) : null}
    </h1>
  );

  return (
    <Modal
      title={title}
      width={860}
      onClose={onBack}
      footer={
        readOnly ? null : (
          <>
            <Btn kind="ghost" icon="upload" onClick={() => setImportOpen(true)}>Import zip</Btn>
            <Btn icon="plus" onClick={() => setRootNewOpen(true)}>New document</Btn>
          </>
        )
      }
    >
      <div className="col" style={{ gap: 0, margin: "-16px" }}>
        {/* Mobile-only (@media max-width: 639px in styles.css) - Modal's
            own mobile variant is a bottom sheet with just a bare ×
            close; this names where "back" actually goes, same as
            before this wave's desktop reconciliation. */}
        <a className="knowledge-mobile-back" onClick={onBack}>
          <Icon name="chevron-left" /> Collections
        </a>
        <KN_IndexingStrip
          collection={collection}
          status={status}
          embedProviders={embedProviders}
          sspProviders={sspProviders}
          pushToast={pushToast}
          onChanged={bump}
        />
        <div className="row" style={{ gap: 16, alignItems: "flex-start", padding: "14px 16px 16px" }}>
          <div className="col kn-tree" style={{ minWidth: 240, maxWidth: 320, gap: 8 }}>
            {/* uiv2 Wave 2 (a-9, re-homed not deleted): the grep bar now
                lives at the top of the tree column, matching the
                delta's own suggested home - documents stay greppable
                regardless of indexing state, so this control belongs
                beside the tree it jumps within, not floating above the
                whole overlay. */}
            <KN_GrepBox collection={collection} onJump={jump} />
            <KN_Tree
              cid={cid}
              parent=""
              depth={0}
              expanded={expanded}
              toggle={toggle}
              selectedPath={selectedPath}
              onSelect={setSelectedPath}
              reloadKey={reloadKey}
            />
          </div>
          <div className="col" style={{ flex: 1, minWidth: 0 }}>
            {selectedPath ? (
              <KN_DocumentPane
                collection={collection}
                path={selectedPath}
                readOnly={readOnly}
                pushToast={pushToast}
                onChanged={bump}
                onSelect={setSelectedPath}
              />
            ) : (
              <KN_EmptyState
                ico="book"
                head="Select a document"
                sub="Pick a node on the left, or grep to jump straight to a line."
              />
            )}
          </div>
        </div>
      </div>

      {importOpen ? (
        <KN_ImportModal
          collection={collection}
          pushToast={pushToast}
          onClose={() => setImportOpen(false)}
          onDone={bump}
        />
      ) : null}
      {rootNewOpen ? (
        <KN_NewDocumentModal
          collection={collection}
          parent=""
          pushToast={pushToast}
          onClose={() => setRootNewOpen(false)}
          onCreated={(p) => { setRootNewOpen(false); bump(); setSelectedPath(p); }}
        />
      ) : null}
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// New collection
// ---------------------------------------------------------------------------

function KN_NewCollectionModal({ pushToast, onClose, onCreate }) {
  const { apiFetch } = window.primerApi;
  const [id, setId] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      const body = { description };
      if (id) body.id = id;
      const row = await apiFetch("POST", "/collections", body);
      // Say it worked. The four document actions in this file all
      // confirm themselves; creating the COLLECTION they live in said
      // nothing, so the only sign was the view changing underneath you.
      pushToast && pushToast({
        kind: "success", title: "Collection created", detail: row.id,
      });
      onCreate(row);
    } catch (err) {
      pushToast && pushToast({
        kind: "error", title: "Create failed",
        detail: err?.detail || err?.message,
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="New collection" onClose={onClose}>
      <div className="col" style={{ gap: 10 }}>
        <input
          className="input mono"
          placeholder="id (optional)"
          value={id}
          onChange={(e) => setId(e.target.value)}
        />
        <input
          className="input"
          placeholder="Description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <div className="muted text-sm">
          A collection is a wiki of text documents. Semantic search is
          opt-in per collection from its search settings.
        </div>
        <div className="row" style={{ justifyContent: "flex-end", gap: 6 }}>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn onClick={submit} disabled={busy || !description}>Create</Btn>
        </div>
      </div>
    </Modal>
  );
}

// ============================================================================
// Page
// ============================================================================

function CollectionsPage({ pushToast, onOpen, onNavigate, selectedId }) {
  const { useResource, useRouter, useViewport, apiFetch, usePagedList, Pager } = window.primerApi;
  const { isMobile } = useViewport();

  const embedProviders = useResource(
    "collections:embedding-providers",
    (signal) => apiFetch("GET", "/embedding_providers?limit=200", null, { signal }),
    { pollMs: null },
  );
  const sspProviders = useResource(
    "collections:ssp",
    (signal) => apiFetch("GET", "/ssp?limit=200", null, { signal }),
    { pollMs: null },
  );
  const cerProviders = useResource(
    "collections:cer-providers",
    (signal) => apiFetch("GET", "/cross_encoder_providers?limit=200", null, { signal }),
    { pollMs: null },
  );

  const [createOpen, setCreateOpen] = React.useState(false);
  const [reloadKey, setReloadKey] = React.useState(0);

  const list = useResource(
    `collections:list:${reloadKey}`,
    (signal) => apiFetch("GET", "/collections?limit=200", null, { signal }),
    { pollMs: null },
  );
  const rows = list.data?.items ?? [];

  // Which collection is open is ADDRESSED, not remembered. It used to
  // live in local state and never reach the url, so the address said
  // "collections" whichever one you were inside; navigating back to the
  // list left you in the collection, because nothing about the request
  // differed from where you already were. The id slot decides which
  // renders, the same way it does for every other overlay here.
  const [localSelected, setLocalSelected] = React.useState(null);
  const addressed = selectedId
    ? rows.find((c) => c.id === selectedId) || null
    : null;
  const selected = onNavigate ? addressed : localSelected;
  const select = (row) => {
    if (onNavigate) onNavigate(row ? row.id : null);
    else setLocalSelected(row);
  };
  const setSelected = select;

  if (selected) {
    return (
      <KN_CollectionDetail
        collection={selected}
        embedProviders={embedProviders}
        sspProviders={sspProviders}
        cerProviders={cerProviders}
        pushToast={pushToast}
        onBack={() => setSelected(null)}
      />
    );
  }

  const cards = rows.map((c) => ({
    id: c.id,
    title: c.id,
    subtitle: c.description,
    meta: c.system ? "system - read-only" : (c.search ? "search enabled" : "grep only"),
    row: c,
  }));

  return (
    <div className="col" style={{ gap: 12 }}>
      {list.error ? (
        <Banner kind="error" title="Could not load collections">
          {String(list.error.detail || list.error.message || list.error)}
        </Banner>
      ) : null}

      {isMobile ? (
        <>
          <CardList
            items={cards}
            renderCard={(item) => (
              <Card
                title={item.title}
                subtitle={item.subtitle}
                meta={item.meta}
                onClick={() => setSelected(item.row)}
              />
            )}
            empty={<KN_EmptyState ico="book" head="No collections yet" />}
          />
          <Fab icon="plus" label="New collection" onClick={() => setCreateOpen(true)} />
        </>
      ) : (
        <>
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <Btn icon="plus" onClick={() => setCreateOpen(true)}>New collection</Btn>
          </div>
          {rows.length === 0 ? (
            <KN_EmptyState
              ico="book"
              head="No collections yet"
              sub="A collection is a wiki of text documents; search is optional."
            />
          ) : (
            <table className="table">
              <thead>
                <tr><th>id</th><th>description</th><th>search</th><th /></tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  // The whole row opens the collection, as every other
                  // list in this console does. It used to be the small
                  // "Open" button in the last cell alone, so clicking a
                  // collection anywhere else did nothing at all and gave
                  // no hint that it should have.
                  <tr key={c.id} style={{ cursor: "pointer" }}
                    onClick={() => setSelected(c)}>
                    <td className="mono">
                      {c.id} {c.system ? <Icon name="lock" /> : null}
                    </td>
                    <td className="muted text-sm">{c.description}</td>
                    <td className="mono muted text-sm">
                      {c.search ? c.search.state : "disabled"}
                    </td>
                    <td>
                      <Btn kind="ghost" onClick={(e) => {
                        e.stopPropagation();
                        setSelected(c);
                      }}>Open</Btn>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}

      {createOpen ? (
        <KN_NewCollectionModal
          pushToast={pushToast}
          onClose={() => setCreateOpen(false)}
          onCreate={(row) => {
            setCreateOpen(false);
            setReloadKey((k) => k + 1);
            setSelected(row);
          }}
        />
      ) : null}
    </div>
  );
}

window.CollectionsPage = CollectionsPage;
