/* global React, SH_useShell */
// The Inbox (revamp spec section 5): typed attention triage as a
// first-class center tab. The shell-level SH_AttentionEngine owns the
// data (cross-workspace pending fan-out + approval records + triage);
// this doc is a VIEW over shell.attentionRef, re-rendering on the
// engine's "sh-attention" event.
//
// Sections by consequence: decisions (approval gates, deciding inline
// via SH_DecisionCard), asking (parked questions), then a collapsed
// digest. Keyboard-first: ArrowUp/Down move the focus ring, Enter
// opens the item's session (cross-workspace aware). Resolved items
// stay queryable behind the "Show resolved" toggle - triage filters,
// it never deletes.

function SH_InboxDoc() {
  var shell = SH_useShell();
  var tickState = React.useState(0);
  var setTick = tickState[1];
  var selState = React.useState(0);
  var sel = selState[0];
  var setSel = selState[1];
  var showResolvedState = React.useState(false);
  var showResolved = showResolvedState[0];
  var setShowResolved = showResolvedState[1];

  React.useEffect(function () {
    function bump() { setTick(function (v) { return v + 1; }); }
    window.addEventListener("sh-attention", bump);
    return function () { window.removeEventListener("sh-attention", bump); };
  }, []);

  var state = shell.attentionRef.current || { items: [], triage: null };
  var items = state.items || [];
  // Digest-tier rows are RESOLVED records: they belong under "Show
  // resolved", never among live decisions (a decided approval rendered
  // as a decidable card again is the bug this filter kills).
  var decisions = items.filter(function (i) {
    return i.kind === "approval" && i.tier !== "digest";
  });
  var asking = items.filter(function (i) {
    return i.kind === "question" && i.tier !== "digest";
  });
  var digest = items.filter(function (i) { return i.tier === "digest"; });
  var focusable = decisions.concat(asking);

  function openItem(item) {
    var wid = item.workspaceId || shell.wid;
    if (wid !== shell.wid) {
      window.location.hash = window.SH_buildUrl({
        wid: wid, doc: { kind: "session", ref: item.sessionId },
      });
      return;
    }
    shell.openDoc({ kind: "session", ref: item.sessionId, preview: true });
  }

  React.useEffect(function () {
    function onKey(ev) {
      if (ev.target && /input|textarea/i.test(ev.target.tagName || "")) return;
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        setSel(function (v) {
          return focusable.length ? (v + 1) % focusable.length : 0;
        });
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        setSel(function (v) {
          return focusable.length
            ? (v - 1 + focusable.length) % focusable.length : 0;
        });
      } else if (ev.key === "Enter" && focusable.length) {
        ev.preventDefault();
        openItem(focusable[Math.min(sel, focusable.length - 1)]);
      }
    }
    window.addEventListener("keydown", onKey);
    return function () { window.removeEventListener("keydown", onKey); };
  });

  function itemRow(item, idx, body) {
    return (
      <li key={item.id}
        className="sh-inbox-item"
        data-kind={item.kind}
        data-active={idx === sel ? "true" : "false"}
        data-testid={"inbox-item:" + item.sessionId}>
        <div className="sh-inbox-item-head">
          <span className="sh-chip" data-kind={item.kind}>
            {item.kind === "approval" ? "decision" : "asking"}
          </span>
          <span className="sh-inbox-title">{item.title}</span>
          {item.workspaceId ? (
            <span className="sh-inbox-ws">{item.workspaceId}</span>
          ) : null}
          <button type="button" className="sh-verb"
            data-testid={"inbox-open:" + item.sessionId}
            onClick={function () { openItem(item); }}>Open</button>
          <window.SH_TriageVerbs item={item} />
        </div>
        {body}
      </li>
    );
  }

  return (
    <div className="sh-inbox" data-testid="shell-inbox">
      <div className="sh-inbox-bar">
        <span className="sh-inbox-count">
          {focusable.length
            ? focusable.length + " need" + (focusable.length === 1 ? "s" : "") + " you"
            : "Nothing needs you"}
        </span>
        <label className="sh-inbox-toggle">
          <input type="checkbox" checked={showResolved}
            data-testid="inbox-show-resolved"
            onChange={function (ev) { setShowResolved(ev.target.checked); }} />
          Show resolved
        </label>
      </div>

      {focusable.length === 0 && digest.length === 0 ? (
        <div className="sh-empty">
          <span>Nothing needs your attention.</span>
          <button type="button"
            onClick={function () {
              var verb = shell.registry.get("session.create");
              if (verb) verb.run();
            }}>Create Session</button>
        </div>
      ) : null}

      {decisions.length ? (
        <ul className="sh-inbox-list" data-testid="inbox-decisions">
          {decisions.map(function (item, i) {
            return itemRow(item, i, (
              <window.SH_DecisionCard item={item}
                onResolved={function () {
                  if (state.refetch) state.refetch();
                }} />
            ));
          })}
        </ul>
      ) : null}

      {asking.length ? (
        <ul className="sh-inbox-list" data-testid="inbox-asking">
          {asking.map(function (item, i) {
            return itemRow(item, decisions.length + i, (
              item.preview ? (
                <div className="sh-inbox-preview">{item.preview}</div>
              ) : null
            ));
          })}
        </ul>
      ) : null}

      {showResolved && digest.length ? (
        <details className="sh-attention-digest" open>
          <summary>Resolved ({digest.length})</summary>
          <ul>
            {digest.map(function (item) {
              return <li key={item.id}>{item.title}</li>;
            })}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

window.SH_InboxDoc = SH_InboxDoc;
