/* global React, Icon, Btn, ST2_api, ST2_bucketOf, DiffView */
// Studio revamp - the Changes view (ui/studio/STUDIO-WIRING.md §7).
//
// The promoted .state trail: what the agents actually did to the workspace,
// grouped by the session that did it, with the diff beside it.
//
// Data, and how it differs from what the spec assumed:
//
//   trail  = GET .../log?limit=&with_files=1  (the endpoint added for this)
//   diff   = GET .../commit/{sha}             (already existed)
//
// The spec proposed a new /turns pair plus a client-side fallback that read
// current bytes from /files/read and diffed them against the previous commit.
// That fallback cannot work: /files/read serves the WORKING TREE, so there is
// no way to obtain "the file as it was two commits ago", and diffing the
// current file against anything else would show the wrong change. The commit
// endpoint already returns git's own unified patch per file, which is both
// correct and one request - so this parses that (ST2_parseUnifiedDiff) instead.
//
// Manual refresh, no polling: history does not change under the reader, and
// WS_LogTab's deliberate unpolled behaviour is kept (§7.1).

var ST2_CHANGES_LIMIT = 100;
var ST2_REVIEWED_KEY = "studio:reviewed:";

// Op tone from the commit's X-Primer-Op trailer. A write and a delete are not
// the same event and should not read the same.
function ST2_opTone(op) {
  var o = String(op || "");
  if (o === "tool_result" || o === "tool_call") return "--blue";
  if (o === "rename") return "--amber";
  if (o === "status_change") return "--text-3";
  return "--text-3";
}

// A file row's tone comes from what happened to the FILE, which the counts
// already tell us - added-only is a create, removed-only a delete.
function ST2_fileTone(file) {
  var f = file || {};
  if (f.binary) return "--text-3";
  if (f.additions && !f.deletions) return "--green";
  if (f.deletions && !f.additions) return "--red";
  return "--blue";
}

