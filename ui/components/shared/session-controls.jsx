// Shared session binding controls: a chip that says what is running,
// and a switcher that changes it.
//
// PROPS ONLY. No window.location, no route table, no console shell
// import. The studio hosts it today and a different shell will host it
// tomorrow, so anything ambient here would have to be rewritten rather
// than re-hosted.
//
// Ported from CT_AgentSwitcher (the chat UI), with two changes the
// session surface forces: graphs are offered as well as agents, because
// the binding endpoint takes both; and the switch is NOT applied
// optimistically, because a busy session queues it and the row does not
// change until the drain checkpoint applies it. Showing the new agent
// straight away would lie for the length of a turn.

function SessionBindingChip({ binding, bindingEpoch }) {
  if (!binding) return null;
  const isGraph = binding.kind === "graph";
  const label = isGraph ? binding.graph_id : binding.agent_id;
  return (
    <span
      className="chip"
      data-testid="session-binding-chip"
      title={
        isGraph
          ? `Graph ${label} runs this session`
          : `Agent ${label} runs this session`
      }
    >
      <span className="chip-glyph">{isGraph ? "\u25C8" : "\u25C6"}</span>
      <span className="chip-label">{label}</span>
      {bindingEpoch > 0 && (
        // Epochs are how a reader tells one hand-off from the next; a
        // session that has never switched shows nothing.
        <span className="chip-epoch muted" data-testid="binding-epoch">
          {"\u00B7 v" + bindingEpoch}
        </span>
      )}
    </span>
  );
}

