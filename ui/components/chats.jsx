/* global React, Icon, Btn, Modal, Banner, ApprovalBanner, relativeTime, fmtDate */

const CHATS = [
  { id: "chat-7f3a9c", agent_id: "support-triage", status: "active", last_seq: 18, created_at_ago: 1200 },
  { id: "chat-2b8e1c", agent_id: "stripe-refunds", status: "active", last_seq: 6, created_at_ago: 380, hasPendingApproval: true },
  { id: "chat-9d4f2a", agent_id: "pr-reviewer", status: "active", last_seq: 24, created_at_ago: 3600 * 2 },
  { id: "chat-1e8c4b", agent_id: "code-explainer", status: "ended", last_seq: 42, created_at_ago: 3600 * 12 },
];

const SAMPLE_MESSAGES = {
  "chat-7f3a9c": [
    { seq: 1, role: "user", text: "How many sessions are running right now?" },
    { seq: 2, role: "agent", text: "Let me check using list_sessions." },
    { seq: 3, role: "tool_call", name: "list_sessions", args: { status: "running" } },
    { seq: 4, role: "tool_result", name: "list_sessions", result: "3 rows" },
    { seq: 5, role: "agent", text: "There are 3 sessions currently running: sess-7f3a9c, sess-1c4d8b, and sess-9b2e6f." },
    { seq: 6, role: "user", text: "Which one has the most turns?" },
    { seq: 7, role: "agent", text: "sess-7f3a9c is on turn 3, the others are on turns 2 and 1 respectively." },
  ],
  "chat-2b8e1c": [
    { seq: 1, role: "user", text: "Refund charge ch_3OZ4mQ for the customer who was double-billed." },
    { seq: 2, role: "agent", text: "I'll look up the charge first." },
    { seq: 3, role: "tool_call", name: "stripe.search_charges", args: { query: "ch_3OZ4mQ" } },
    { seq: 4, role: "tool_result", name: "stripe.search_charges", result: "1 result · $148.50 · paid" },
    { seq: 5, role: "agent", text: "Found it — $148.50 charge. I'll issue the full refund. Calling stripe.create_refund…" },
    { seq: 6, role: "tool_call", name: "stripe.create_refund", args: { charge: "ch_3OZ4mQ", amount: 14850, reason: "duplicate" }, pending_approval: true },
  ],
};

