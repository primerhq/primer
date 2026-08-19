/* global React, SH_api, SH_useShell, SH_parseAnchor */
// The three non-session doc bodies. Each is a TAB, never an overlay:
// section 8 puts comparison and deep editing in the center and reserves
// overlays for shallow one-decision tasks.

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

  var content = draft == null ? ((read.data && read.data.content) || "") : draft;
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

  return (
    <div className="sh-file" data-testid={"shell-file:" + path}>
      <div className="sh-file-bar">
        <span className="sh-file-path">{path}</span>
        <button type="button" className="sh-verb" data-testid="shell-doc-save"
          disabled={draft == null}
          onClick={function () {
            SH_api.fileWrite(shell.wid, path, draft).then(function () {
              setDraft(null);
              read.refetch();
              shell.toast("Saved " + path);
            });
          }}>Save File</button>
      </div>
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
