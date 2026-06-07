/* global React */

// embed:<id> directive. Looks up the id in window.DocsEmbedRegistry and
// renders the real console component inside an iframe fed by fixture data
// from /v1/user_docs/_fixtures/<fixtures>.json.
//
// Unknown id shows an amber warning naming valid ids.
// The iframe boots the full /console/_app.js bundle (suppressing the
// full-app auto-boot by omitting #root), installs window.primerApi as
// DocsMakeStubApi(fixtures), then renders window[entry.component].
//
// Wraps the iframe in a chrome container with a small uppercase label
// "Console preview - sample data".

if (window.MarkdownDirectives) {
  window.MarkdownDirectives.register("embed:", function (_ref) {
    var directive = _ref.directive;
    var embedId = directive.slice("embed:".length).trim();
    var registry = window.DocsEmbedRegistry || {};
    var entry = registry[embedId];

    if (!entry) {
      var validIds = (window.DocsEmbedIds && window.DocsEmbedIds()) || Object.keys(registry);
      return React.createElement(
        "div",
        {
          style: {
            padding: "10px 14px",
            margin: "12px 0",
            borderLeft: "3px solid var(--amber)",
            background: "var(--bg-2)",
            borderRadius: 4,
            color: "var(--text-2)",
            fontSize: 12,
          },
        },
        React.createElement(React.Fragment, null,
          "Unknown embed id: ",
          React.createElement("code", null, embedId),
          ". Valid ids: ",
          React.createElement("code", null, validIds.join(", ")),
          "."
        )
      );
    }

    return React.createElement(DocsEmbedPreview, { embedId: embedId, entry: entry });
  });
}

function DocsEmbedPreview(_ref2) {
  var embedId = _ref2.embedId;
  var entry = _ref2.entry;
  var iframeRef = React.useRef(null);
  var _state = React.useState("booting");
  var status = _state[0];
  var setStatus = _state[1];

  React.useEffect(function () {
    var iframe = iframeRef.current;
    if (!iframe) return;
    if (typeof window.DocsBootEmbedIframe !== "function") {
      setStatus("error: DocsBootEmbedIframe not available");
      return;
    }
    var fixturesPath = "/v1/user_docs/_fixtures/" + entry.fixtures + ".json";
    window.DocsBootEmbedIframe(
      iframe,
      entry.component,
      fixturesPath,
      entry.props || {},
      function (err) {
        if (err) {
          setStatus("error: " + (err.message || String(err)));
        } else {
          setStatus("done");
        }
      }
    );
    // Run once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  var isError = status.startsWith("error");

  return React.createElement(
    "div",
    {
      style: {
        margin: "16px 0",
        border: "1px solid var(--border)",
        borderRadius: 6,
        overflow: "hidden",
      },
      "data-embed-id": embedId,
      "data-embed-status": status,
    },
    // Chrome label bar
    React.createElement(
      "div",
      {
        style: {
          padding: "5px 10px",
          background: "var(--bg-2)",
          borderBottom: "1px solid var(--border)",
          fontSize: 10,
          color: "var(--text-3)",
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        },
      },
      React.createElement("span", null, "Console preview - sample data"),
      React.createElement(
        "span",
        { style: { color: isError ? "var(--red)" : "var(--text-3)" } },
        status !== "done" ? status : null
      )
    ),
    // Iframe
    React.createElement("iframe", {
      ref: iframeRef,
      title: "embed-" + embedId,
      style: {
        width: "100%",
        height: 480,
        border: 0,
        display: "block",
        background: "var(--bg)",
      },
      sandbox: "allow-scripts allow-same-origin",
    })
  );
}