function ChatsPage({ onOpen, pushToast }) {
  const [showNew, setShowNew] = React.useState(false);
  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter chats…" />
        </div>
        <div className="sep-v" />
        <select className="select"><option>all agents</option>{window.MOCK.AGENTS.map((a) => <option key={a.id}>{a.id}</option>)}</select>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setShowNew(true)}>New chat</Btn>
        </div>
      </div>
      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>ID</th>
              <th>Agent</th>
              <th>Status</th>
              <th style={{ textAlign: "right" }}>Messages</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {CHATS.map((c) => (
              <tr key={c.id} onClick={() => onOpen(c.id)}>
                <td className="mono">
                  {c.id}
                  {c.hasPendingApproval && <span className="pill pill-paused" style={{ marginLeft: 8, fontSize: 9.5 }}><span className="dot"></span>approval pending</span>}
                </td>
                <td className="mono">{c.agent_id}</td>
                <td>
                  {c.status === "active" ? <span className="pill pill-running"><span className="dot"></span>active</span> : <span className="pill pill-ended"><span className="dot"></span>ended</span>}
                </td>
                <td className="mono num tabular">{c.last_seq}</td>
                <td className="mono muted">{relativeTime(c.created_at_ago)}</td>
                <td style={{ textAlign: "right", paddingRight: 12 }}><Icon name="chevron-right" size={12} className="muted" /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {showNew && (
        <Modal
          title="New chat"
          onClose={() => setShowNew(false)}
          footer={<><Btn kind="ghost" onClick={() => setShowNew(false)}>Cancel</Btn><Btn kind="primary" icon="plus" onClick={() => { setShowNew(false); pushToast({ kind: "success", title: "Chat created", detail: "POST /v1/chats → 201" }); }}>Create chat</Btn></>}
        >
          <div className="field"><label className="field-label">agent</label><select className="select mono" style={{ width: "100%" }}>{window.MOCK.AGENTS.map((a) => <option key={a.id}>{a.id}</option>)}</select></div>
          <div className="field"><label className="field-label">initial instructions <span className="hint">optional</span></label><textarea className="textarea" rows={4} placeholder="What should the agent know before starting?" /></div>
        </Modal>
      )}
    </div>
  );
}

function ChatDetail({ chatId, onBack, pushToast }) {
  const chat = CHATS.find((c) => c.id === chatId);
  const initialMessages = SAMPLE_MESSAGES[chatId] || [];
  const [messages, setMessages] = React.useState(initialMessages);
  const [composer, setComposer] = React.useState("");
  const [showAutoReject, setShowAutoReject] = React.useState(false);
  const scrollRef = React.useRef(null);

  // Pending approval state from chat fixture
  const pending = chat?.hasPendingApproval ? {
    tool_call_id: "tc-f2c918",
    tool_name: "stripe.create_refund",
    toolset_id: "stripe-mcp",
    arguments: { charge: "ch_3OZ4mQ", amount: 14850, reason: "duplicate" },
    policy_id: "approve-stripe-refund",
    approval_type: "policy",
    gate_reason: "amount 14850 > 10000 cents",
  } : null;

  const [resolvedApproval, setResolvedApproval] = React.useState(null);

  React.useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, resolvedApproval]);

  const send = () => {
    if (!composer.trim()) return;
    if (pending && !resolvedApproval) {
      setShowAutoReject(true);
      return;
    }
    setMessages((m) => [...m, { seq: m.length + 1, role: "user", text: composer }, { seq: m.length + 2, role: "agent", text: "Working on it…" }]);
    setComposer("");
  };

  const approve = () => {
    setResolvedApproval({ decision: "approved", at: new Date() });
    pushToast({ kind: "success", title: "Approved", detail: "Approval sent over WS · tool_approval_decide" });
  };
  const reject = (reason) => {
    setResolvedApproval({ decision: "rejected", at: new Date(), reason });
    pushToast({ kind: "warning", title: "Rejected", detail: `"${reason}"` });
  };
  const autoRejectAndSend = () => {
    setShowAutoReject(false);
    setResolvedApproval({ decision: "rejected", at: new Date(), reason: "auto-rejected by new message" });
    setMessages((m) => [...m, { seq: m.length + 1, role: "user", text: composer }, { seq: m.length + 2, role: "agent", text: "I'll abandon the refund call and address your new message." }]);
    setComposer("");
  };

  if (!chat) return null;

  return (
    <div className="col" style={{ gap: 14, height: "calc(100vh - 180px)", display: "flex", flexDirection: "column" }}>
      <div className="panel" style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
        <div className="panel-h">
          <Icon name="send" size={13} style={{ color: "var(--accent)" }} />
          <span className="mono">{chat.id}</span>
          <span className="sub">· agent <span className="mono">{chat.agent_id}</span></span>
          <div className="right">
            <span className={chat.status === "active" ? "pill pill-running" : "pill pill-ended"}>
              <span className="dot"></span>{chat.status}
            </span>
            <Btn size="sm" kind="ghost" icon="x">End chat</Btn>
          </div>
        </div>
        <div ref={scrollRef} style={{ flex: 1, overflow: "auto", padding: "18px 24px", minHeight: 0 }}>
          {messages.map((m, i) => <Message key={i} m={m} />)}

          {/* Inline approval card */}
          {pending && !resolvedApproval && (
            <div style={{ marginLeft: 60, marginTop: 6 }}>
              <ApprovalBanner
                approval={pending}
                onApprove={approve}
                onReject={reject}
              />
            </div>
          )}

          {/* Resolved approval marker */}
          {resolvedApproval && (
            <div style={{ marginLeft: 60, marginTop: 6, padding: "8px 12px", background: "var(--bg-1)", border: "1px solid var(--border)", borderRadius: 6, fontSize: 12.5, color: "var(--text-2)", display: "flex", alignItems: "center", gap: 8 }}>
              <Icon name={resolvedApproval.decision === "approved" ? "check" : "x"} size={13} style={{ color: resolvedApproval.decision === "approved" ? "var(--green)" : "var(--red)" }} />
              <span style={{ color: resolvedApproval.decision === "approved" ? "var(--green)" : "var(--red)" }}>
                {resolvedApproval.decision === "approved" ? "Approved" : "Rejected"}
              </span>
              {resolvedApproval.reason && <span className="muted">— "{resolvedApproval.reason}"</span>}
              <span className="muted mono text-sm" style={{ marginLeft: "auto" }}>by operator at {fmtDate(resolvedApproval.at).slice(11)}</span>
              <span className="mono muted text-sm">stripe.create_refund</span>
            </div>
          )}
        </div>

        <div style={{ borderTop: "1px solid var(--border)", padding: 14, display: "flex", gap: 8, alignItems: "flex-end" }}>
          <textarea
            className="textarea"
            value={composer}
            onChange={(e) => setComposer(e.target.value)}
            placeholder="Send a message…"
            rows={2}
            style={{ flex: 1, resize: "none" }}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
          />
          <Btn kind="primary" icon="send" disabled={!composer.trim()} onClick={send}>Send</Btn>
        </div>
      </div>

      {showAutoReject && (
        <Modal
          title="Auto-reject pending approval?"
          onClose={() => setShowAutoReject(false)}
          danger
          footer={
            <>
              <Btn kind="ghost" onClick={() => setShowAutoReject(false)}>Cancel</Btn>
              <Btn kind="danger" icon="send" onClick={autoRejectAndSend}>Send &amp; reject</Btn>
            </>
          }
        >
          Sending a new message will <strong>auto-reject the pending approval</strong> for <span className="mono">{pending.tool_name}</span>. The server applies an automatic rejection per §10.1.
        </Modal>
      )}
    </div>
  );
}

