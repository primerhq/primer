/* global React, Icon, Btn, Banner, CapabilityBadges */
// Python toolset authoring surface.
//
// The editor is the whole feature for an operator: source in, derived tools
// out. Registration happens server-side on save, so this shows what the server
// actually derived rather than what the source appears to say.
//
// It also shows the isolation level this deployment ENFORCES. "rlimit-only"
// bounds CPU and memory but stops neither filesystem reads nor egress, and an
// operator deciding whether to trust a tool needs to know which of the four
// levels they are on - a generic "sandboxed" badge would be a lie on one of
// them.

var PY_ISOLATION_COPY = {
  container: {
    tone: "--green",
    label: "container",
    detail: "Kernel-enforced: namespaces and cgroups.",
  },
  seccomp: {
    tone: "--green",
    label: "seccomp",
    detail: "Syscall filter: no exec, no ptrace, no network unless allowed.",
  },
  "sandbox-exec": {
    tone: "--blue",
    label: "sandbox-exec",
    detail: "macOS sandbox profile plus resource limits.",
  },
  "rlimit-only": {
    tone: "--amber",
    label: "rlimit-only",
    detail:
      "CPU and memory are bounded. Filesystem reads and outbound network are NOT.",
  },
};

// The templates live in python-code-editor.jsx as window.PY_SCAFFOLDS -- one
// per tool shape, each carrying the contract as # comments. This file only
// decides where they get inserted.

// Turn a registration error's 1-based line into a CodeMirror range, so the
// failure is marked on the line that caused it rather than only described in
// a panel above the editor. Handles BOTH shapes the dry-run route can carry:
// a single module-level `error` (bad syntax, a dangling @resumes reference -
// nothing registers at all), and per-tool refusals inside `tools` (one bad
// function does not stop the rest from validating - batch-2's
// register_module_report). Every refused tool gets its own gutter mark, not
// just the first.
function PY_diagnosticsFor(source, error, tools) {
  var lines = (source || "").split("\n");
  function rangeFor(lineno) {
    var idx = Math.max(0, Math.min(lineno - 1, lines.length - 1));
    var from = 0;
    for (var i = 0; i < idx; i++) from += lines[i].length + 1;
    return { from: from, to: from + lines[idx].length };
  }
  var out = [];
  if (error && error.lineno) {
    var r = rangeFor(error.lineno);
    out.push({
      from: r.from,
      to: r.to,
      severity: "error",
      message: error.field ? error.message + "  (" + error.field + ")" : error.message,
    });
  }
  (tools || []).forEach(function (t) {
    if (t.ok === false && t.lineno) {
      var tr = rangeFor(t.lineno);
      var msg = (t.error && t.error.message) || "registration refused";
      var field = t.error && t.error.field;
      out.push({
        from: tr.from,
        to: tr.to,
        severity: "error",
        message: field ? msg + "  (" + field + ")" : msg,
      });
    }
  });
  return out;
}

function PY_IsolationBadge({ level }) {
  var copy = PY_ISOLATION_COPY[level];
  if (!copy) return null;
  return (
    <div
      data-testid="python-isolation-level"
      data-level={level}
      className="col"
      style={{
        gap: 3,
        padding: "8px 11px",
        borderRadius: 8,
        border: "1px solid var(" + copy.tone + ")",
        background: "var(" + copy.tone + "-dim)",
      }}
    >
      <span style={{ fontSize: "var(--fs-12)", fontWeight: 600, color: "var(" + copy.tone + ")" }}>
        Isolation: {copy.label}
      </span>
      <span className="muted" style={{ fontSize: "var(--fs-11)", lineHeight: 1.5 }}>
        {copy.detail}
      </span>
    </div>
  );
}

