/* global React, GB_parseTemplate, GB_serialize, GB_availableRefs, GB_chipLabel, GB_refIsBroken */
// GB_RefEditor - the chip text area, and GB_RefPicker - the insert popover.
// The stored value is always a Jinja string; chips are a view over it, so a
// graph authored in JSON round-trips untouched. WIRING.md §7.

// Read the contenteditable DOM back into a Jinja string. Chips are
// contenteditable=false spans carrying data-ref; everything else is literal text.
function GB_domToTemplate(root) {
  let out = "";
  const walk = (node) => {
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType === 3) { out += child.nodeValue; continue; }
      if (child.nodeType !== 1) continue;
      const el = child;
      if (el.dataset && el.dataset.ref) { out += `{{ ${el.dataset.ref} }}`; continue; }
      if (el.dataset && el.dataset.raw) { out += el.dataset.raw; continue; }
      if (el.tagName === "BR") { out += "\n"; continue; }
      if (el.tagName === "DIV" && out && !out.endsWith("\n")) out += "\n";
      walk(el);
    }
  };
  walk(root);
  return out;
}

function GB_RefEditor(props) {
  const { value, onChange, draft, nodeId, placeholder, readOnly, sampleByExpr } = props;
  const { useRef, useEffect, useState } = React;
  const ref = useRef(null);
  const lastEmitted = useRef(value == null ? "" : String(value));
  const [picker, setPicker] = useState(null); // {slashLen} when open

  // Paint tokens into the DOM. Only when the incoming value differs from what
  // we last emitted, so typing never fights the cursor.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const incoming = value == null ? "" : String(value);
    if (incoming === lastEmitted.current && el.childNodes.length) return;
    lastEmitted.current = incoming;
    el.innerHTML = "";
    for (const tk of GB_parseTemplate(incoming)) {
      if (tk.t === "text") {
        const parts = String(tk.v).split("\n");
        parts.forEach((p, i) => {
          if (i) el.appendChild(document.createElement("br"));
          if (p) el.appendChild(document.createTextNode(p));
        });
        continue;
      }
      el.appendChild(GB_makeChip(tk, draft));
    }
  }, [value, draft]);

  const emit = () => {
    const el = ref.current;
    if (!el) return;
    const text = GB_domToTemplate(el);
    lastEmitted.current = text;
    onChange(text);
  };

  const insertRef = (expr) => {
    const el = ref.current;
    if (!el) return;
    const sel = window.getSelection();
    const chip = GB_makeChip({ t: "ref", v: expr, ...GB_splitSafe(expr) }, draft);
    const space = document.createTextNode(" ");
    let range = sel && sel.rangeCount ? sel.getRangeAt(0) : null;
    if (!range || !el.contains(range.commonAncestorContainer)) {
      el.appendChild(chip);
      el.appendChild(space);
    } else {
      // Drop the "/" trigger the user typed, then insert the chip there.
      if (picker && picker.slashLen) {
        for (let i = 0; i < picker.slashLen; i++) range.setStart(range.startContainer, Math.max(0, range.startOffset - 1));
        range.deleteContents();
      }
      range.insertNode(space);
      range.insertNode(chip);
      range.setStartAfter(space);
      range.collapse(true);
      sel.removeAllRanges();
      sel.addRange(range);
    }
    setPicker(null);
    emit();
    el.focus();
  };

  return (
    <div className="col" style={{ gap: 6, position: "relative" }}>
      <div
        data-testid="gb-ref-editor"
        ref={ref}
        contentEditable={!readOnly}
        suppressContentEditableWarning
        onInput={(e) => {
          const txt = (window.getSelection() && window.getSelection().anchorNode
            && window.getSelection().anchorNode.nodeValue) || "";
          const caret = window.getSelection() ? window.getSelection().anchorOffset : 0;
          if (txt.slice(Math.max(0, caret - 1), caret) === "/") setPicker({ slashLen: 1 });
          else if (picker && !/\/$/.test(txt.slice(0, caret))) setPicker(null);
          emit(e);
        }}
        onBlur={emit}
        onKeyDown={(e) => { if (e.key === "Escape" && picker) { e.preventDefault(); setPicker(null); } }}
        style={{
          minHeight: 60, padding: 10, background: "var(--bg-1)",
          border: "1px solid var(--border)", borderRadius: "var(--r-9)",
          fontSize: "var(--fs-12)", lineHeight: 1.9, color: "var(--text-2)",
          outline: "none", whiteSpace: "pre-wrap",
        }}
        data-placeholder={placeholder || ""}
      />
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
          Type <span className="mono" style={{ color: "var(--text-2)" }}>/</span> or use Insert to add a value from an earlier step. Chips stay correct when steps are renamed.
        </span>
        {!readOnly ? (
          <span
            onClick={() => setPicker({ slashLen: 0 })}
            style={{ marginLeft: "auto", fontSize: "var(--fs-11)", color: "var(--accent)", cursor: "pointer" }}
          >
            Insert…
          </span>
        ) : null}
      </div>

      {picker ? (
        <GB_RefPicker
          draft={draft}
          nodeId={nodeId}
          sampleByExpr={sampleByExpr}
          onPick={insertRef}
          onClose={() => setPicker(null)}
        />
      ) : null}
    </div>
  );
}

