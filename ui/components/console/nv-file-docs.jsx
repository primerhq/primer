/* global React, SH_api, NV_useConsole */
// Non-session docs (wiring plan P2 T7): the file editor with the
// etag/412 changed-on-disk discipline, the diff doc, and the wiki
// doc, restyled to the prototype's FILE DOC / DIFF DOC / WIKI DOC.

var NV_FILE_EDIT_MAX_BYTES = 1024 * 1024;

// GU-6: unsaved file drafts, keyed by workspace and path (the key of gap review GU-1). Each tab group renders only its active tab's document, so a
// tab switch unmounts the file doc; a draft that lived in component state went with it. This map outlives the unmount, so the draft is there when
// the tab comes back, and the tab bar reads it to mark a tab that holds one and to ask before closing it.
var NV_FILE_DRAFTS = {};
var NV_FILE_DRAFT_LISTENERS = [];
function NV_fileDraftKey(wid, path) { return wid + ":" + path; }
function NV_fileDraftGet(key) { return Object.prototype.hasOwnProperty.call(NV_FILE_DRAFTS, key) ? NV_FILE_DRAFTS[key] : null; }
function NV_fileDraftSet(key, value) {
  if (value == null) delete NV_FILE_DRAFTS[key]; else NV_FILE_DRAFTS[key] = value;
  NV_FILE_DRAFT_LISTENERS.slice().forEach(function (fn) { fn(); });
}
// Re-renders the caller when any draft appears or goes; answers {dirty(path), confirmClose(tab)} for the current workspace.
function NV_useFileDrafts() {
  var con = NV_useConsole();
  var tick = React.useState(0);
  var bump = tick[1];
  React.useEffect(function () {
    var fn = function () { bump(function (n) { return n + 1; }); };
    NV_FILE_DRAFT_LISTENERS.push(fn);
    return function () { NV_FILE_DRAFT_LISTENERS = NV_FILE_DRAFT_LISTENERS.filter(function (f) { return f !== fn; }); };
  }, []);
  return {
    dirty: function (path) { return NV_fileDraftGet(NV_fileDraftKey(con.wid, path)) != null; },
    confirmClose: function (tab) { return NV_confirmCloseTab(con.wid, tab); },
  };
}
// Closing a file tab that holds a draft asks first; true when the tab may close (a "yes" drops the draft). Other tabs close without a question.
async function NV_confirmCloseTab(wid, tab) {
  if (!tab || tab.kind !== "file") return true;
  var key = NV_fileDraftKey(wid, tab.ref);
  if (NV_fileDraftGet(key) == null) return true;
  var ok = await window.confirmDialog({
    title: "Discard unsaved changes",
    message: tab.ref + " has changes that are not saved. Closing the tab discards them.",
    confirmLabel: "Discard", danger: true,
  });
  if (ok) NV_fileDraftSet(key, null);
  return !!ok;
}
window.NV_useFileDrafts = NV_useFileDrafts;
window.NV_confirmCloseTab = NV_confirmCloseTab;

