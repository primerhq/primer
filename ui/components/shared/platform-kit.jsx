/* global React, Modal */
// The platform kit (PK_*): the page and form primitives every revamped Platform page and overlay is built from, so the pages share one
// header, one card, one grid and one overlay (the master plan's kit contract; docs/dev/subsystems/ui-foundation.md, "Platform kit"). Loaded
// right after shared/form-field.jsx and before every page component (ui/index.html); every export is on window. The components hold only
// their own small UI state (a menu that is open, a fold that is unfolded): a page passes its data in and reads its events back.
//
// tests/ui/test_platform_kit_page.py and tests/ui/test_platform_kit_form.py run this file in MiniRacer.

// ---- shared helpers ---------------------------------------------------------------------------------------------------------------------------------

// A click that a control handles itself stops here: the card's "⋯" menu sits over the card's stretched Open button, and a click on the menu,
// one of its items or an actionable status must never reach anything under it.
function PK_stop(ev) {
  if (ev && typeof ev.stopPropagation === "function") ev.stopPropagation();
}

function PK_focus(ref) {
  var node = ref && ref.current;
  if (node && typeof node.focus === "function") node.focus();
}

// Focus moved to an element (relatedTarget) outside the container that lost it: a keyboard user tabbed out of a menu, which closes it. A null
// relatedTarget (a click on nothing focusable) is not a move: the outside-click listener decides that one.
function PK_focusLeft(ev) {
  var next = ev && ev.relatedTarget;
  var box = ev && ev.currentTarget;
  return !!next && !(box && typeof box.contains === "function" && box.contains(next));
}

// Arrow keys, Home and End move the focus between the items of an open menu (the WAI-ARIA menu pattern); Tab leaves it.
function PK_menuKeys(ev) {
  var box = ev.currentTarget;
  var moves = { ArrowDown: 1, ArrowUp: -1, Home: "first", End: "last" };
  if (!(ev.key in moves) || !box || typeof box.querySelectorAll !== "function") return;
  var items = Array.prototype.slice.call(box.querySelectorAll('[role="menuitem"], [role="menuitemradio"]'));
  if (!items.length) return;
  ev.preventDefault();
  var at = items.indexOf(document.activeElement);
  var move = moves[ev.key];
  var next = move === "first" ? 0 : move === "last" ? items.length - 1 : (at + move + items.length) % items.length;
  items[next].focus();
}

// The first item of a menu that just opened takes the focus (the WAI-ARIA menu button pattern), so a keyboard user lands in it.
function PK_focusFirstItem(ref) {
  var box = ref && ref.current;
  var first = box && typeof box.querySelector === "function" ? box.querySelector('[role="menuitem"], [role="menuitemradio"]') : null;
  if (first && typeof first.focus === "function") first.focus();
}

// An open menu closes on a click anywhere outside it. The listener is on the document in the CAPTURE phase, so a click that a control stops
// (another card's "⋯" button, an actionable status) still closes this menu; a click inside the menu's own box is left to the menu.
function PK_useOutsideClick(open, boxRef, close) {
  React.useEffect(function () {
    if (!open) return undefined;
    function onClick(ev) {
      var box = boxRef.current;
      if (box && ev && ev.target && typeof box.contains === "function" && box.contains(ev.target)) return;
      close();
    }
    document.addEventListener("click", onClick, true);
    return function () { document.removeEventListener("click", onClick, true); };
  }, [open]);
}

// ---- page primitives --------------------------------------------------------------------------------------------------------------------------------

// "1-12 of 13", or "0 of 0" for an empty list: one text node, so it reads as one phrase.
function PK_pagerRange(pager) {
  var total = pager.total || 0;
  return total ? pager.from + "-" + pager.to + " of " + total : "0 of 0";
}