function SessionSwitcher({
  sessionId,
  workspaceId,
  binding,
  onSwitched,
  pushToast,
}) {
  const [open, setOpen] = React.useState(false);
  const [filter, setFilter] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [queued, setQueued] = React.useState(false);

  const apiFetch = window.primerApi.apiFetch;
  const agents = window.primerApi.useResource(
    "agents:switcher",
    (signal) => apiFetch("GET", "/agents?limit=200", null, { signal }),
    {},
  );
  const graphs = window.primerApi.useResource(
    "graphs:switcher",
    (signal) => apiFetch("GET", "/graphs?limit=200", null, { signal }),
    {},
  );

  React.useEffect(() => {
    function onKey(e) {
      if (e.ctrlKey && e.shiftKey && (e.key === "A" || e.key === "a")) {
        e.preventDefault();
        setOpen((v) => !v);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const entries = [];
  for (const a of agents.data?.items ?? []) {
    entries.push({ kind: "agent", id: a.id, label: a.id });
  }
  for (const g of graphs.data?.items ?? []) {
    entries.push({ kind: "graph", id: g.id, label: g.id });
  }
  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? entries.filter((e) => e.label.toLowerCase().indexOf(needle) !== -1)
    : entries;

  async function choose(entry) {
    if (busy) return;
    setBusy(true);
    try {
      const body = { kind: entry.kind };
      if (entry.kind === "graph") body.graph_id = entry.id;
      else body.agent_id = entry.id;

      const row = await apiFetch(
        "POST",
        `/workspaces/${workspaceId}/sessions/${sessionId}/binding`,
        body,
      );
      // A busy session queues the switch: the endpoint returns the row
      // unchanged apart from pending_binding_switch, and the binding
      // itself only moves when the checkpoint applies it.
      const isQueued = !!(row && row.pending_binding_switch);
      setQueued(isQueued);
      setOpen(false);
      if (pushToast) {
        pushToast({
          kind: "info",
          title: isQueued ? "Switch queued" : "Switched",
          detail: isQueued
            ? `${entry.label} takes over when this turn finishes`
            : `${entry.label} runs this session now`,
        });
      }
      // The host re-reads from the row (or the tap) rather than us
      // rewriting local state, so a queued switch never renders as done.
      if (onSwitched) onSwitched(row);
    } catch (err) {
      if (pushToast) {
        pushToast({
          kind: "error",
          title: "Switch failed",
          detail: String((err && err.message) || err),
        });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="session-switcher" data-testid="session-switcher">
      <button
        type="button"
        className="btn-ghost"
        data-testid="session-switcher-open"
        onClick={() => setOpen((v) => !v)}
        title="Switch agent or graph (Ctrl+Shift+A)"
      >
        Switch
      </button>
      {queued && (
        <span className="muted text-sm" data-testid="session-switch-queued">
          switch queued
        </span>
      )}
      {open && (
        <div className="palette-overlay" data-testid="session-switcher-panel">
          <input
            autoFocus
            className="palette-input"
            data-testid="session-switcher-filter"
            placeholder="Agent or graph..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <ul className="palette-list">
            {shown.map((entry) => (
              <li key={entry.kind + ":" + entry.id}>
                <button
                  type="button"
                  className="palette-row"
                  data-testid="session-switcher-entry"
                  disabled={busy}
                  onClick={() => choose(entry)}
                >
                  <span className="palette-glyph">
                    {entry.kind === "graph" ? "\u25C8" : "\u25C6"}
                  </span>
                  <span className="palette-label">{entry.label}</span>
                  {binding
                    && binding.kind === entry.kind
                    && (binding.agent_id === entry.id
                      || binding.graph_id === entry.id) && (
                    <span className="muted text-sm">current</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </span>
  );
}

window.SessionBindingChip = SessionBindingChip;
window.SessionSwitcher = SessionSwitcher;


// Stream a completed assistant turn as chunked mp3.
//
// The proxy is a POST with a JSON body, so an <audio src=...> cannot
// fetch it directly; the response body is instead pumped into a
// MediaSource as it arrives, which is what makes playback start on the
// first frames instead of after the last one. Awaiting response.blob()
// would collect the whole synthesis first and hand back exactly the
// latency Task 13's yield-through pump exists to remove. MediaSource
// with "audio/mpeg" is the incremental path; the blob is the fallback
// for engines that do not advertise it. Nothing here touches the
// transcript -- audio is ephemeral, text is the record.
function SC_speakTurn(text, agentId) {
  const element = new Audio();
  element.autoplay = true;

  const request = () =>
    fetch(window.primerApi.resolvePath("/audio/speech"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ input: text, agent_id: agentId || null }),
    }).then((response) => {
      if (!response.ok) throw new Error(`speech proxy returned ${response.status}`);
      return response;
    });

  const canStream =
    typeof window.MediaSource === "function" &&
    window.MediaSource.isTypeSupported("audio/mpeg");

  if (!canStream) {
    request()
      .then((response) => response.blob())
      .then((blob) => {
        element.src = URL.createObjectURL(blob);
        element.addEventListener("ended", () => URL.revokeObjectURL(element.src));
      })
      .catch(() => { /* playback is best-effort; the text already landed */ });
    return element;
  }

  const media = new window.MediaSource();
  element.src = URL.createObjectURL(media);
  element.addEventListener("ended", () => URL.revokeObjectURL(element.src));

  media.addEventListener("sourceopen", () => {
    let buffer;
    try {
      buffer = media.addSourceBuffer("audio/mpeg");
    } catch {
      return;
    }
    // SourceBuffer.appendBuffer is single-flight: a second append before
    // "updateend" throws InvalidStateError, so chunks queue here.
    const queue = [];
    let done = false;
    const drain = () => {
      if (buffer.updating) return;
      if (queue.length > 0) {
        buffer.appendBuffer(queue.shift());
        return;
      }
      if (done && media.readyState === "open") media.endOfStream();
    };
    buffer.addEventListener("updateend", drain);

    request()
      .then((response) => {
        const reader = response.body.getReader();
        const pump = () =>
          reader.read().then(({ done: finished, value }) => {
            if (finished) {
              done = true;
              drain();
              return;
            }
            queue.push(value);
            drain();
            return pump();
          });
        return pump();
      })
      .catch(() => {
        done = true;
        try { drain(); } catch { /* the element is already detached */ }
      });
  });

  return element;
}


// Props-only, like SessionBindingChip and SessionSwitcher beside it (S1 P6
// Task 33): no route table, no window.location, no host import, so S8's session
// doc re-hosts it unchanged. The host passes the session's own conversation
// state; today that host is studio-center.jsx's session panels.
function SessionSpeakerToggle({ agentId, messages, turnInFlight }) {
  const caps = window.primerApi.useCapabilities();
  const speechCaps = (caps.data && caps.data.speech) || {};
  const [speakerOn, setSpeakerOn] = React.useState(false);
  const spokenRef = React.useRef(new Set());

  React.useEffect(() => {
    if (!speakerOn || turnInFlight) return;
    const last = (messages || [])[(messages || []).length - 1];
    if (!last || last.role !== "assistant" || !last.content) return;
    if (spokenRef.current.has(last.id)) return;
    spokenRef.current.add(last.id);
    SC_speakTurn(last.content, agentId);
  }, [speakerOn, turnInFlight, messages, agentId]);

  return (
    <div className="row" style={{ gap: 6, alignItems: "center" }}>
        {!!speechCaps.tts_configured && (
          <button
            type="button"
            data-testid="chat-speaker-toggle"
            className={"icon-btn" + (speakerOn ? " is-active" : "")}
            title={speakerOn ? "Turn speech off" : "Read replies aloud"}
            onClick={() => setSpeakerOn((on) => !on)}
          >
            <Icon name={speakerOn ? "volume" : "volume-off"} size={18} />
          </button>
        )}
    </div>
  );
}


window.SessionSpeakerToggle = SessionSpeakerToggle;
