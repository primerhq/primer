/* global React, Icon */

// chat-stream mockup. Shows a chat thread with three messages
// (user, assistant, user) and a streaming-dots indicator under the
// last assistant message.

function ChatStreamMockup({
  chatId = "chat-x9y8z7",
  streaming = true,
  userName = "alex",
  agentName = "helper",
}) {
  const Bubble = ({ role, text, streamingDots }) => {
    const isUser = role === "user";
    return (
      <div style={{
        display: "flex",
        justifyContent: isUser ? "flex-end" : "flex-start",
        marginBottom: 10,
      }}>
        <div style={{
          maxWidth: "75%",
          padding: "8px 12px",
          borderRadius: 10,
          borderBottomRightRadius: isUser ? 2 : 10,
          borderBottomLeftRadius: isUser ? 10 : 2,
          background: isUser ? "var(--accent)" : "var(--bg-2)",
          color: isUser ? "#fff" : "var(--text)",
          fontSize: 13,
          lineHeight: 1.4,
        }}>
          <div style={{
            fontSize: 10, fontWeight: 600,
            opacity: 0.7, textTransform: "uppercase",
            letterSpacing: "0.05em", marginBottom: 4,
          }}>
            {isUser ? userName : agentName}
          </div>
          {text}
          {streamingDots && (
            <span style={{ marginLeft: 8, opacity: 0.7 }}>...</span>
          )}
        </div>
      </div>
    );
  };
  return (
    <div style={{
      border: "1px solid var(--border)",
      borderRadius: 6,
      background: "var(--bg)",
      padding: 12,
      minHeight: 280,
      display: "flex",
      flexDirection: "column",
    }}>
      <div style={{
        fontSize: 11, color: "var(--text-3)",
        marginBottom: 10, fontFamily: "var(--mono)",
      }}>
        chat: {chatId}
      </div>
      <Bubble role="user" text="Hey, can you summarise yesterday's incident report?" />
      <Bubble role="assistant" text="Of course. Pulling the on-call channel logs now." />
      <Bubble role="user" text="Just the high-severity ones, please." />
      <Bubble
        role="assistant"
        text="On it. Two SEV-1s yesterday: the auth latency spike at 14:02 and the queue backlog at 22:50"
        streamingDots={streaming}
      />
      <div style={{
        marginTop: "auto",
        padding: "8px 10px",
        background: "var(--bg-2)",
        borderRadius: 6,
        color: "var(--text-3)",
        fontSize: 12,
        display: "flex",
        alignItems: "center",
        gap: 8,
      }}>
        <Icon name="message" size={13} />
        <span>Type a message...</span>
        <kbd style={{ marginLeft: "auto", fontSize: 10 }}>Enter</kbd>
      </div>
    </div>
  );
}

window.ChatStreamMockup = ChatStreamMockup;