function NV_FileDoc(props) {
  var con = NV_useConsole();
  var path = props.path;
  var read = window.primerApi.useResource(
    SH_api.keys.file(con.wid, path),
    function (signal) { return SH_api.fileRead(con.wid, path, signal); },
    { pollMs: 0, deps: [con.wid, path] }
  );
  var draftKey = NV_fileDraftKey(con.wid, path);
  var draftState = React.useState(function () { return NV_fileDraftGet(draftKey); });
  var draft = draftState[0];
  var setDraftState = draftState[1];
  // The map is the draft's home (it outlives this component); the state only re-renders it. Accepts a value or an updater, like a state setter.
  function setDraft(next) {
    var value = typeof next === "function" ? next(NV_fileDraftGet(draftKey)) : next;
    NV_fileDraftSet(draftKey, value);
    setDraftState(value);
  }
  var conflictState = React.useState(false);
  var conflict = conflictState[0];
  var setConflict = conflictState[1];
  // The last successful save's body plus the read object the save went out
  // against. The render shows the body only while that read is still the
  // read on file: a refetch landing different content (an agent's write in
  // between) must show that content in the same render, because the save's
  // etag no longer matches the disk. A refetch that keeps the same read
  // object (or identical content) still shows the body, which equals it.
  var savedRef = React.useRef(null);
  // A save already in flight; a second save would ride the same etag out
  // and 412 into a false "changed on disk" banner.
  var savingRef = React.useRef(false);

  var meta = read.data || {};
  var etag = meta.etag || null;
  var binary = meta.encoding === "base64";
  var tooBig = (meta.size_bytes || 0) > NV_FILE_EDIT_MAX_BYTES;
  var saved = savedRef.current;
  var content = draft != null ? draft
    : (saved && saved.data === read.data ? saved.body : (meta.content || ""));

  function save(force) {
    if (draft == null) return;
    if (savingRef.current) return;
    savingRef.current = true;
    var sent = draft;
    SH_api.fileWrite(con.wid, path, sent, force ? null : etag).then(
      function () {
        savingRef.current = false;
        // Keystrokes typed while the write was out are the next edit:
        // clear the draft only if it is still exactly what went out.
        setDraft(function (cur) { return cur === sent ? null : cur; });
        savedRef.current = { body: sent, data: read.data };
        setConflict(false);
        read.refetch();
        con.toast("Saved " + path);
      },
      function (err) {
        savingRef.current = false;
        if (err && err.status === 412) { setConflict(true); return; }
        con.toast("Save failed: " + (err && (err.detail || err.message)));
      }
    );
  }

  React.useEffect(function () {
    function onKey(ev) {
      if (!((ev.ctrlKey || ev.metaKey) && String(ev.key).toLowerCase() === "s")) return;
      // Only this editor saves: with two file docs open, one Ctrl+S must
      // not write both, and a Ctrl+S aimed at another surface's input
      // must not write at all. The nearest file-doc root of the focused
      // element is this doc's root exactly when focus is inside it.
      var el = document.activeElement;
      var root = el && el.closest ? el.closest('[data-testid^="nv-file-doc:"]') : null;
      if (!root || root.getAttribute("data-testid") !== "nv-file-doc:" + path) return;
      ev.preventDefault();
      save(false);
    }
    window.addEventListener("keydown", onKey);
    return function () { window.removeEventListener("keydown", onKey); };
  });

  // A refused read (403: a non-admin naming the runtime's .state / .tmp,
  // or any other failure) shows why instead of an empty editor that a
  // save would turn into a write.
  if (read.error && !read.data) {
    var e = read.error;
    var why = (typeof e.detail === "string" ? e.detail
      : (e.detail && e.detail.message)) || e.message || e.title
      || "the read failed";
    return (
      <div className="nv-filedoc" data-testid={"nv-file-doc:" + path}>
        <div className="nv-rail-empty" data-testid="nv-file-doc-error">
          <div>
            Can't open {path}: {String(why)}
          </div>
        </div>
      </div>
    );
  }

  if (binary || tooBig) {
    return (
      <div className="nv-filedoc" data-testid={"nv-file-doc:" + path}>
        <div className="nv-rail-empty">
          <div>
            {path} is {binary ? "binary" : "large"}; it opens outside
            the editor.
          </div>
          <a className="nv-btn-secondary" data-testid="nv-file-gate"
            href={SH_api.fileDownloadUrl(con.wid, path)} download>
            Download
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="nv-filedoc" data-testid={"nv-file-doc:" + path}>
      <div className="nv-filedoc-bar">
        <span className="nv-filedoc-path">{path}</span>
        {draft != null ? (
          <span className="nv-filedoc-dirty" data-testid="nv-file-dirty">
            modified
          </span>
        ) : null}
        <span style={{ flex: 1 }} />
        <button type="button" className="nv-btn-secondary"
          data-testid="nv-file-save" disabled={draft == null}
          onClick={function () { save(false); }}>Save</button>
      </div>
      {conflict ? (
        <div className="nv-conflict" data-testid="nv-file-conflict">
          <span>
            This file changed on disk while you edited it. Reload takes
            the new version (your draft is lost); Overwrite keeps yours.
          </span>
          <button type="button" className="nv-btn-secondary"
            onClick={function () {
              setDraft(null);
              setConflict(false);
              read.refetch();
            }}>Reload</button>
          <button type="button" className="nv-btn-secondary"
            onClick={function () { save(true); }}>Overwrite</button>
        </div>
      ) : null}
      <textarea className="nv-file-edit" value={content}
        data-testid="nv-file-edit"
        spellCheck={false}
        onChange={function (ev) {
          setDraft(ev.target.value);
          if (con.promoteDoc) con.promoteDoc("file:" + path);
        }} />
    </div>
  );
}

function NV_DiffDoc(props) {
  var con = NV_useConsole();
  var sha = props.sha;
  var commit = window.primerApi.useResource(
    SH_api.keys.commit(con.wid, sha),
    function (signal) { return SH_api.commit(con.wid, sha, signal); },
    { pollMs: 0, deps: [con.wid, sha] }
  );
  var files = (commit.data && commit.data.files) || [];
  // A commit read that is still in flight (or failed) is not an empty commit: say what
  // is happening instead of rendering "Empty commit." for a data: null row (G12-04),
  // in the file DOC's read-error tone.
  var commitErr = (commit.error && !commit.data);
  var commitErrWhy = commitErr ? (function (e) {
    return (typeof e.detail === "string" ? e.detail
      : (e.detail && e.detail.message)) || e.message || e.title || "the read failed";
  })(commit.error) : null;
  return (
    <div className="nv-diffdoc" data-testid={"nv-diff-doc:" + sha}>
      <div className="nv-filedoc-bar">
        <span className="nv-commit-sha">{String(sha).slice(0, 7)}</span>
        <span className="nv-filedoc-path">
          {commit.data && commit.data.subject}
        </span>
      </div>
      <div className="nv-diff-body">
        {commitErr ? (
          <div className="nv-rail-empty" data-testid="nv-diff-error">
            <div>Could not open this commit: {String(commitErrWhy)}</div>
          </div>
        ) : commit.data == null ? (
          <div className="nv-rail-empty" data-testid="nv-diff-loading"><div>Loading commit…</div></div>
        ) : null}
        {files.map(function (f) {
          var lines = String(f.patch || "").split("\n");
          return (
            <section key={f.path} className="nv-diff-file">
              <div className="nv-diff-file-head">{f.path}</div>
              <pre className="nv-diff-lines">
                {lines.map(function (l, i) {
                  var tone = l.charAt(0) === "+" ? "add"
                    : l.charAt(0) === "-" ? "del" : "ctx";
                  return (
                    <div key={i} className="nv-diff-line" data-tone={tone}>
                      {l}
                    </div>
                  );
                })}
              </pre>
            </section>
          );
        })}
        {!files.length && commit.data && commit.data.files_hidden ? (
          <div className="nv-rail-empty" data-testid="nv-diff-hidden">
            <div>This commit changes the workspace runtime's state; its file changes are visible to admins only.</div>
          </div>
        ) : null}
        {commit.data != null && !files.length && !(commit.data && commit.data.files_hidden) ? (
          <div className="nv-rail-empty"><div>Empty commit.</div></div>
        ) : null}
      </div>
    </div>
  );
}

function NV_WikiDoc(props) {
  var slug = String(props.slug || "");
  var cut = slug.indexOf("/");
  var cid = cut < 0 ? slug : slug.slice(0, cut);
  var path = cut < 0 ? "" : slug.slice(cut + 1);
  var doc = window.primerApi.useResource(
    SH_api.keys.document(cid, path),
    function (signal) { return SH_api.collectionDocument(cid, path, signal); },
    { pollMs: 0, deps: [cid, path] }
  );
  var body = doc.data || {};
  return (
    <div className="nv-wikidoc" data-testid={"nv-wiki-doc:" + slug}>
      <h3 className="nv-wiki-title">
        {(body.document && body.document.title) || path || cid}
      </h3>
      <article className="nv-wiki-body">{body.content || ""}</article>
    </div>
  );
}

window.NV_FileDoc = NV_FileDoc;
window.NV_DiffDoc = NV_DiffDoc;
window.NV_WikiDoc = NV_WikiDoc;
