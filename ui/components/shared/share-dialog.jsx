/* global React, SH_api, Modal, Btn */
// The console Share dialog (ACL-P1, Task 10): owners, read/full grants and the
// public switch for the five owned entity types (agents, graphs, toolsets,
// workspace templates, channels). The server enforces everything (the access
// sub-resource, Tasks 6-7); this dialog only draws the view the contract
// serves and posts back the edit with the rev it loaded:
//
//   GET /v1/{plural}/{id}/access  -> { owners, grants, public, public_tools,
//                                      rev, your_level }
//   PUT  body { owners, grants: {id: "read"|"full"}, public, public_tools, rev }
//
// 409 (rev stale) says "Sharing changed since you opened this; reload" with a
// Reload action; 422 shows the field message inline; a your_level that is not
// "owner" gets the view read-only.
//
// The entity.share verb (nv-shell.jsx) dispatches through the seam below: the
// open owned-entity surface subscribes with SHR_useShareOpen and opens when
// the palette (or any surface) runs the verb.

// ---- the seam: one slot between the verb and the open surface -------------

var SHR_shareSubs = new Set();

// Subscribes while a surface is mounted; returns the unsubscribe.
window.SHR_onShare = function (cb) {
  SHR_shareSubs.add(cb);
  return function () { SHR_shareSubs.delete(cb); };
};

// The verb's run(): deliver to every open surface (only one owned-entity
// surface is open at a time, so no matching is needed); a no-op with no
// subscriber rather than a throw.
window.SHR_requestShare = function () {
  SHR_shareSubs.forEach(function (cb) { cb(); });
};

// Whether any surface is subscribed (the verb's availability backstop: the
// channel form's edit mode has no overlay id, so the gate can read this).
window.SHR_shareLive = function () {
  return SHR_shareSubs.size > 0;
};

// The hook the five detail surfaces use: open flips when the share verb
// dispatches while the surface is mounted; the dialog's Close flips it back.
function SHR_useShareOpen() {
  var st = React.useState(false);
  React.useEffect(function () {
    return window.SHR_onShare(function () { st[1](true); });
  }, []);
  return st;
}
window.SHR_useShareOpen = SHR_useShareOpen;

// ---- badges the list rows and cards draw from the row's access -------------

// "Public" when anyone can use it, "Shared" when a grant exists; private is
// the default and says nothing. The row's access is the Access model: grants
// is the uid -> level map (the /access view's array shape only arrives
// through the dialog), so a grant is counted by the map's keys.
function SHR_accessBadges(access) {
  var out = [];
  if (!access) return out;
  if (access.public) out.push("Public");
  var grants = access.grants;
  if (grants && (Array.isArray(grants) ? grants.length : Object.keys(grants).length)) out.push("Shared");
  return out;
}
window.SHR_accessBadges = SHR_accessBadges;

// ACL-P3-T7: a row the harness installs (a non-null harness_id) answers 409
// harness_managed to a direct share - its sharing is the harness's to change.
// Its detail surface draws this INSTEAD of the Share button: it names the
// harness and opens it, so the operator shares the harness instead.
function SHR_HarnessShare({ harnessId, onOpen }) {
  return (
    <Btn
      size="sm"
      kind="ghost"
      icon="book"
      data-testid="shr-harness-share"
      title="This is shared through its harness; share the harness instead"
      onClick={onOpen}
    >
      Shared through harness <span className="mono">{harnessId}</span>
    </Btn>
  );
}
window.SHR_HarnessShare = SHR_HarnessShare;

// ---- the dialog -------------------------------------------------------------

function SHR_draftOf(a) {
  var d = {
    owners: (a.owners || []).map(function (o) { return o.id; }),
    grants: {},
    public: !!a.public,
    publicTools: (a.public_tools || []).slice(),
  };
  (a.grants || []).forEach(function (g) {
    if (g && g.user) d.grants[g.user.id] = g.level;
  });
  return d;
}

function SHR_userOf(a, uid) {
  var gs = a.grants || [];
  var i;
  for (i = 0; i < gs.length; i++) {
    if (gs[i].user && gs[i].user.id === uid) return gs[i].user;
  }
  var os = a.owners || [];
  for (i = 0; i < os.length; i++) {
    if (os[i].id === uid) return os[i];
  }
  return null;
}

