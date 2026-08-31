/* global React */

// CapabilityBadges - the y/w/r/n tool-capability flags, one shared render
// for every tool picker (agent builder, MCP allowlist, python toolset
// dry-run pane, approval-policy tool pattern, ...). Mirrors the
// prototype's flagBadges()/FLAGS fixture exactly (uiv2/Primer Console.dc.html):
// four badges always render, dimmed when the flag does not apply to this
// tool, colored + full-opacity + strong border when it does.
//
// Registered on window.primerApi the same way pager.jsx / entity-picker.jsx
// are, so any component can render window.primerApi.CapabilityBadges (or
// the bare global CapabilityBadges).
//
// tool shape: the four raw fields GET /tools/catalogue and GET /tools now
// carry per tool (batch-2 catalogue-badges work): yields (bool),
// requires_workspace (bool), tool_class ("standard"|"notifying"),
// required_role (string|null).

(function () {
  const FLAG_META = {
    y: { title: "yields — parks the run", color: "var(--amber)" },
    w: { title: "requires workspace", color: "var(--teal)" },
    r: { title: "role-gated", color: "var(--violet)" },
    n: { title: "notifying — fire and forget", color: "var(--pink)" },
  };

  // Derive the active-flag set from a catalogue tool row.
  //
  // RULING (R4 review, recorded so this does not resurface): "r" means
  // the tool DECLARES a required_role - a property of the tool itself,
  // set at its make_tool call site. It does NOT mean "is this tool
  // effectively role-gated once exposed over MCP" - primer/toolset/
  // internal.py's required_role() defaults an UNDECLARED tool to
  // "admin" for MCP dispatch specifically, which is an EXPOSURE POLICY
  // (a property of the surface a tool is reached through), not a
  // property of the tool. That distinction belongs on the MCP-allowlist
  // page's verdict column (R5), which can show "effectively admin-only
  // over MCP" for an undeclared tool without this shared badge lying
  // about every tool everywhere else it renders (agent builder, python
  // dry-run, approval-policy pattern - none of which go through MCP
  // dispatch at all).
  function capabilityFlags(tool) {
    const flags = [];
    if (tool && tool.yields) flags.push("y");
    if (tool && tool.requires_workspace) flags.push("w");
    if (tool && tool.required_role) flags.push("r");
    if (tool && tool.tool_class === "notifying") flags.push("n");
    return flags;
  }

  function CapabilityBadges(props) {
    const flags = props.flags || capabilityFlags(props.tool);
    return (
      <span style={{ display: "flex", gap: 3, flexShrink: 0 }} data-testid={props.testid || "cap-badges"}>
        {["y", "w", "r", "n"].map((k) => {
          const on = flags.indexOf(k) >= 0;
          const meta = FLAG_META[k];
          return (
            <span
              key={k}
              title={meta.title}
              data-testid={"cap-badge-" + k}
              data-on={on ? "true" : "false"}
              style={{
                width: 15, height: 15, display: "grid", placeItems: "center",
                border: "1px solid " + (on ? "var(--border-strong)" : "var(--border)"),
                borderRadius: 3, fontFamily: "var(--font-mono)", fontSize: 8.5,
                color: on ? meta.color : "var(--text-4)",
                opacity: on ? 1 : 0.35,
              }}
            >
              {k}
            </span>
          );
        })}
      </span>
    );
  }

  window.CapabilityBadges = CapabilityBadges;
  window.capabilityFlags = capabilityFlags;
  const ns = (window.primerApi = window.primerApi || {});
  ns.CapabilityBadges = CapabilityBadges;
  ns.capabilityFlags = capabilityFlags;
})();
