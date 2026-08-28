/* global React, SH_api, NV_useConsole */
// Files sidebar (uiv2 R2 cutover, US-011a): always-visible right panel
// per implementer-notes 2.5 - lazy one-level tree with full management
// (new/upload/rename/delete/download/copy-path via header verbs +
// right-click), and the History toggle listing turn commits that open
// diff tabs.
//
// Header verbs used to be a SEPARATE component (NV_FilesSidebarVerbs)
// sharing state with this one through a module ref + change event,
// because nv-studio.jsx's old Sessions|Files tab bar rendered them as
// siblings. That tab bar is retired (Files is its own panel now, no
// tab to share a header row with), so the verbs render inline here
// with plain closures - the ref/event indirection had no reason left
// to exist once verbs and body share one component.

function NV_FileContextMenu(props) {
  var con = NV_useConsole();
  var f = props.entry;
  var rows = [];
  if (!f.is_dir) {
    rows.push({ label: "Open", fn: function () { props.onOpen(f); } });
  }
  rows.push({
    label: "Rename",
    fn: function () {
      promptDialog({
        title: "Rename " + f.path, defaultValue: f.path,
      }).then(function (dst) {
        if (!dst || dst === f.path) return;
        SH_api.fileMove(con.wid, f.path, dst).then(props.onChanged);
      });
    },
  });
  rows.push({
    label: "Delete", danger: true,
    fn: function () {
      confirmDialog({
        title: "Delete " + f.path,
        message: f.is_dir
          ? "Delete this folder and everything in it?"
          : "Delete this file?",
        danger: true,
      }).then(function (ok) {
        if (!ok) return;
        SH_api.fileDelete(con.wid, f.path).then(props.onChanged);
      });
    },
  });
  if (!f.is_dir) {
    rows.push({
      label: "Download",
      fn: function () {
        var a = document.createElement("a");
        a.href = SH_api.fileDownloadUrl(con.wid, f.path);
        a.download = "";
        a.click();
      },
    });
  }
  rows.push({
    label: "Copy Path",
    fn: function () {
      try { navigator.clipboard.writeText(f.path); } catch (_e) { /* noop */ }
      con.toast("Copied " + f.path);
    },
  });
  return (
    <div className="nv-ctx" data-testid={"nv-file-menu:" + f.path}
      style={{ left: props.x, top: props.y }}
      onClick={function (ev) { ev.stopPropagation(); }}>
      {rows.map(function (r) {
        return (
          <button type="button" key={r.label} className="nv-menu-row"
            data-danger={r.danger ? "true" : "false"}
            onClick={function () { props.onClose(); r.fn(); }}>
            {r.label}
          </button>
        );
      })}
    </div>
  );
}

function NV_FilesSubtree(props) {
  var con = NV_useConsole();
  var tree = window.primerApi.useResource(
    SH_api.keys.tree(con.wid, props.path),
    function (signal) { return SH_api.filesTree(con.wid, props.path, signal); },
    { pollMs: 5000, deps: [con.wid, props.path] }
  );
  var items = (tree.data && tree.data.items) || [];
  return (
    <React.Fragment>
      {items.map(function (entry) {
        return props.row(entry, props.depth);
      })}
    </React.Fragment>
  );
}