// PK_PageHeader: the one header of a Platform page (design 5.1). The title and its count on the left; on the right, in this order, an optional
// secondary control (extra), the filter box, the previous/next pager and the page's one compact, filled action. The right-hand controls are one
// height (calc(var(--hit) - 4px), 32px at the default density). `filter` and `pager` are optional: a detail page's header is a title and an
// action, and an omitted one is not drawn. The action's label is drawn verbatim (the "+" belongs to the label).
function PK_PageHeader(props) {
  var filter = props.filter || null;
  var pager = props.pager || null;
  var action = props.action || null;
  var page = pager ? pager.page || 0 : 0;
  var pages = pager ? Math.max(1, pager.pages || 1) : 1;
  return (
    <div className="pk-header" data-testid="pk-header">
      <h2 className="pk-title">{props.title}</h2>
      {props.count != null && props.count !== "" ? (
        <span className="pk-count" data-testid="pk-count">{props.count}</span>
      ) : null}
      <span className="pk-spacer" />
      {props.extra || null}
      {filter ? (
        <div className="pk-filter">
          <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true">
            <circle cx="5.2" cy="5.2" r="3.6" />
            <path d="M8 8 11 11" />
          </svg>
          <input value={filter.value == null ? "" : filter.value} placeholder="Filter…"
            aria-label={filter.label} data-testid={filter.testid || "pk-filter"}
            onChange={function (ev) { filter.onChange(ev.target.value); }} />
        </div>
      ) : null}
      {pager ? (
        <div className="pk-pager" role="group" aria-label="Pages" data-testid="pk-pager">
          <span className="pk-pager-range" data-testid="pk-pager-range">{PK_pagerRange(pager)}</span>
          <button type="button" className="pk-pager-btn" data-testid="pk-pager-prev" aria-label="Previous page"
            disabled={!(page > 0)} onClick={function () { pager.onPage(page - 1); }}>‹</button>
          <button type="button" className="pk-pager-btn" data-testid="pk-pager-next" aria-label="Next page"
            disabled={!(page < pages - 1)} onClick={function () { pager.onPage(page + 1); }}>›</button>
        </div>
      ) : null}
      {action ? (
        <button type="button" className="pk-action" data-testid={action.testid}
          onClick={function () { action.onClick(); }}>{action.label}</button>
      ) : null}
    </div>
  );
}

// PK_CardGrid: the full-width card grid (design 5.3): auto-fill columns of at least 240px, no width cap, the cards of a row one height.
function PK_CardGrid(props) {
  return <div className="pk-grid" data-testid={props.testid}>{props.children}</div>;
}

// The facts a card draws: null and empty entries dropped (NV_fact answers null for a missing value), then at most three.
function PK_cardFacts(facts) {
  return (facts || []).filter(function (f) { return f != null && f !== ""; }).slice(0, 3);
}

