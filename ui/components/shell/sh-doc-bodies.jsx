/* global React, SH_api, SH_useShell, SH_parseAnchor */
// The three non-session doc bodies. Each is a TAB, never an overlay:
// section 8 puts comparison and deep editing in the center and reserves
// overlays for shallow one-decision tasks.

// 1MB: past this the line renderer and textarea both stop being an
// editor anyone would want; the gate offers download instead.
var SH_FILE_EDIT_MAX_BYTES = 1024 * 1024;

function SH_FileDoc(props) {
  var shell = SH_useShell();
  var path = props.path;
  var read = window.primerApi.useResource(
    SH_api.keys.file(shell.wid, path),
    function (signal) { return SH_api.fileRead(shell.wid, path, signal); },
    { pollMs: 0, deps: [shell.wid, path] }
  );
  var draftState = React.useState(null);
  var draft = draftState[0];
  var setDraft = draftState[1];
  // "conflict" when a save came back 412: the file changed on disk
  // under the draft (revamp section 6).
  var conflictState = React.useState(false);
  var conflict = conflictState[0];
  var setConflict = conflictState[1];

  var meta = read.data || {};
  var etag = meta.etag || null;
  var binary = meta.encoding === "base64";
  var tooBig = (meta.size_bytes || 0) > SH_FILE_EDIT_MAX_BYTES;
  var content = draft == null ? (meta.content || "") : draft;
  var lines = String(content).split("\n");
  var anchor = SH_parseAnchor(shell.anchor);
  var from = anchor && anchor.kind === "lines" ? anchor.from : null;
  var to = anchor && anchor.kind === "lines" ? (anchor.to || anchor.from) : null;

  function edit(next) {
    // Edit promotes: a preview tab that kept its italics would be reused
    // out from under the typed character on the next single click.
    setDraft(next);
    shell.promoteDoc("file:" + path);
  }

  function save(force) {
    if (draft == null) return;
    // The etag from the read makes the write conditional; 412 means the
    // file changed on disk since - never silently clobber it.
    SH_api.fileWrite(shell.wid, path, draft, force ? null : etag).then(
      function () {
        setDraft(null);
        setConflict(false);
        read.refetch();
        shell.toast("Saved " + path);
      },
      function (err) {
        if (err && err.status === 412) { setConflict(true); return; }
        shell.toast("Save failed: " + (err && err.message));
      }
    );
  }

  // Ctrl+S saves the dirty doc; the browser's own save dialog never
  // helps anyone edit a workspace file.
  React.useEffect(function () {
    function onKey(ev) {
      if ((ev.ctrlKey || ev.metaKey) && String(ev.key).toLowerCase() === "s") {
        ev.preventDefault();
        save(false);
      }
    }
    window.addEventListener("keydown", onKey);
    return function () { window.removeEventListener("keydown", onKey); };
  });

  if (binary || tooBig) {
    // The gate: binary or oversized content downloads, never renders.
    return (
      <div className="sh-file" data-testid={"shell-file:" + path}>
        <div className="sh-empty">
          <span>
            {path} is {binary ? "binary" : "large"}
            {meta.size_bytes ? " (" + meta.size_bytes + " bytes)" : ""}
            {"; it opens outside the editor."}
          </span>
          <a className="sh-verb" data-testid="file-download-gate"
            href={SH_api.fileDownloadUrl(shell.wid, path)} download>
            Download
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="sh-file" data-testid={"shell-file:" + path}>
      <div className="sh-file-bar">
        <span className="sh-file-path" data-testid="file-breadcrumb">{path}</span>
        {draft != null ? (
          <span className="sh-chip" data-testid="file-dirty">modified</span>
        ) : null}
        <button type="button" className="sh-verb" data-testid="shell-doc-save"
          disabled={draft == null}
          onClick={function () { save(false); }}>Save File</button>
      </div>
      {conflict ? (
        <div className="sh-file-conflict" data-testid="file-conflict-banner">
          <span>
            This file changed on disk while you edited it. Reload to take
            the new version (your draft is lost), or Overwrite to keep
            yours.
          </span>
          <button type="button" className="sh-verb"
            data-testid="file-conflict-reload"
            onClick={function () {
              setDraft(null);
              setConflict(false);
              read.refetch();
            }}>Reload</button>
          <button type="button" className="sh-verb"
            data-testid="file-conflict-overwrite"
            onClick={function () { save(true); }}>Overwrite</button>
        </div>
      ) : null}
      <ol className="sh-file-body">
        {lines.map(function (text, i) {
          var n = i + 1;
          var hit = from != null && n >= from && n <= to;
          return (
            <li key={n} data-testid={"shell-file-line:" + n} data-anchor={hit}>
              <code>{text}</code>
            </li>
          );
        })}
      </ol>
      <textarea className="sh-file-edit" value={content}
        onChange={function (ev) { edit(ev.target.value); }} />
    </div>
  );
}

function SH_DiffDoc(props) {
  var shell = SH_useShell();
  var sha = props.sha;
  var commit = window.primerApi.useResource(
    SH_api.keys.commit(shell.wid, sha),
    function (signal) { return SH_api.commit(shell.wid, sha, signal); },
    { pollMs: 0, deps: [shell.wid, sha] }
  );
  var files = (commit.data && commit.data.files) || [];

  return (
    <div className="sh-diff" data-testid={"shell-diff:" + sha}>
      <div className="sh-diff-head">
        <span className="sh-diff-sha">{sha}</span>
        <span className="sh-diff-subject">{commit.data && commit.data.subject}</span>
      </div>
      {files.map(function (file) {
        return (
          <section key={file.path} className="sh-diff-file">
            <h4>{file.path}</h4>
            {/* Side by side, in the tab. Diffs never go in an overlay. */}
            <div className="sh-diff-split">
              <pre className="sh-diff-before">{file.before || ""}</pre>
              <pre className="sh-diff-after">{file.after || file.patch || ""}</pre>
            </div>
          </section>
        );
      })}
    </div>
  );
}

// wiki:<collection-id>/<slug/path>. The first segment names the
// collection; everything after it is S2's path of slugs.
function SH_WikiDoc(props) {
  var shell = SH_useShell();
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
    <div className="sh-wiki" data-testid={"shell-wiki:" + slug}>
      <h3>{(body.document && body.document.title) || path || cid}</h3>
      <article className="sh-wiki-body">{body.content || ""}</article>
      {shell.registry.forSurface("tab-menu").map(function (verb) {
        if (verb.contexts && verb.contexts.indexOf("wiki") < 0) return null;
        return (
          <button key={verb.id} type="button" className="sh-verb"
            data-verb={verb.id} onClick={function () { verb.run(); }}
          >{verb.label}</button>
        );
      })}
    </div>
  );
}

window.SH_FileDoc = SH_FileDoc;
window.SH_DiffDoc = SH_DiffDoc;
window.SH_WikiDoc = SH_WikiDoc;