function GB_splitSafe(expr) {
  return window.GB_splitRef ? window.GB_splitRef(expr) : { nodeId: null, path: "" };
}

function GB_makeChip(token, draft) {
  const span = document.createElement("span");
  span.contentEditable = "false";
  span.dataset.ref = token.v;
  span.setAttribute("data-testid", "gb-ref-chip");
  const broken = GB_refIsBroken && GB_refIsBroken(draft, token);
  const label = broken ? "this step was deleted" : (GB_chipLabel ? GB_chipLabel(draft, token) : token.v);
  span.textContent = label;
  const tint = broken ? "var(--red)" : (token.v || "").startsWith("initial_input") ? "var(--green)" : "var(--blue)";
  span.style.cssText = [
    "display:inline-flex", "align-items:center", "gap:5px", "padding:2px 7px",
    "border-radius:6px", "font-size:var(--fs-11)", "margin:0 1px",
    `background:color-mix(in oklab, ${tint} 16%, transparent)`,
    `border:1px solid color-mix(in oklab, ${tint} 35%, transparent)`,
    `color:${tint}`,
  ].join(";");
  return span;
}

// The insert popover - grouped, with sample values when we have them.
function GB_RefPicker({ draft, nodeId, onPick, onClose, sampleByExpr }) {
  const { useState } = React;
  const [q, setQ] = useState("");
  const groups = GB_availableRefs ? GB_availableRefs(draft, nodeId) : [];
  const match = (r) => !q || (r.path + " " + (r.label || "")).toLowerCase().includes(q.toLowerCase());

  return (
    <div
      data-testid="gb-ref-picker"
      style={{
        position: "absolute", left: 0, right: 0, top: "100%", zIndex: 30, marginTop: 4,
        background: "var(--bg-elev)", border: "1px solid var(--border-strong)",
        borderRadius: 11, overflow: "hidden", boxShadow: "0 24px 50px -24px rgba(0,0,0,.9)",
      }}
    >
      <div style={{ padding: "9px 12px", borderBottom: "1px solid var(--border)" }}>
        <input
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Insert a value from…"
          onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}
          style={{
            width: "100%", background: "transparent", border: "none", outline: "none",
            color: "var(--text)", fontSize: "var(--fs-12)",
          }}
        />
      </div>
      <div style={{ maxHeight: 300, overflow: "auto" }}>
        {groups.map((g) => {
          const rows = (g.rows || []).filter(match).filter((r) => !r.more || q);
          if (!rows.length) return null;
          return (
            <div key={g.id}>
              <div
                className="row"
                style={{
                  gap: 6, padding: "8px 12px", fontSize: 10.5, color: "var(--text-4)",
                  background: "var(--bg-1)", alignItems: "center",
                }}
              >
                <span>{g.title}</span>
                {g.laterLoop ? <span style={{ marginLeft: "auto", fontSize: 10 }}>{g.note}</span> : null}
              </div>
              {rows.map((r) => {
                const sample = sampleByExpr && sampleByExpr[r.expr];
                return (
                  <div
                    key={r.expr}
                    data-testid="gb-ref-row"
                    data-path={r.expr}
                    onClick={() => onPick(r.expr)}
                    className="row"
                    style={{
                      gap: 10, alignItems: "center", padding: "9px 12px", cursor: "pointer",
                      opacity: g.laterLoop ? 0.5 : 1,
                    }}
                  >
                    <span className="mono" style={{ fontSize: "var(--fs-11)", flex: "0 0 96px", color: "var(--text)" }}>{r.path}</span>
                    <span
                      className="muted"
                      style={{ fontSize: "var(--fs-11)", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                    >
                      {sample != null ? String(sample) : (r.label || "")}
                    </span>
                    <span style={{ marginLeft: "auto", fontSize: 10, color: "var(--text-4)", flex: "0 0 auto" }}>{r.type}</span>
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
      <div
        className="row"
        style={{
          padding: "8px 12px", borderTop: "1px solid var(--border)", fontSize: 10.5,
          color: "var(--text-4)", gap: 12,
        }}
      >
        <span>{sampleByExpr ? "Values are from the last run" : "Types come from each step's declared fields"}</span>
        <span className="mono" style={{ marginLeft: "auto" }}>esc to close</span>
      </div>
    </div>
  );
}

Object.assign(window, { GB_RefEditor, GB_RefPicker, GB_domToTemplate, GB_makeChip });
