/* global React */
//
// TokenMeter — pill rendering `<input>/<context_length> (<pct>%)`.
//
// Color band: green (<60%), amber (60-89%), red (>=90%).
// Read-only when `onCompact` is not supplied.
//
// Props:
//   - inputTokens     : number            (default 0)
//   - contextLength   : number            (default 0; meter dims out at 0)
//   - onCompact       : () => void | null (renders the compress button when supplied)
//   - compactDisabled : boolean           (greys the button + sets the tooltip)
//   - compactTooltip  : string            (button tooltip when disabled)

const TokenMeter = ({
  inputTokens = 0,
  contextLength = 0,
  onCompact = null,
  compactDisabled = false,
  compactTooltip = "",
}) => {
  const pct = contextLength > 0 ? inputTokens / contextLength : 0;
  let band = "green";
  if (pct >= 0.9) band = "red";
  else if (pct >= 0.6) band = "amber";

  const bg = {
    green: "var(--green)",
    amber: "var(--amber)",
    red: "var(--red)",
  }[band];

  const label = `${inputTokens.toLocaleString()} / ${contextLength.toLocaleString()} (${(pct * 100).toFixed(0)}%)`;

  return (
    <span className="token-meter" style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
      <span
        style={{
          padding: "2px 8px",
          borderRadius: 12,
          backgroundColor: bg,
          color: "#fff",
          fontSize: 11,
          fontWeight: 500,
          opacity: contextLength > 0 ? 1 : 0.5,
        }}
        title={`Prompt token usage: ${label}`}
      >
        {label}
      </span>
      {onCompact && (
        <window.Btn
          icon="compress"
          size="xs"
          onClick={onCompact}
          disabled={compactDisabled}
          title={compactDisabled ? compactTooltip : "Compact now"}
        />
      )}
    </span>
  );
};

window.TokenMeter = TokenMeter;