function SHR_ShareDialog({ plural, id, title, allowPublicTools, tools, onClose }) {
  var api = window.primerApi;

  var res = api.useResource(
    "shr-access:" + plural + ":" + id,
    function (signal) { return SH_api.getAccess(plural, id, signal); },
    { pollMs: null, deps: [plural, id] }
  );
  var a = res.data;
  var readOnly = !a || a.your_level !== "owner";

  var draftState = React.useState(null);
  var draft = draftState[0];
  var setDraft = draftState[1];
  var staleState = React.useState(false);
  var stale = staleState[0];
  var setStale = staleState[1];
  var fieldErrState = React.useState(null);
  var fieldErr = fieldErrState[0];
  var setFieldErr = fieldErrState[1];
  var qState = React.useState("");
  var q = qState[0];
  var setQ = qState[1];
  var hitsState = React.useState([]);
  var hits = hitsState[0];
  var setHits = hitsState[1];

  var save = api.useMutation(
    function (body) { return SH_api.setAccess(plural, id, body); },
    {
      onSuccess: function () {
        res.refetch();
        if (onClose) onClose();
      },
      onError: function (err) {
        if (err && err.status === 409) {
          setStale(true);
          return;
        }
        if (err && err.status === 422) {
          var msgs = (Array.isArray(err.fieldErrors) ? err.fieldErrors : [])
            .map(function (fe) { return (fe && fe.msg) || String(fe); });
          if (msgs.length) { setFieldErr(msgs.join(" ")); return; }
        }
        // A refusal the dialog does not own (a 403, a 5xx, the network): the
        // mutation's default toast is suppressed because this dialog supplies
        // its own onError, so say it here, with the same one-reader.
        var refusal = api.readRefusal ? api.readRefusal(err) : null;
        if (api.toastPush) {
          api.toastPush({
            kind: "error",
            title: (err && err.title) || "Sharing could not be saved",
            detail: (refusal && refusal.message) || (err && (err.detail || err.message)) || "",
            requestId: err && err.requestId,
          });
        }
      },
    }
  );

  // A fresh view (the initial load, a refetch, a reload after 409) resets the
  // draft onto it: a save that did not land must not leave the draft claiming
  // an edit the server rejected.
  React.useEffect(function () {
    if (!a) return;
    setDraft(SHR_draftOf(a));
    setStale(false);
    setFieldErr(null);
  }, [a]);

  // The username-prefix lookup (top ten): offered while a prefix is typed,
  // cleared when it is not (or the view is read-only).
  React.useEffect(function () {
    if (!q || q.length < 2 || readOnly) { setHits([]); return; }
    var on = true;
    SH_api.lookupUsers(q).then(
      function (rows) { if (on) setHits(rows || []); },
      function () { if (on) setHits([]); }
    );
    return function () { on = false; };
  }, [q, readOnly]);

  function setDraftOf(fn) {
    setDraft(function (d) { return d && fn(d); });
  }
  function setGrantLevel(uid, level) {
    setDraftOf(function (d) {
      var g = {};
      var k;
      for (k in d.grants) g[k] = d.grants[k];
      g[uid] = level;
      return { owners: d.owners, grants: g, public: d.public, publicTools: d.publicTools };
    });
  }
  function addGrant(uid) {
    setDraftOf(function (d) {
      var g = {};
      var k;
      for (k in d.grants) g[k] = d.grants[k];
      if (!g[uid]) g[uid] = "read"; // least privilege: a new grant is read
      return { owners: d.owners, grants: g, public: d.public, publicTools: d.publicTools };
    });
  }
  function removeGrant(uid) {
    setDraftOf(function (d) {
      var g = {};
      var k;
      for (k in d.grants) if (k !== uid) g[k] = d.grants[k];
      return { owners: d.owners, grants: g, public: d.public, publicTools: d.publicTools };
    });
  }
  function removeOwner(uid) {
    setDraftOf(function (d) {
      return {
        owners: d.owners.filter(function (x) { return x !== uid; }),
        grants: d.grants,
        public: d.public,
        publicTools: d.publicTools,
      };
    });
  }
  function setPublic(v) {
    setDraftOf(function (d) { return { owners: d.owners, grants: d.grants, public: v, publicTools: d.publicTools }; });
  }
  function setPublicTool(name, v) {
    setDraftOf(function (d) {
      var t = d.publicTools.filter(function (x) { return x !== name; });
      if (v) t.push(name);
      return { owners: d.owners, grants: d.grants, public: d.public, publicTools: t };
    });
  }

  function submit() {
    if (readOnly || !draft || save.loading) return;
    setFieldErr(null);
    save.mutate({
      owners: draft.owners,
      grants: draft.grants,
      public: draft.public,
      public_tools: draft.publicTools,
      rev: a.rev,
    });
  }

  return (
    <Modal
      title={"Share " + (title || id)}
      testid="shr-dialog"
      onClose={onClose}
      footer={
        <>
          {!readOnly && (
            <Btn kind="primary" data-testid="shr-save" busy={save.loading} onClick={submit}>
              {save.loading ? "Saving…" : "Save"}
            </Btn>
          )}
          <Btn kind="ghost" data-testid="shr-close" onClick={onClose}>Close</Btn>
        </>
      }
    >
      {res.loading && !a ? (
        <div role="status" data-testid="shr-loading">Loading sharing…</div>
      ) : res.error && !a ? (
        <div role="alert" data-testid="shr-error" style={{ color: "var(--red)" }}>
          <div>{api.readRefusal ? api.readRefusal(res.error).message : (res.error.title || "Couldn't load sharing")}</div>
          <div style={{ marginTop: 8 }}>
            <Btn size="sm" kind="ghost" data-testid="shr-retry" onClick={res.refetch}>Retry</Btn>
          </div>
        </div>
      ) : !a ? null : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {readOnly && (
            <div className="muted text-sm" data-testid="shr-readonly">Only owners can change sharing.</div>
          )}
          {stale && (
            <div role="alert" data-testid="shr-stale" style={{ color: "var(--red)" }}>
              <div>Sharing changed since you opened this; reload</div>
              <div style={{ marginTop: 6 }}>
                <Btn size="sm" kind="ghost" data-testid="shr-stale-reload"
                  onClick={function () { setStale(false); res.refetch(); }}>
                  Reload
                </Btn>
              </div>
            </div>
          )}
          {fieldErr && (
            <div role="alert" data-testid="shr-field-errors" style={{ color: "var(--red)" }}>{fieldErr}</div>
          )}
          <div>
            <div className="muted text-sm">Owners</div>
            <ul style={{ margin: "4px 0 0", paddingLeft: 18 }} data-testid="shr-owners">
              {(a.owners || []).map(function (o) {
                return (
                  <li key={o.id} style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    <span className="mono">{o.username}</span>
                    {!readOnly && (
                      <Btn size="sm" kind="ghost" data-testid={"shr-owner-remove-" + o.id} onClick={function () { removeOwner(o.id); }}>
                        Remove
                      </Btn>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
          <div>
            <div className="muted text-sm">People with access</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }} data-testid="shr-grants">
              {(!draft || Object.keys(draft.grants).length === 0) && (
                <span className="muted text-sm">No one else yet.</span>
              )}
              {draft && Object.keys(draft.grants).map(function (uid) {
                var who = SHR_userOf(a, uid);
                return (
                  <div key={uid} style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    <span className="mono">{who ? who.username : uid}</span>
                    <select
                      className="select"
                      data-testid={"shr-grant-level-" + uid}
                      value={draft.grants[uid]}
                      onChange={function (e) { setGrantLevel(uid, e.target.value); }}
                      disabled={readOnly}
                    >
                      <option value="read">read</option>
                      <option value="full">full</option>
                    </select>
                    {!readOnly && (
                      <Btn size="sm" kind="ghost" data-testid={"shr-grant-remove-" + uid} onClick={function () { removeGrant(uid); }}>
                        Remove
                      </Btn>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
          {!readOnly && (
            <div data-testid="shr-add">
              <input
                className="input mono"
                data-testid="shr-add-user"
                value={q}
                placeholder="Add by username prefix"
                onChange={function (e) { setQ(e.target.value); }}
                style={{ width: "100%" }}
              />
              {hits.length > 0 && (
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 4 }} data-testid="shr-lookup">
                  {hits.map(function (u) {
                    return (
                      <button
                        key={u.id}
                        type="button"
                        className="btn btn-ghost btn-sm"
                        data-testid={"shr-lookup-" + u.id}
                        onClick={function () { addGrant(u.id); setQ(""); }}
                      >
                        {u.username}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          )}
          <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input
              type="checkbox"
              data-testid="shr-public"
              checked={!!(draft && draft.public)}
              onChange={function (e) { setPublic(e.target.checked); }}
              disabled={readOnly}
            />
            <span>Public (any signed-in user can use it)</span>
          </label>
          {allowPublicTools && tools && tools.length > 0 && (
            <div data-testid="shr-public-tools">
              <div className="muted text-sm">Public tools (usable without access to this toolset)</div>
              {tools.map(function (name) {
                return (
                  <label key={name} style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    <input
                      type="checkbox"
                      data-testid={"shr-public-tool-" + name}
                      checked={!!draft && draft.publicTools.indexOf(name) >= 0}
                      onChange={function (e) { setPublicTool(name, e.target.checked); }}
                      disabled={readOnly}
                    />
                    <span className="mono">{name}</span>
                  </label>
                );
              })}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}
window.SHR_ShareDialog = SHR_ShareDialog;