function NV_FilesSidebar() {
  var con = NV_useConsole();
  var tree = window.primerApi.useResource(
    SH_api.keys.tree(con.wid, "."),
    function (signal) { return SH_api.filesTree(con.wid, ".", signal); },
    { pollMs: 5000, deps: [con.wid] }
  );
  var commits = window.primerApi.useResource(
    SH_api.keys.log(con.wid),
    function (signal) { return SH_api.commitLog(con.wid, 50, signal); },
    { pollMs: 0, deps: [con.wid] }
  );
  var openState = React.useState({});
  var open = openState[0];
  var setOpen = openState[1];
  var historyState = React.useState(false);
  var history = historyState[0];
  var setHistory = historyState[1];
  var menuState = React.useState(null);
  var menu = menuState[0];
  var setMenu = menuState[1];
  React.useEffect(function () {
    function close() { setMenu(null); }
    document.addEventListener("click", close);
    return function () { document.removeEventListener("click", close); };
  }, []);

  function refetch() { tree.refetch(); }

  function upload(fileList, dirPath) {
    Array.prototype.slice.call(fileList || []).forEach(function (f) {
      var reader = new FileReader();
      reader.onload = function () {
        var b64 = String(reader.result).split(",")[1] || "";
        var dest = (dirPath ? dirPath + "/" : "") + f.name;
        SH_api.fileUpload(con.wid, dest, b64).then(function () {
          con.toast("Uploaded " + dest);
          refetch();
        }, function (err) { con.toast("Upload failed: " + err.message); });
      };
      reader.readAsDataURL(f);
    });
  }

  function openFile(entry, promote) {
    con.setDoc({ kind: "file", ref: entry.path });
    // VS Code tab semantics (BDD pass 2026-08-24): double-click
    // promotes the preview to a permanent tab, same as session rows.
    if (promote && con.promoteDoc) con.promoteDoc("file:" + entry.path);
  }

  function row(entry, depth) {
    var expanded = !!open[entry.path];
    return (
      <React.Fragment key={entry.path}>
        {/* A real button: focus, Enter and Space come from the
            platform, not a role/tabindex retrofit (FC5). */}
        <button type="button" className="nv-file-row"
          style={{ paddingLeft: 10 + depth * 14 }}
          data-testid={"nv-file:" + entry.path}
          onClick={function () {
            if (entry.is_dir) {
              setOpen(function (prev) {
                var next = Object.assign({}, prev);
                if (next[entry.path]) delete next[entry.path];
                else next[entry.path] = true;
                return next;
              });
            } else {
              openFile(entry, false);
            }
          }}
          onDoubleClick={function () {
            if (!entry.is_dir) openFile(entry, true);
          }}
          onContextMenu={function (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            setMenu({ entry: entry, x: ev.clientX, y: ev.clientY });
          }}>
          {entry.is_dir ? (
            <React.Fragment>
              <svg width="9" height="9" viewBox="0 0 10 10" fill="none"
                stroke="var(--text-3)" strokeWidth="1.5"
                style={{
                  transform: expanded ? "rotate(90deg)" : "none",
                  flexShrink: 0,
                }}>
                <path d="M3.5 2 6.5 5 3.5 8" />
              </svg>
              <svg width="13" height="13" viewBox="0 0 14 14" fill="none"
                stroke="var(--text-3)" strokeWidth="1.2"
                style={{ flexShrink: 0 }}>
                <path d="M1.5 3.5h4l1.5 2h5.5v6h-11Z" />
              </svg>
            </React.Fragment>
          ) : (
            <svg width="12" height="12" viewBox="0 0 14 14" fill="none"
              stroke="var(--text-4)" strokeWidth="1.2"
              style={{ flexShrink: 0, marginLeft: 16 }}>
              <path d="M3 1.5h5.5L11 4v8.5H3Z M8.5 1.5V4H11" />
            </svg>
          )}
          <span className="nv-file-name">
            {entry.path.split("/").pop()}
          </span>
        </button>
        {entry.is_dir && expanded ? (
          <NV_FilesSubtree path={entry.path} depth={depth + 1} row={row} />
        ) : null}
      </React.Fragment>
    );
  }

  var items = (tree.data && tree.data.items) || [];
  var commitRows = (commits.data && commits.data.items) || [];
  var ws = (con.workspaces || []).find(function (w) { return w.id === con.wid; });

  return (
    <div className="nv-files-panel" data-testid="nv-files-panel">
      <div className="nv-rail-section-head">
        <span>Files - {(ws && (ws.name || ws.id)) || con.wid}</span>
        <div style={{ flex: 1 }} />
        <button type="button" className="nv-rail-iconbtn" title="New file"
          data-testid="nv-file-new"
          onClick={function () {
            promptDialog({ title: "New file path" }).then(function (p) {
              if (!p) return;
              SH_api.fileWrite(con.wid, p, "").then(refetch);
            });
          }}>+</button>
        <button type="button" className="nv-rail-iconbtn" title="Upload"
          data-testid="nv-file-upload"
          onClick={function () {
            var input = document.createElement("input");
            input.type = "file";
            input.multiple = true;
            input.onchange = function () { upload(input.files, ""); };
            input.click();
          }}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none"
            stroke="currentColor" strokeWidth="1.4">
            <path d="M6 8V1.5M3 4 6 1l3 3M1.5 10.5h9" />
          </svg>
        </button>
        <button type="button" className="nv-rail-iconbtn"
          title="Workspace history" data-testid="nv-file-history"
          data-active={history ? "true" : "false"}
          onClick={function () { setHistory(function (v) { return !v; }); }}>
          <svg width="13" height="13" viewBox="0 0 13 13" fill="none"
            stroke="currentColor" strokeWidth="1.3">
            <circle cx="6.5" cy="6.5" r="5" />
            <path d="M6.5 3.8v2.9l2 1.4" />
          </svg>
        </button>
      </div>
      <div className="nv-rail-body" data-testid="nv-files"
        onDragOver={function (ev) { ev.preventDefault(); }}
        onDrop={function (ev) {
          ev.preventDefault();
          if (ev.dataTransfer && ev.dataTransfer.files.length) {
            upload(ev.dataTransfer.files, "");
          }
        }}>
        {history ? (
          <React.Fragment>
            <div className="nv-band-head">
              <span>History — turn commits</span>
            </div>
            {commitRows.map(function (c) {
              return (
                <button type="button" key={c.sha} className="nv-commit-row"
                  data-testid={"nv-commit:" + c.sha}
                  onClick={function () {
                    con.setDoc({ kind: "diff", ref: c.sha });
                  }}>
                  <div className="nv-commit-line">
                    <span className="nv-commit-sha">
                      {String(c.sha).slice(0, 7)}
                    </span>
                    <span className="nv-commit-subject">{c.subject}</span>
                  </div>
                  <div className="nv-commit-meta">
                    {(c.session_id || "") + (c.op ? " · " + c.op : "")}
                  </div>
                </button>
              );
            })}
            {!commitRows.length ? (
              <div className="nv-rail-empty">
                <div>No turn commits yet.</div>
              </div>
            ) : null}
          </React.Fragment>
        ) : (
          <React.Fragment>
            {!items.length ? (
              <div className="nv-rail-empty">
                <div>This workspace has no files yet.</div>
                <button type="button" className="nv-btn-secondary"
                  data-testid="nv-files-empty-new"
                  onClick={function () {
                    promptDialog({ title: "New file path" })
                      .then(function (p) {
                        if (!p) return;
                        SH_api.fileWrite(con.wid, p, "").then(refetch);
                      });
                  }}>New file</button>
              </div>
            ) : null}
            {items.map(function (entry) { return row(entry, 0); })}
          </React.Fragment>
        )}
        {menu ? (
          <NV_FileContextMenu entry={menu.entry} x={menu.x} y={menu.y}
            onOpen={openFile} onChanged={refetch}
            onClose={function () { setMenu(null); }} />
        ) : null}
      </div>
    </div>
  );
}

window.NV_FilesSidebar = NV_FilesSidebar;
