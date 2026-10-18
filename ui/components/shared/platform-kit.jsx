/* global React */
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
// before it runs the item. Every click it handles stops where it lands, so none reaches the stretched Open under it.
function PK_CardMenu(props) {
  var openState = React.useState(false);
  var open = openState[0];
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
  return (
    <div className="pk-card-menu" ref={boxRef}
      onBlur={function (ev) { if (open && PK_focusLeft(ev)) setOpen(false); }}>
      <button type="button" ref={triggerRef} className="pk-card-menu-btn" data-testid={"pk-card-menu:" + props.id}
        aria-label={"More actions for " + props.title} aria-haspopup="menu" aria-expanded={open ? "true" : "false"}
        onClick={function (ev) { PK_stop(ev); setOpen(!open); }}>⋯</button>
      {open ? (
        <div className="pk-card-menu-list" role="menu" ref={listRef} aria-label={"Actions for " + props.title}
          data-testid={"pk-card-menu-list:" + props.id} onClick={PK_stop} onKeyDown={PK_menuKeys}>
          {props.items.map(function (item) {
            return (
              <button type="button" key={item.testid || item.label} role="menuitem" className="pk-card-menu-item"
                data-testid={item.testid} data-danger={item.danger ? "true" : undefined}
                onClick={function (ev) {
                  PK_stop(ev);
                  setOpen(false);
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
// A fact is a string (one line) or a [key, value] pair.
function PK_Card(props) {
  var id = props.id;
  var icon = props.icon || null;
  var status = props.status || null;
  var facts = PK_cardFacts(props.facts);
  var menu = props.menu && props.menu.length ? props.menu : null;
  var statusId = (status && status.testid) || "pk-card-status:" + id;
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
            onClick={function (ev) { PK_stop(ev); status.onClick(); }}>{status.label}</button>
        ) : status ? (
          <span className="pk-card-status" data-tone={status.tone} data-testid={statusId}>{status.label}</span>
        ) : null}
        {menu ? <PK_CardMenu id={id} title={props.title} items={menu} /> : null}
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
        <button type="button" className="pk-card-open" data-testid={"pk-card-open:" + id} aria-label={"Open " + props.title}
          onClick={function () { props.onOpen(); }}>Open</button>
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

Object.assign(window, { PK_PageHeader, PK_CardGrid, PK_Card, PK_usePagedFilter });
