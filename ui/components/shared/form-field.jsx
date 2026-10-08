/* global React, Icon */

// FormField: the ONE labelled form row of the console (console review C-003). A form that draws its own `<label className="field-label">` next to an input leaves the label a
// SIBLING of the control: input.labels is empty, clicking the visible text focuses nothing, and a screen reader lands on an unnamed edit field. Every form row draws a FormField
// instead (WS_FieldRow in workspaces/shared.jsx and FieldRow in semantic-search.jsx are one-line wrappers of it), and scripts/audit_field_labels.py counts the labels that still
// name nothing so the count only goes down (tests/ui/test_field_label_ratchet.py).
//
// The first native control among the row's direct children (input, select, textarea) gets an id (its own when it has one) that the label points at, and an error is tied to it
// (aria-invalid, aria-describedby, role="alert"), and so is a `help` line (a muted sentence under the control: aria-describedby, before the error). `className`, `labelClassName`
// and `hintClassName` let a surface with its own look (the console overlays' nv-field) keep it. A row whose control is a custom component or sits inside a wrapper cannot be labelled by id: the row becomes a group named by its
// label instead (found is false).
function FF_fieldControls(children, ids, hasErr, hasHelp) {
  var kids = React.Children.toArray(children);
  var firstIndex = -1;
  for (var i = 0; i < kids.length; i++) {
    var t = React.isValidElement(kids[i]) ? kids[i].type : null;
    if (t === "input" || t === "select" || t === "textarea") { firstIndex = i; break; }
  }
  if (firstIndex < 0) return { children: kids, found: false, controlId: null };
  var control = kids[firstIndex];
  var controlId = control.props.id || ids.control;
  var extra = { id: controlId };
  if (hasErr) extra["aria-invalid"] = "true";
  var describedBy = [control.props["aria-describedby"], hasHelp ? ids.help : null, hasErr ? ids.err : null].filter(Boolean).join(" ");
  if (describedBy) extra["aria-describedby"] = describedBy;
  kids[firstIndex] = React.cloneElement(control, extra);
  return { children: kids, found: true, controlId: controlId };
}

function FormField({ label, hint, help, err, className, labelClassName, hintClassName, children }) {
  var uid = React.useId();
  var ids = { label: uid + "l", control: uid + "c", err: uid + "e", help: uid + "h" };
  var wired = FF_fieldControls(children, ids, !!err, !!help);
  return (
    <div className={className || "field"} role={wired.found ? undefined : "group"} aria-labelledby={wired.found ? undefined : ids.label}
      aria-describedby={wired.found || !help ? undefined : ids.help}>
      <label className={labelClassName || "field-label"} id={ids.label} htmlFor={wired.found ? wired.controlId : undefined}>
        {label}
        {hint && <>{" "}<span className={hintClassName || "hint"}>{hint}</span></>}
      </label>
      {wired.children}
      {help && <div className="field-help" id={ids.help}>{help}</div>}
      {err && <div className="field-help" id={ids.err} role="alert" style={{ color: "var(--red)" }}>
        <Icon name="x-circle" size={11} style={{ verticalAlign: -1, marginRight: 3 }} />
        {err}
      </div>}
    </div>
  );
}

window.FormField = FormField;