// Reviewed is client-side until there is somewhere server-side to put it, and
// the chip says "Unreviewed" so the local-only semantics stay honest (§7.4).
function ST2_loadReviewed(wid) {
  try {
    var raw = window.localStorage.getItem(ST2_REVIEWED_KEY + wid);
    var parsed = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (_e) {
    return {};
  }
}

function ST2_saveReviewed(wid, map) {
  try {
    window.localStorage.setItem(ST2_REVIEWED_KEY + wid, JSON.stringify(map));
  } catch (_e) {
    /* quota / disabled storage - non-fatal */
  }
}

// ST2_groupTrail(commits, opts) -> [{ session_id, label, commits, files }]
// Grouped by session because "who did this" is the question a changed file
// raises. Commits with no session trailer (platform writes) collect under one
// group rather than being dropped.
function ST2_groupTrail(commits, opts) {
  var o = opts || {};
  var order = [];
  var bySession = {};
  (commits || []).forEach(function (c) {
    var key = c.session_id || "__workspace__";
    if (!bySession[key]) {
      bySession[key] = { session_id: c.session_id || null, commits: [], fileCount: 0 };
      order.push(key);
    }
    var g = bySession[key];
    g.commits.push(c);
    // files is null when the backend cannot supply it (the sandbox backend
    // never can). Null is not zero, so it must not be counted as zero.
    if (c.files) g.fileCount += c.files.length;
  });
  return order.map(function (key) {
    var g = bySession[key];
    var session = (o.sessionsById || {})[key] || null;
    return {
      session_id: g.session_id,
      label: (session && (session.name || session.session_id)) || g.session_id || "workspace",
      bucket: session ? ST2_bucketOf(session, o).bucket : null,
      commits: g.commits,
      fileCount: g.fileCount,
      unknownFiles: g.commits.some(function (c) { return !c.files; }),
    };
  });
}

// ---------------------------------------------------------------------------

function ST2_TrailFileRow({ commit, file, active, reviewed, onOpen }) {
  var tone = ST2_fileTone(file);
  return (
    <div
      data-testid="changes-file-row"
      data-path={file.path}
      onClick={function () { onOpen(commit, file); }}
      className="row"
      style={{
        gap: 8, alignItems: "center", padding: "5px 10px 5px 22px", cursor: "pointer",
        background: active ? "var(--bg-active)" : "transparent",
        borderLeft: "2px solid " + (active ? "var(--accent)" : "transparent"),
        opacity: reviewed ? 0.55 : 1,
      }}
    >
      <span className="mono" style={{
        flex: 1, minWidth: 0, fontSize: "var(--fs-11)", color: "var(" + tone + ")",
        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", direction: "rtl",
        textAlign: "left",
      }}>{file.path}</span>
      {file.binary ? (
        <span className="muted" style={{ fontSize: "var(--fs-11)", flex: "0 0 auto" }}>bin</span>
      ) : (
        <span style={{ display: "flex", gap: 5, flex: "0 0 auto", fontSize: "var(--fs-11)" }}>
          <span style={{ color: "var(--green)" }}>+{file.additions}</span>
          <span style={{ color: "var(--red)" }}>-{file.deletions}</span>
        </span>
      )}
      {reviewed ? <Icon name="check" size={11} /> : null}
    </div>
  );
}

function ST2_ChangesTrail({ groups, live, activeKey, reviewed, onOpen }) {
  if (!groups.length) {
    return (
      <div data-testid="changes-empty" className="muted" style={{ padding: 16, fontSize: "var(--fs-12)" }}>
        No changes yet. Anything an agent writes shows up here.
      </div>
    );
  }
  return (
    <div data-testid="changes-trail" className="col" style={{ gap: 0, overflow: "auto", minHeight: 0 }}>
      {groups.map(function (g) {
        var isLive = !!(g.session_id && live[g.session_id]);
        return (
          <div key={g.session_id || "__workspace__"} data-testid="changes-session-group">
            <div
              className="row"
              style={{
                gap: 7, alignItems: "center", padding: "6px 10px", position: "sticky", top: 0,
                background: "var(--bg-2)", borderBottom: "1px solid var(--bg-active)", zIndex: 1,
              }}
            >
              {/* A live session's file counts are still moving, so show the
                  pulsing dot instead of a number that is about to be wrong. */}
              {isLive ? (
                <span
                  data-testid="changes-live-dot"
                  style={{
                    width: 7, height: 7, borderRadius: "50%", background: "var(--blue)",
                    animation: "pulse 1.8s ease-in-out infinite", flex: "0 0 auto",
                  }}
                />
              ) : null}
              <span style={{ fontSize: "var(--fs-12)", fontWeight: 600, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {g.label}
              </span>
              <span className="muted" style={{ marginLeft: "auto", fontSize: "var(--fs-11)", flex: "0 0 auto" }}>
                {isLive
                  ? "working"
                  : g.unknownFiles
                    ? g.commits.length + (g.commits.length === 1 ? " turn" : " turns")
                    : g.fileCount + (g.fileCount === 1 ? " file" : " files")}
              </span>
            </div>
            {g.commits.map(function (c) {
              // No file list for this commit: the backend cannot supply one.
              // Say that rather than rendering an empty turn.
              if (!c.files) {
                return (
                  <div
                    key={c.sha}
                    data-testid="changes-files-unavailable"
                    className="muted"
                    style={{ padding: "5px 10px 5px 22px", fontSize: "var(--fs-11)" }}
                  >
                    {c.subject} - file list not available on this backend
                  </div>
                );
              }
              return c.files.map(function (f) {
                var key = c.sha + ":" + f.path;
                return (
                  <ST2_TrailFileRow
                    key={key}
                    commit={c}
                    file={f}
                    active={activeKey === key}
                    reviewed={!!reviewed[key]}
                    onOpen={onOpen}
                  />
                );
              });
            })}
          </div>
        );
      })}
    </div>
  );
}

function ST2_ChangesDetail({ wid, sel, reviewed, onToggleReviewed, onShowTurn }) {
  var api = window.primerApi;
  // Only the opened commit's patches are fetched, which is the whole reason the
  // trail carries counts rather than content.
  var res = api.useResource(
    ST2_api.keys.commit(wid, sel.commit.sha),
    function (signal) { return ST2_api.commit(wid, sel.commit.sha, signal); },
    { deps: [wid, sel.commit.sha] }
  );

  var entry = null;
  var files = (res.data && res.data.files) || [];
  for (var i = 0; i < files.length; i++) {
    if (files[i].path === sel.file.path) { entry = files[i]; break; }
  }

  var key = sel.commit.sha + ":" + sel.file.path;
  var isReviewed = !!reviewed[key];
  // Revert needs a backend op that does not exist. Hidden behind the flag
  // rather than faked with a file write (§7.4).
  var revertEnabled = (api.useTweaks()[0] || {})["studio.revert"] === true;

  return (
    <div className="col" data-testid="changes-detail" style={{ flex: 1, minHeight: 0, gap: 0 }}>
      <div
        className="row"
        style={{
          gap: 8, alignItems: "center", padding: "7px 11px", flex: "0 0 auto",
          borderBottom: "1px solid var(--border)", background: "var(--bg-elev)", flexWrap: "wrap",
        }}
      >
        <span className="mono" style={{ fontSize: "var(--fs-12)", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
          {sel.file.path}
        </span>
        <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
          {sel.commit.agent_id || sel.commit.session_id || "workspace"}
        </span>
        <span className="muted mono" style={{ fontSize: "var(--fs-11)" }}>{sel.commit.sha.slice(0, 8)}</span>
        <span style={{ marginLeft: "auto", display: "flex", gap: 5, flex: "0 0 auto" }}>
          <Btn size="sm" kind="ghost" data-testid="changes-show-turn"
               onClick={function () { onShowTurn(sel.commit); }}>Why? Show the turn</Btn>
          {revertEnabled ? (
            <Btn size="sm" kind="danger" data-testid="changes-revert">Revert turn</Btn>
          ) : null}
          <Btn size="sm" kind={isReviewed ? "primary" : "ghost"} data-testid="changes-mark-reviewed"
               onClick={function () { onToggleReviewed(key); }}>
            {isReviewed ? "Reviewed" : "Mark reviewed"}
          </Btn>
        </span>
      </div>

      <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
        {res.loading && !res.data ? (
          <div className="muted" style={{ padding: 14, fontSize: "var(--fs-12)" }}>Loading diff...</div>
        ) : entry ? (
          <DiffView patch={entry.patch} path={sel.file.path} />
        ) : (
          <div data-testid="changes-no-diff" className="muted" style={{ padding: 14, fontSize: "var(--fs-12)" }}>
            {res.error
              ? (res.error.detail || res.error.title || "Could not load this commit.")
              : "No diff recorded for this path in that commit."}
          </div>
        )}
      </div>

      {/* The turn's rationale is the assistant text from that turn, i.e. the
          commit subject the runtime already writes. No backend "rationale"
          field is invented for it (§7.2). */}
      {sel.commit.subject ? (
        <div
          data-testid="changes-rationale"
          className="muted"
          style={{
            flex: "0 0 auto", padding: "7px 11px", fontSize: "var(--fs-11)",
            borderTop: "1px solid var(--border)", background: "var(--bg-2)",
            lineHeight: 1.5,
          }}
        >{sel.commit.subject}</div>
      ) : null}
    </div>
  );
}

function ChangesView({ wid, studio }) {
  var api = window.primerApi;

  var trail = api.useResource(
    ST2_api.keys.trail(wid, ST2_CHANGES_LIMIT, true),
    function (signal) { return ST2_api.trail(wid, ST2_CHANGES_LIMIT, true, signal); },
    { deps: [wid] }
  );
  var commits = (trail.data && trail.data.commits) || [];

  var sessionsRes = api.useResource(
    ST2_api.keys.sessions(wid),
    function (signal) { return ST2_api.sessions(wid, signal); },
    { pollMs: 5000, deps: [wid] }
  );
  var sessions = (sessionsRes.data && sessionsRes.data.items) || [];

  var sessionsById = React.useMemo(function () {
    var map = {};
    sessions.forEach(function (s) { map[s.session_id || s.id] = s; });
    return map;
  }, [sessionsRes.data]);

  // Which sessions are still working, so their groups show the dot rather than
  // a count that is about to change.
  var live = React.useMemo(function () {
    var map = {};
    sessions.forEach(function (s) {
      if (ST2_bucketOf(s, {}).bucket === "working") map[s.session_id || s.id] = true;
    });
    return map;
  }, [sessionsRes.data]);

  var revState = React.useState(function () { return ST2_loadReviewed(wid); });
  var reviewed = revState[0];
  var setReviewed = revState[1];
  React.useEffect(function () { setReviewed(ST2_loadReviewed(wid)); }, [wid]);

  var selState = React.useState(null);
  var sel = selState[0];
  var setSel = selState[1];

  var onlyUnreviewed = React.useState(false);
  var unreviewedOnly = onlyUnreviewed[0];
  var setUnreviewedOnly = onlyUnreviewed[1];

  var groups = React.useMemo(function () {
    var built = ST2_groupTrail(commits, { sessionsById: sessionsById });
    if (!unreviewedOnly) return built;
    return built
      .map(function (g) {
        return Object.assign({}, g, {
          commits: g.commits
            .map(function (c) {
              if (!c.files) return c;
              return Object.assign({}, c, {
                files: c.files.filter(function (f) {
                  return !reviewed[c.sha + ":" + f.path];
                }),
              });
            })
            .filter(function (c) { return !c.files || c.files.length; }),
        });
      })
      .filter(function (g) { return g.commits.length; });
  }, [commits, sessionsById, unreviewedOnly, reviewed]);

  function toggleReviewed(key) {
    setReviewed(function (prev) {
      var next = Object.assign({}, prev);
      if (next[key]) delete next[key];
      else next[key] = true;
      ST2_saveReviewed(wid, next);
      return next;
    });
  }

  // "Why? Show the turn" opens the session transcript in the OTHER pane, so the
  // diff you are reading stays on screen next to its explanation (§7.2).
  function showTurn(commit) {
    if (!commit.session_id) return;
    var tab = {
      id: "session:" + commit.session_id,
      kind: "session",
      ref: commit.session_id,
      title: commit.session_id,
    };
    if (typeof studio.openAside === "function") studio.openAside(tab);
    else studio.openTab(tab);
  }

  return (
    <div className="row" data-testid="changes-view" style={{ flex: 1, minHeight: 0, gap: 0, alignItems: "stretch" }}>
      <div
        className="col"
        style={{
          width: 320, flex: "0 0 auto", minHeight: 0, gap: 0,
          borderRight: "1px solid var(--border)", background: "var(--bg-2)",
        }}
      >
        <div className="row" style={{ gap: 6, alignItems: "center", padding: "7px 10px", flex: "0 0 auto" }}>
          <span className="muted" style={{ fontSize: "var(--fs-11)", fontWeight: 600, letterSpacing: "0.04em" }}>
            CHANGES
          </span>
          <button
            data-testid="changes-unreviewed-chip"
            aria-pressed={unreviewedOnly}
            onClick={function () { setUnreviewedOnly(function (v) { return !v; }); }}
            style={{
              marginLeft: "auto", padding: "3px 9px", borderRadius: 999, cursor: "pointer",
              border: "1px solid " + (unreviewedOnly ? "var(--border-strong)" : "var(--border)"),
              background: unreviewedOnly ? "var(--bg-active)" : "transparent",
              color: unreviewedOnly ? "var(--text)" : "var(--text-3)",
              fontSize: "var(--fs-11)",
            }}
          >Unreviewed</button>
          <Btn size="sm" kind="ghost" icon="refresh" data-testid="changes-refresh"
               onClick={trail.refetch}>Refresh</Btn>
        </div>
        <ST2_ChangesTrail
          groups={groups}
          live={live}
          activeKey={sel ? sel.commit.sha + ":" + sel.file.path : null}
          reviewed={reviewed}
          onOpen={function (commit, file) { setSel({ commit: commit, file: file }); }}
        />
      </div>

      <div style={{ flex: 1, minWidth: 0, display: "flex" }}>
        {sel ? (
          <ST2_ChangesDetail
            wid={wid}
            sel={sel}
            reviewed={reviewed}
            onToggleReviewed={toggleReviewed}
            onShowTurn={showTurn}
          />
        ) : (
          <div data-testid="changes-no-selection" className="muted" style={{ padding: 20, fontSize: "var(--fs-12)" }}>
            Pick a changed file to see what happened to it.
          </div>
        )}
      </div>
    </div>
  );
}

window.ChangesView = ChangesView;
window.ST2_groupTrail = ST2_groupTrail;
window.ST2_opTone = ST2_opTone;
window.ST2_fileTone = ST2_fileTone;
window.ST2_loadReviewed = ST2_loadReviewed;
window.ST2_saveReviewed = ST2_saveReviewed;
window.ST2_CHANGES_LIMIT = ST2_CHANGES_LIMIT;