function Message({ m }) {
  if (m.role === "tool_call") {
    return (
      <div style={{ marginLeft: 60, marginTop: 6, marginBottom: 6 }}>
        <div className="tool-call">
          <Icon name="play" size={10} style={{ color: m.pending_approval ? "var(--amber)" : "var(--text-3)" }} />
          <span className="name">{m.name}</span>
          <span className="arrow">(</span>
          <span className="muted">{JSON.stringify(m.args).slice(0, 80)}</span>
          <span className="arrow">)</span>
          {m.pending_approval && <span className="pill pill-paused" style={{ marginLeft: "auto" }}><span className="dot"></span>awaiting approval</span>}
        </div>
      </div>
    );
  }
  if (m.role === "tool_result") {
    return (
      <div style={{ marginLeft: 60, marginTop: 2, marginBottom: 6 }}>
        <div className="tool-call" style={{ borderLeft: "2px solid var(--green)" }}>
          <Icon name="check" size={10} style={{ color: "var(--green)" }} />
          <span className="name">{m.name}</span>
          <span className="arrow">→</span>
          <span className="muted">{m.result}</span>
        </div>
      </div>
    );
  }
  const isUser = m.role === "user";
  return (
    <div style={{ display: "flex", gap: 12, marginBottom: 14 }}>
      <div style={{
        width: 48, flexShrink: 0,
        fontFamily: "IBM Plex Mono, monospace",
        fontSize: 10.5,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        color: isUser ? "var(--text-2)" : "var(--accent)",
        fontWeight: 600,
        paddingTop: 2,
      }}>{isUser ? "user" : "agent"}</div>
      <div style={{ flex: 1, fontSize: 13, lineHeight: 1.55, color: "var(--text)", borderLeft: `2px solid ${isUser ? "var(--border)" : "var(--accent)"}`, paddingLeft: 12 }}>
        {m.text}
      </div>
    </div>
  );
}

window.ChatsPage = ChatsPage;
window.ChatDetail = ChatDetail;
window.CHATS_DATA = CHATS;
