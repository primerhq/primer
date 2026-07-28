/* global React, Icon, Btn, Banner */
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

var PY_STARTER = [
  "@primer_tool()",
  "def greet(name: str) -> str:",
  '    """Greet a person by name.',
  "",
  "    Use when you need a friendly greeting.",
  "",
  "    Args:",
  "        name: Who to greet.",
  '    """',
  '    return "hello " + name',
  "",
].join("\n");

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
            data-testid="python-tool-row"
            className="col"
            style={{ gap: 3, padding: "9px 11px", borderBottom: "1px solid var(--bg-active)" }}
          >
            <div className="row" style={{ gap: 7, alignItems: "center" }}>
              <span className="mono" style={{ fontSize: "var(--fs-12)", fontWeight: 600 }}>{t.id}</span>
              {t.yields ? (
                <span
                  data-testid="python-tool-yields"
                  style={{
                    padding: "0 6px", borderRadius: 999, fontSize: "var(--fs-11)",
                    background: "var(--amber-dim)", color: "var(--amber)",
                  }}
                >yields</span>
              ) : null}
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

  var save = api.useMutation(
    function (next) {
      return api.apiFetch("PUT", "/toolsets/" + encodeURIComponent(toolsetId), {
        id: toolsetId,
        provider: "python",
        config: Object.assign({}, detail.data.config, { source: next }),
      });
    },
    {
      invalidates: ["toolset:" + toolsetId, "toolset-runtime:" + toolsetId],
      onSuccess: function () {
        setRegError(null);
        setDraft(null);
        if (pushToast) pushToast({ kind: "success", title: "Toolset saved", detail: toolsetId });
      },
      onError: function (err) {
        // Registration errors carry the offending field and line, so they
        // belong next to the editor rather than in a toast that slides away
        // while the operator is still looking for the line.
        var ext = (err && err.extensions) || {};
        setRegError({
          message: (err && (err.detail || err.message)) || "could not save",
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
          <Btn
            size="sm"
            kind="ghost"
            data-testid="python-insert-starter"
            onClick={function () { setDraft(source + (source ? "\n\n" : "") + PY_STARTER); }}
          >Insert example</Btn>
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

        <textarea
          data-testid="python-source"
          className="input mono"
          value={source}
          spellCheck={false}
          onChange={function (e) { setDraft(e.target.value); }}
          style={{
            width: "100%", minHeight: 420, resize: "vertical",
            fontSize: "var(--fs-12)", lineHeight: 1.6, whiteSpace: "pre",
          }}
        />
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

        <div className="col" style={{ gap: 0, border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
          <div
            className="row"
            style={{
              padding: "7px 11px", gap: 7, alignItems: "center",
              background: "var(--bg-elev)", borderBottom: "1px solid var(--border)",
            }}
          >
            <Icon name="tools" size={13} />
            <span style={{ fontSize: "var(--fs-12)", fontWeight: 600 }}>Derived tools</span>
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