function PY_DerivedTools({ tools }) {
  if (!tools || !tools.length) {
    return (
      <div data-testid="python-derived-empty" className="muted" style={{ padding: 12, fontSize: "var(--fs-12)" }}>
        No tools yet. Add a function with the <span className="mono">@primer_tool</span> decorator and save.
      </div>
    );
  }
  return (
    <div data-testid="python-derived-tools" className="col" style={{ gap: 0 }}>
      {tools.map(function (t) {
        var args = Object.keys((t.args_schema && t.args_schema.properties) || {});
        return (
          <div
            key={t.id}
            data-testid={"python-tool-row:" + t.id}
            className="col"
            style={{ gap: 3, padding: "9px 11px", borderBottom: "1px solid var(--bg-active)" }}
          >
            <div className="row" style={{ gap: 7, alignItems: "center" }}>
              <span className="mono" style={{ fontSize: "var(--fs-12)", fontWeight: 600 }}>{t.id}</span>
              <CapabilityBadges tool={t} testid={"python-tool-badges-" + t.id} />
              <span className="muted mono" style={{ marginLeft: "auto", fontSize: "var(--fs-11)" }}>
                ({args.join(", ")})
              </span>
            </div>
            <span className="muted" style={{ fontSize: "var(--fs-11)", lineHeight: 1.5 }}>
              {(t.description || "").split("\n")[0]}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// The outline. Its job is to turn "one long text document" into something
// with structure you can navigate: every registered function, what shape it
// is, and a click that puts the cursor on its `def`.
//
// It is fed by the live validate route rather than the saved runtime, so a
// function you just typed appears before you save it.
function PY_Outline({ tools, error, onJump }) {
  if (error) {
    return (
      <div
        data-testid="python-outline-error"
        className="muted"
        style={{ padding: "10px 12px", fontSize: "var(--fs-11)", lineHeight: 1.6 }}
      >
        No functions registered while the source has an error
        {error.lineno ? " (line " + error.lineno + ")" : ""}.
      </div>
    );
  }
  if (!tools || !tools.length) {
    return (
      <div
        data-testid="python-outline-empty"
        className="muted"
        style={{ padding: "10px 12px", fontSize: "var(--fs-11)", lineHeight: 1.6 }}
      >
        No functions yet. Use <span className="mono">Add function</span> to
        insert one with the contract spelled out in comments.
      </div>
    );
  }
  return (
    <div data-testid="python-outline" className="col" style={{ gap: 0 }}>
      {tools.map(function (t) {
        // A per-tool refusal (batch-2 register_module_report): the rest of
        // the module still validated, so this function shows up alongside
        // the good ones with its own reason, instead of the whole outline
        // going empty the way one bad function used to make it.
        var refused = t.ok === false;
        return (
          <div
            key={t.id || t.fn_name}
            data-testid={"python-outline-row:" + (t.id || t.fn_name)}
            data-ok={refused ? "0" : "1"}
            className="row"
            onClick={function () { if (onJump) onJump(t.lineno); }}
            style={{
              gap: 7, alignItems: "flex-start", cursor: "pointer",
              padding: "6px 11px", borderBottom: "1px solid var(--border)",
            }}
          >
            <Icon name={refused ? "x-circle" : (t.yields ? "clock" : "tools")} size={12}
              style={{ marginTop: 2, color: refused ? "var(--red)" : undefined }} />
            <div className="col" style={{ gap: 2, minWidth: 0, flex: 1 }}>
              <div className="row" style={{ gap: 7, alignItems: "center" }}>
                <span className="mono" style={{ fontSize: "var(--fs-12)", color: refused ? "var(--red)" : undefined }}>
                  {t.id || t.fn_name}
                </span>
                {!refused ? (
                  <CapabilityBadges tool={t} testid={"python-outline-badges-" + (t.id || t.fn_name)} />
                ) : null}
                {!refused ? (
                  <span className="muted mono" style={{ marginLeft: "auto", fontSize: "var(--fs-11)" }}>
                    ({(t.args || []).join(", ")})
                  </span>
                ) : null}
              </div>
              {refused ? (
                <span
                  data-testid="python-outline-refused-reason"
                  className="mono"
                  style={{ fontSize: "var(--fs-11)", color: "var(--red)", lineHeight: 1.5 }}
                >
                  {(t.error && t.error.message) || "registration refused"}
                </span>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// "Add function" -- a small menu rather than one button, because the two tool
// shapes differ enough that a single template would teach the wrong thing
// about the one you did not pick.
function PY_AddFunction({ onInsert }) {
  var openState = React.useState(false);
  var open = openState[0];
  var setOpen = openState[1];
  var scaffolds = window.PY_SCAFFOLDS || [];

  return (
    <div style={{ position: "relative" }}>
      <Btn
        size="sm"
        kind="ghost"
        icon="plus"
        data-testid="python-add-function"
        onClick={function () { setOpen(function (o) { return !o; }); }}
      >Add function</Btn>
      {open ? (
        <div
          data-testid="python-add-function-menu"
          className="col"
          style={{
            position: "absolute", top: 30, left: 0, zIndex: 40, width: 290,
            gap: 0, background: "var(--bg-elev)", padding: 5,
            border: "1px solid var(--border-strong)", borderRadius: 9,
            boxShadow: "var(--shadow)",
          }}
        >
          {scaffolds.map(function (s) {
            return (
              <div
                key={s.id}
                data-testid={"python-scaffold-" + s.id}
                className="col"
                onClick={function () { setOpen(false); if (onInsert) onInsert(s.source); }}
                style={{ gap: 2, padding: "7px 9px", borderRadius: 6, cursor: "pointer" }}
                onMouseEnter={function (e) { e.currentTarget.style.background = "var(--bg-hover)"; }}
                onMouseLeave={function (e) { e.currentTarget.style.background = "transparent"; }}
              >
                <span style={{ fontSize: "var(--fs-12)", fontWeight: 600 }}>{s.label}</span>
                <span className="muted" style={{ fontSize: "var(--fs-11)", lineHeight: 1.45 }}>
                  {s.hint}
                </span>
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function PythonToolsetEditor({ toolsetId, pushToast }) {
  var api = window.primerApi;

  var detail = api.useResource(
    "toolset:" + toolsetId,
    function (signal) {
      return api.apiFetch("GET", "/toolsets/" + encodeURIComponent(toolsetId), null, { signal });
    },
    { deps: [toolsetId] }
  );

  // Runtime facts come from a separate route because they are not properties
  // of the record: the isolation level is a property of THIS deployment.
  var runtime = api.useResource(
    "toolset-runtime:" + toolsetId,
    function (signal) {
      return api.apiFetch(
        "GET", "/toolsets/" + encodeURIComponent(toolsetId) + "/runtime", null, { signal }
      );
    },
    { deps: [toolsetId] }
  );

  var srcState = React.useState(null);
  var draft = srcState[0];
  var setDraft = srcState[1];
  var errState = React.useState(null);
  var regError = errState[0];
  var setRegError = errState[1];

  var stored = (detail.data && detail.data.config && detail.data.config.source) || "";
  var source = draft == null ? stored : draft;
  var dirty = draft != null && draft !== stored;

  // Handle on the CodeMirror view, so the outline can jump and "Add function"
  // can insert at the end without routing text through React state.
  var viewRef = React.useRef(null);

  // ---- live validation -------------------------------------------------
  // The registrar's verdict while the operator is still typing. Debounced
  // because it is a round trip per keystroke otherwise, and 450ms is below
  // the point where feedback stops feeling attached to the edit.
  //
  // A failed request deliberately keeps the previous verdict rather than
  // clearing it: a dropped packet should not make a real error disappear.
  var vState = React.useState(null);
  var validation = vState[0];
  var setValidation = vState[1];

  React.useEffect(function () {
    if (detail.loading && !detail.data) return undefined;
    var cancelled = false;
    var timer = setTimeout(function () {
      api.apiFetch(
        "POST",
        "/toolsets/" + encodeURIComponent(toolsetId) + "/validate",
        { source: source }
      ).then(function (res) {
        if (!cancelled) setValidation(res);
      }).catch(function () { /* keep the last verdict */ });
    }, 450);
    return function () { cancelled = true; clearTimeout(timer); };
  }, [source, toolsetId, detail.loading]);

  var liveError = validation ? validation.error : null;
  var liveTools = validation ? validation.tools : null;
  var diagnostics = React.useMemo(
    function () { return PY_diagnosticsFor(source, liveError, liveTools); },
    [source, liveError, liveTools]
  );

  var save = api.useMutation(
    function (next) {
      // Send only the fields this form owns, not the config echoed back from
      // the read path. A read response is shaped for reading - secrets are
      // masked, and anything the server adds has to survive a round trip it
      // was never designed for. Spreading it made every save fail request
      // validation before it reached the registration check.
      var prior = (detail.data && detail.data.config) || {};
      var cfg = { source: next, source_version: prior.source_version || 1 };
      if (prior.default_timeout_seconds) {
        cfg.default_timeout_seconds = prior.default_timeout_seconds;
      }
      if (prior.allow_network) cfg.allow_network = true;
      if (prior.image) cfg.image = prior.image;
      return api.apiFetch("PUT", "/toolsets/" + encodeURIComponent(toolsetId), {
        id: toolsetId,
        provider: "python",
        config: cfg,
      });
    },
    {
      invalidates: [
        "toolset:" + toolsetId,
        "toolset-detail:" + toolsetId,
        "toolset-runtime:" + toolsetId,
      ],
      onSuccess: function () {
        setRegError(null);
        setDraft(null);
        if (pushToast) pushToast({ kind: "success", title: "Toolset saved", detail: toolsetId });
      },
      onError: function (err) {
        // Registration errors carry the offending field and line, so they
        // belong next to the editor rather than in a toast that slides away
        // while the operator is still looking for the line.
        // Read the RAW envelope, not ApiError's fields.
        //
        // Two layers strip this message. The RFC7807 handler keeps the raised
        // detail dict verbatim under `extensions` and puts a generic status
        // title in `detail`. Then ApiError REWRITES title/detail for every 422
        // into "Data is incomplete" / "Some required fields are missing or
        // invalid" - friendly for form validation, fatal here, because a
        // registration error carries the function name and line that are the
        // entire reason this is shown inline. ApiError also exposes no
        // `.extensions`; the envelope hangs off `.envelope`.
        var ext = ((err && err.envelope && err.envelope.extensions) || {});
        setRegError({
          message:
            ext.message || (err && (err.detail || err.message)) || "could not save",
          field: ext.field || null,
          lineno: ext.lineno || null,
        });
      },
    }
  );

  if (detail.loading && !detail.data) {
    return <div className="muted" style={{ padding: 16 }}>Loading toolset...</div>;
  }
  if (detail.error) {
    return (
      <Banner
        kind="error"
        title="Couldn't load this toolset"
        detail={detail.error.detail || detail.error.message}
      />
    );
  }

  var rt = runtime.data || {};
  var storedError = rt.registration_error;

  return (
    <div className="row" data-testid="python-editor" style={{ gap: 14, alignItems: "stretch" }}>
      <div className="col" style={{ flex: "1 1 60%", gap: 8, minWidth: 0 }}>
        <div className="row" style={{ gap: 8, alignItems: "center" }}>
          <span className="muted" style={{ fontSize: "var(--fs-11)", fontWeight: 600, letterSpacing: "0.04em" }}>
            SOURCE
          </span>
          <PY_AddFunction
            onInsert={function (text) {
              // Straight into the editor when it is mounted, so the cursor
              // lands in the new function. React state is the fallback path
              // for the textarea build.
              if (viewRef.current && window.PY_appendSource) {
                window.PY_appendSource(viewRef.current, text);
              } else {
                setDraft(source + text);
              }
            }}
          />
          {validation ? (function () {
            // Partial success is real now (batch-2 register_module_report):
            // a module with 3 good functions and 1 refused one still
            // registers 3 tools, so the pill has to say that instead of
            // collapsing to "does not register" the way a single bad
            // function used to make the whole module look dead.
            var toolList = validation.tools || [];
            var refusedCount = toolList.filter(function (t) { return t.ok === false; }).length;
            var okCount = toolList.length - refusedCount;
            var label = liveError
              ? "does not register"
              : refusedCount > 0
                ? okCount + " registered, " + refusedCount + " refused"
                : okCount + " registered";
            return (
              <span
                data-testid="python-live-status"
                data-ok={validation.ok ? "1" : "0"}
                className="mono"
                style={{
                  fontSize: "var(--fs-11)",
                  color: validation.ok ? "var(--accent)" : "var(--red)",
                }}
              >
                {label}
              </span>
            );
          })() : null}
          <Btn
            size="sm"
            kind="primary"
            data-testid="python-save"
            disabled={!dirty || save.loading}
            onClick={function () { save.mutate(source); }}
            style={{ marginLeft: "auto" }}
          >{save.loading ? "Saving..." : "Save"}</Btn>
        </div>

        {regError ? (
          <div
            data-testid="python-registration-error"
            className="col"
            style={{
              gap: 3, padding: "9px 11px", borderRadius: 8,
              border: "1px solid var(--red)", background: "var(--red-dim)",
            }}
          >
            <span style={{ fontSize: "var(--fs-12)", color: "var(--red)", fontWeight: 600 }}>
              This source did not register
            </span>
            <span className="mono" style={{ fontSize: "var(--fs-11)", lineHeight: 1.5 }}>
              {regError.message}
            </span>
            {regError.lineno ? (
              <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
                line {regError.lineno}
                {regError.field ? " - " + regError.field : ""}
              </span>
            ) : null}
          </div>
        ) : null}

        {typeof window.PY_CodeEditor === "function" ? (
          <window.PY_CodeEditor
            value={source}
            onChange={function (next) { setDraft(next); }}
            diagnostics={diagnostics}
            viewRef={viewRef}
            minHeight={460}
          />
        ) : null}
      </div>

      <div className="col" style={{ flex: "1 1 40%", gap: 10, minWidth: 0 }}>
        <PY_IsolationBadge level={rt.isolation_level} />

        {storedError ? (
          <Banner
            kind="warn"
            title="The saved source does not register"
            detail={
              storedError.message +
              (storedError.lineno ? " (line " + storedError.lineno + ")" : "")
            }
          />
        ) : null}

        {/* Outline reflects the DRAFT (live validate); Saved reflects what is
            actually callable right now. Keeping both visible is the point --
            they differ exactly while there are unsaved edits, and that gap is
            what an operator needs to see before deciding to save. */}
        <div className="col" style={{ gap: 0, border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
          <div
            className="row"
            style={{
              padding: "7px 11px", gap: 7, alignItems: "center",
              background: "var(--bg-elev)", borderBottom: "1px solid var(--border)",
            }}
          >
            <Icon name="code" size={13} />
            <span style={{ fontSize: "var(--fs-12)", fontWeight: 600 }}>Functions</span>
            {dirty ? (
              <span className="muted" style={{ fontSize: "var(--fs-11)" }}>unsaved</span>
            ) : null}
            <span className="muted" style={{ marginLeft: "auto", fontSize: "var(--fs-11)" }}>
              {validation && validation.ok ? (validation.tools || []).length : ""}
            </span>
          </div>
          <PY_Outline
            tools={validation ? validation.tools : null}
            error={liveError}
            onJump={function (lineno) {
              if (viewRef.current && window.PY_revealLine) {
                window.PY_revealLine(viewRef.current, lineno);
              }
            }}
          />
        </div>

        <div className="col" style={{ gap: 0, border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
          <div
            className="row"
            style={{
              padding: "7px 11px", gap: 7, alignItems: "center",
              background: "var(--bg-elev)", borderBottom: "1px solid var(--border)",
            }}
          >
            <Icon name="tools" size={13} />
            <span style={{ fontSize: "var(--fs-12)", fontWeight: 600 }}>Saved &amp; callable</span>
            <span className="muted" style={{ marginLeft: "auto", fontSize: "var(--fs-11)" }}>
              {(rt.tools || []).length}
            </span>
          </div>
          <PY_DerivedTools tools={rt.tools} />
        </div>

        <div className="muted" style={{ fontSize: "var(--fs-11)", lineHeight: 1.6 }}>
          Every <span className="mono">@primer_tool</span> function needs a docstring with a
          summary line, a <span className="mono">Use when</span> line, and an
          <span className="mono"> Args:</span> entry per argument. A
          <span className="mono"> @resumes(fn)</span> companion makes a tool yielding.
        </div>
      </div>
    </div>
  );
}

window.PythonToolsetEditor = PythonToolsetEditor;
window.PY_ISOLATION_COPY = PY_ISOLATION_COPY;