// The card's "⋯" menu (design 20 Q1): a button that opens a list of actions, Delete among them. The list is a layer of the Escape stack only
// while it is open (Escape closes it and nothing under it), it closes on a click outside it and when the focus leaves it, and a pick closes it
// before it runs the item. Every click it handles stops where it lands, so none reaches the stretched Open under it. The open list is ONE Tab
// stop (the WAI-ARIA roving tabindex): the item that has the focus carries tabindex 0 and the others -1, the arrow keys move the focus between
// them (PK_menuKeys), and Tab leaves the list instead of walking through its items.
function PK_CardMenu(props) {
  var openState = React.useState(false);
  var open = openState[0];
  var setOpen = openState[1];
  // the item that is the list's Tab stop: the first one when the list opens, then whichever item last took the focus
  var activeState = React.useState(0);
  var active = activeState[0];
  var setActive = activeState[1];
  var boxRef = React.useRef(null);
  var triggerRef = React.useRef(null);
  var listRef = React.useRef(null);
  function close() {
    setOpen(false);
    setActive(0);
  }
  window.primerApi.useEscape(function (ev) {
    if (ev && typeof ev.preventDefault === "function") ev.preventDefault();
    close();
    PK_focus(triggerRef);
  }, open);
  PK_useOutsideClick(open, boxRef, close);
  React.useEffect(function () {
    if (open) PK_focusFirstItem(listRef);
  }, [open]);
  return (
    <div className="pk-card-menu" ref={boxRef}
      onBlur={function (ev) { if (open && PK_focusLeft(ev)) close(); }}>
      <button type="button" ref={triggerRef} className="pk-card-menu-btn" data-testid={"pk-card-menu:" + props.id}
        aria-label={"More actions for " + props.title} aria-haspopup="menu" aria-expanded={open ? "true" : "false"}
        onClick={function (ev) { PK_stop(ev); if (open) close(); else setOpen(true); }}>⋯</button>
      {open ? (
        <div className="pk-card-menu-list" role="menu" ref={listRef} aria-label={"Actions for " + props.title}
          data-testid={"pk-card-menu-list:" + props.id} onClick={PK_stop} onKeyDown={PK_menuKeys}>
          {props.items.map(function (item, i) {
            return (
              <button type="button" key={item.testid || item.label} role="menuitem" className="pk-card-menu-item"
                tabIndex={i === active ? 0 : -1} onFocus={function () { setActive(i); }}
                data-testid={item.testid} data-danger={item.danger ? "true" : undefined}
                onClick={function (ev) {
                  PK_stop(ev);
                  close();
                  PK_focus(triggerRef);
                  item.onClick();
                }}>{item.label}</button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

// PK_Card: the one card of a Platform page (style C, design 5.2): the type's icon top left; the badge, the status and the "⋯" menu top right;
// then the title, the type line and one to three facts, left-aligned; Open at the bottom. The card div has no click of its own: the Open button
// is stretched over the whole card (.pk-card-open::after), so a click anywhere on it opens it and a keyboard user reaches it by Tab, and the
// menu and an actionable status sit above the stretch. A status with onClick is a button named by its label (seam I-8); without one it is text.
// A status's `title`, when it has one, is its tooltip (why a toolset is unreachable).
// `label`, when set, is the name the card's controls use instead of the title ("Open <label>", "More actions for <label>"), for a card whose title
// alone is ambiguous: two toolsets may each have a tool titled "search", and each card is labelled by its scoped id (audit G1-21).
// A fact is a string (one line) or a [key, value] pair. The title is cut with an ellipsis when it is long, and the stretched Open covers it, so
// the card's full name rides on the Open button as its `title` (a tooltip over the whole card).
function PK_Card(props) {
  var id = props.id;
  var icon = props.icon || null;
  var status = props.status || null;
  var facts = PK_cardFacts(props.facts);
  var menu = props.menu && props.menu.length ? props.menu : null;
  var statusId = (status && status.testid) || "pk-card-status:" + id;
  var name = props.label || props.title;
  return (
    <div className="pk-card" data-testid={"pk-card:" + id}>
      <div className="pk-card-top">
        {icon && icon.path ? (
          <span className="pk-card-icon" aria-hidden="true" style={icon.tone ? { color: icon.tone } : undefined}>
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round">
              <path d={icon.path} />
            </svg>
          </span>
        ) : null}
        <span className="pk-card-gap" />
        {props.badge ? <span className="pk-card-badge" data-testid={"pk-card-badge:" + id}>{props.badge}</span> : null}
        {status && typeof status.onClick === "function" ? (
          <button type="button" className="pk-card-status pk-card-status-btn" data-tone={status.tone} data-testid={statusId}
            title={status.title || undefined} onClick={function (ev) { PK_stop(ev); status.onClick(); }}>{status.label}</button>
        ) : status ? (
          <span className="pk-card-status" data-tone={status.tone} data-testid={statusId} title={status.title || undefined}>{status.label}</span>
        ) : null}
        {menu ? <PK_CardMenu id={id} title={name} items={menu} /> : null}
      </div>
      <div className="pk-card-title" title={props.title}>{props.title}</div>
      {props.subtitle ? <div className="pk-card-sub">{props.subtitle}</div> : null}
      {facts.length ? (
        <div className="pk-card-facts">
          {facts.map(function (f, i) {
            if (!Array.isArray(f)) return <div key={i} className="pk-fact pk-fact-line" title={String(f)}>{String(f)}</div>;
            return (
              <div key={i} className="pk-fact">
                <span className="pk-fact-k">{f[0]}</span>
                <span className="pk-fact-v" title={String(f[1])}>{f[1]}</span>
              </div>
            );
          })}
        </div>
      ) : null}
      <div className="pk-card-foot">
        <button type="button" className="pk-card-open" data-testid={"pk-card-open:" + id} aria-label={"Open " + name}
          title={props.title} onClick={function () { props.onOpen(); }}>Open</button>
      </div>
    </div>
  );
}

// PK_usePagedFilter: one filter and one pager over a list. `total` is the number of rows the filter keeps; `setQ` sets the query and sends the
// page back to the first in the same call (a filter typed on page 3 lands on page 1); the page is clamped into range when it is read, so a list
// that shrinks under the current page shows its last page, never an empty one; `setPage` clamps too. A row matches when text(row) contains the
// trimmed query, case-insensitively.
function PK_usePagedFilter(items, opts) {
  var size = Math.max(1, Math.floor((opts && opts.pageSize) || 12));
  var text = (opts && opts.text) || function (row) { return String(row); };
  var qState = React.useState("");
  var pageState = React.useState(0);
  var q = qState[0];
  var needle = q.trim().toLowerCase();
  var rows = items || [];
  var matched = needle ? rows.filter(function (row) {
    return String(text(row) == null ? "" : text(row)).toLowerCase().indexOf(needle) >= 0;
  }) : rows;
  var total = matched.length;
  var pages = Math.max(1, Math.ceil(total / size));
  var page = Math.min(Math.max(0, pageState[0]), pages - 1);
  return {
    q: q,
    setQ: function (value) {
      qState[1](value == null ? "" : String(value));
      pageState[1](0);
    },
    page: page,
    setPage: function (n) {
      var want = Math.floor(Number(n)) || 0;
      pageState[1](Math.min(Math.max(0, want), pages - 1));
    },
    pages: pages,
    from: total ? page * size + 1 : 0,
    to: Math.min((page + 1) * size, total),
    total: total,
    visible: matched.slice(page * size, (page + 1) * size),
  };
}

// ---- form primitives --------------------------------------------------------------------------------------------------------------------------------
// The alignment rules of design 6.2: every label on one line, help and errors under their control, short fields in a two- or three-column grid
// whose controls share a top edge and a height (34px), related fields in a bordered box with a small-caps title, Cancel on the left and the
// primary action on the right.

// The type selector at the centre top of a form overlay, larger than the other controls (design 6.1): "Register a", then a button holding the
// current type's icon and label and a caret that opens the list of types. A pick reports the new value and closes the list; Escape closes only
// the list. A locked selector (edit mode) shows its type and opens nothing.
function PK_Selector(props) {
  var sel = props.selector;
  var openState = React.useState(false);
  var open = openState[0] && !sel.locked;
  var setOpen = openState[1];
  var boxRef = React.useRef(null);
  var triggerRef = React.useRef(null);
  var listRef = React.useRef(null);
  window.primerApi.useEscape(function (ev) {
    if (ev && typeof ev.preventDefault === "function") ev.preventDefault();
    setOpen(false);
    PK_focus(triggerRef);
  }, open);
  PK_useOutsideClick(open, boxRef, function () { setOpen(false); });
  React.useEffect(function () {
    if (open) PK_focusFirstItem(listRef);
  }, [open]);
  var options = sel.options || [];
  var current = options.filter(function (o) { return o.value === sel.value; })[0] || { value: sel.value, label: String(sel.value || "") };
  return (
    <div className="pk-selector">
      {sel.prefix ? <div className="pk-selector-prefix">{sel.prefix}</div> : null}
      <span className="pk-selector-anchor" ref={boxRef}
        onBlur={function (ev) { if (open && PK_focusLeft(ev)) setOpen(false); }}>
        <button type="button" ref={triggerRef} className="pk-selector-btn" data-testid={sel.testid}
          aria-haspopup="menu" aria-expanded={open ? "true" : "false"} aria-disabled={sel.locked ? "true" : undefined}
          onClick={function (ev) { PK_stop(ev); if (!sel.locked) setOpen(!open); }}>
          {current.iconPath ? (
            <svg width="16" height="16" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" aria-hidden="true">
              <path d={current.iconPath} />
            </svg>
          ) : null}
          <span className="pk-selector-label">{current.label}</span>
          {sel.locked ? null : <span className="pk-selector-caret" aria-hidden="true">▾</span>}
        </button>
        {open ? (
          <div className="pk-selector-menu" role="menu" ref={listRef} data-testid={sel.testid + ":menu"}
            onClick={PK_stop} onKeyDown={PK_menuKeys}>
            {options.map(function (o) {
              var on = o.value === sel.value;
              return (
                <button type="button" key={o.value} role="menuitemradio" aria-checked={on ? "true" : "false"} className="pk-selector-item"
                  data-testid={sel.testid + ":" + o.value}
                  onClick={function (ev) {
                    PK_stop(ev);
                    setOpen(false);
                    PK_focus(triggerRef);
                    sel.onChange(o.value);
                  }}>
                  {o.iconPath ? (
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" aria-hidden="true">
                      <path d={o.iconPath} />
                    </svg>
                  ) : <span className="pk-selector-noicon" aria-hidden="true" />}
                  <span>{o.label}</span>
                </button>
              );
            })}
          </div>
        ) : null}
      </span>
    </div>
  );
}

// PK_FormOverlay: the one overlay shell of the revamped forms. It is a shared Modal (the Escape stack, the focus trap, the scrim that closes it,
// and the bottom sheet on a phone) with the kit's class and the given test id, 720px wide unless `width` says otherwise, the title centred, the
// optional type selector at the top of the body, and a footer of two sides: `left` (Cancel) and `right` (the primary action). Neither the dialog
// nor any kit overlay rule uses a transform, so a position: fixed child (a full-screen editor) covers the viewport, not the dialog.
function PK_FormOverlay(props) {
  var footer = props.footer || {};
  return (
    <Modal title={props.title} onClose={props.onClose} width={props.width || 720} className="pk-overlay" testid={props.testid}
      footer={
        <div className="pk-foot">
          <div className="pk-foot-left">{footer.left || null}</div>
          <div className="pk-foot-right">{footer.right || null}</div>
        </div>
      }>
      {props.selector ? <PK_Selector selector={props.selector} /> : null}
      {props.children}
    </Modal>
  );
}

// PK_Grid: the form grid. One, two (the default) or three equal columns; the controls of a row share their top edge; one column below 520px.
function PK_Grid(props) {
  var cols = props.cols === 1 || props.cols === 3 ? props.cols : 2;
  return <div className="pk-form-grid" data-cols={String(cols)}>{props.children}</div>;
}

// PK_Field: one labelled form row, the shared FormField with the kit's classes. The label is one line; `help` and `error` sit UNDER the control
// (the error as FormField's role="alert"), and the control is described by them. It never passes FormField's inline `hint`. `span: "full"` takes
// the whole grid row. `required` draws an asterisk that assistive tech does not read: the control says it is required itself (`required` or
// `aria-required`), because the children pass through untouched apart from FormField's id and description.
function PK_Field(props) {
  var Field = window.FormField;
  var label = props.required ? (
    <React.Fragment>
      {props.label}
      <span className="pk-required" aria-hidden="true">{" *"}</span>
    </React.Fragment>
  ) : props.label;
  return (
    <Field className={"field pk-field" + (props.span === "full" ? " pk-span-full" : "")} labelClassName="field-label pk-label"
      label={label} help={props.help} err={props.error}>
      {props.children}
    </Field>
  );
}

// PK_Section: related fields in a bordered box with a small-caps title ("LIMITS AND RETRIES"), a group named by that title; the fields sit in a
// PK_Grid of `cols` columns.
function PK_Section(props) {
  var titleId = React.useId() + "title";
  return (
    <div className="pk-section" role="group" aria-labelledby={titleId} data-testid={props.testid}>
      <div className="pk-section-title" id={titleId}>{props.title}</div>
      <PK_Grid cols={props.cols}>{props.children}</PK_Grid>
    </div>
  );
}

// PK_Fold: the "▸ More ..." disclosure. `open` is the INITIAL state only; `onToggle(open)` reports each change. The folded children are not
// mounted. Inside a grid the fold takes a whole row.
function PK_Fold(props) {
  var openState = React.useState(!!props.open);
  var open = openState[0];
  function toggle() {
    var next = !open;
    openState[1](next);
    if (props.onToggle) props.onToggle(next);
  }
  return (
    <div className="pk-fold" data-testid={props.testid}>
      <button type="button" className="pk-fold-toggle" data-testid={props.testid + ":toggle"} aria-expanded={open ? "true" : "false"}
        onClick={toggle}>
        <span className="pk-fold-caret" aria-hidden="true">{open ? "▾" : "▸"}</span>
        {" " + props.label}
      </button>
      {open ? <div className="pk-fold-body">{props.children}</div> : null}
    </div>
  );
}

// PK_Segmented: a small set of exclusive choices ("Sequential fallback | Round robin"), a group of toggle buttons; the current one is pressed.
function PK_Segmented(props) {
  return (
    <div className="pk-seg" role="group" aria-label={props.ariaLabel} data-testid={props.testid}>
      {(props.options || []).map(function (o) {
        var on = o.value === props.value;
        return (
          <button type="button" key={o.value} className="pk-seg-btn" data-testid={props.testid + ":" + o.value}
            aria-pressed={on ? "true" : "false"} onClick={function () { if (!on) props.onChange(o.value); }}>{o.label}</button>
        );
      })}
    </div>
  );
}

// PK_RowTable: an editable list of rows (models, environment, files, node selector, tolerations). "+ Add" appends a row with "" in each column
// (a select column starts on its first option); "✕" removes a row; every edit hands the parent the next rows. Each control is named by its
// column and row number ("Model 1"); a secret column is a password input the browser does not fill in.
function PK_RowTable(props) {
  var columns = props.columns || [];
  var rows = props.rows || [];
  var template = columns.map(function (c) { return c.width || "minmax(0, 1fr)"; }).concat(["auto"]).join(" ");
  function blank() {
    var row = {};
    columns.forEach(function (c) {
      row[c.key] = c.input === "select" && c.options && c.options.length ? c.options[0] : "";
    });
    return row;
  }
  function edit(i, key, value) {
    props.onChange(rows.map(function (row, j) {
      if (j !== i) return row;
      var next = Object.assign({}, row);
      next[key] = value;
      return next;
    }));
  }
  return (
    <div className="pk-rowtable" data-testid={props.testid}>
      <div className="pk-rowtable-head">
        <span className="pk-rowtable-title">{props.title || ""}</span>
        <button type="button" className="pk-rowtable-add" data-testid={props.testid + ":add"}
          onClick={function () { props.onChange(rows.concat([blank()])); }}>{props.addLabel}</button>
      </div>
      {rows.length && columns.length > 1 ? (
        <div className="pk-rowtable-row pk-rowtable-cols" style={{ gridTemplateColumns: template }} aria-hidden="true">
          {columns.map(function (c) { return <span key={c.key}>{c.label}</span>; })}
          <span />
        </div>
      ) : null}
      {rows.map(function (row, i) {
        return (
          <div key={i} className="pk-rowtable-row" style={{ gridTemplateColumns: template }}>
            {columns.map(function (c) {
              var testid = props.testid + ":" + i + ":" + c.key;
              var name = c.label + " " + (i + 1);
              var value = row[c.key] == null ? "" : String(row[c.key]);
              function onChange(ev) { edit(i, c.key, ev.target.value); }
              if (c.input === "select") {
                return (
                  <select key={c.key} className="pk-rowtable-input" data-testid={testid} aria-label={name} value={value} onChange={onChange}>
                    {(c.options || []).map(function (o) { return <option key={o} value={o}>{o}</option>; })}
                  </select>
                );
              }
              var type = c.input === "secret" ? "password" : c.input === "number" ? "number" : "text";
              return (
                <input key={c.key} className="pk-rowtable-input" data-testid={testid} aria-label={name} type={type}
                  autoComplete={c.input === "secret" ? "off" : undefined} value={value} onChange={onChange} />
              );
            })}
            <button type="button" className="pk-rowtable-remove" data-testid={props.testid + ":remove:" + i}
              aria-label={"Remove row " + (i + 1)}
              onClick={function () { props.onChange(rows.filter(function (_row, j) { return j !== i; })); }}>✕</button>
          </div>
        );
      })}
    </div>
  );
}

// PK_CheckTable: a list to tick (the import checklist of design 6.4, the members of an aggregated profile of design 7.2). A checkbox per row,
// named by the row's label; the header's "Select all" is ticked when every row that can be ticked is, and `onToggleAll(checked, keys)` names the
// rows it applies to. A `disabled` row (already imported, say) cannot be toggled, is left out of toggle-all and is drawn muted. A row's `extra`
// sits in its last column, under `extraLabel`. The body scrolls past 320px.
function PK_CheckTable(props) {
  var rows = props.rows || [];
  var uid = React.useId();
  var live = rows.filter(function (r) { return !r.disabled; });
  var liveKeys = live.map(function (r) { return r.key; });
  var allChecked = live.length > 0 && live.every(function (r) { return !!r.checked; });
  var hasExtra = !!props.extraLabel || rows.some(function (r) { return r.extra != null; });
  var cls = "pk-checktable" + (hasExtra ? " pk-checktable-has-extra" : "");
  return (
    <div className={cls} data-testid={props.testid}>
      <div className="pk-checktable-row pk-checktable-head">
        <input type="checkbox" id={uid + "all"} data-testid={props.testid + ":all"} aria-label="Select all"
          checked={allChecked} disabled={!live.length}
          onChange={function () { if (live.length) props.onToggleAll(!allChecked, liveKeys); }} />
        <label className="pk-checktable-label" htmlFor={uid + "all"}>Select all</label>
        {hasExtra ? <span className="pk-checktable-extra">{props.extraLabel || ""}</span> : null}
      </div>
      <div className="pk-checktable-body">
        {rows.map(function (r, i) {
          var id = uid + "row" + i;
          return (
            <div key={r.key} className="pk-checktable-row" data-disabled={r.disabled ? "true" : undefined}>
              <input type="checkbox" id={id} data-testid={props.testid + ":" + r.key} aria-label={r.label}
                checked={!!r.checked} disabled={!!r.disabled}
                onChange={function () { if (!r.disabled) props.onToggle(r.key); }} />
              <label className="pk-checktable-label" htmlFor={id}>{r.label}</label>
              {hasExtra ? <div className="pk-checktable-extra">{r.extra != null ? r.extra : null}</div> : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

Object.assign(window, {
  PK_PageHeader, PK_CardGrid, PK_Card, PK_usePagedFilter,
  PK_FormOverlay, PK_Grid, PK_Field, PK_Section, PK_Fold, PK_Segmented, PK_RowTable, PK_CheckTable,
});
