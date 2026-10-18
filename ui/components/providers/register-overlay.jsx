/* global React, window, Btn */
// register-overlay.jsx - window.PC_RegisterOverlay, the one Register provider overlay (plan 02 Task 11; seam I-3 of the master plan; design 6).
//
// One overlay registers and edits a provider of every class. It is the kit's PK_FormOverlay: the title ("Register provider", or "Edit <id>"), the type
// selector at the centre top ("Register a [icon] <Type> provider", the classes in the sidebar order; Web Fetch only when a caller allows it), the page of
// the chosen class below it, and the page's footer: Cancel on the left, the action on the right. A page renders INTO the overlay: it is handed a `frame`
// function and returns frame({ body, footerLeft, footerRight, error, busy }), so each page owns its own state and footer while the overlay owns the shell, the
// error line, Invalidate and Delete. Changing the type remounts the PAGE (`key`), so values typed for the previous type are dropped (design 6.1); the SHELL is
// not the page's, so the dialog, its scrim, its focus trap and the selector stay mounted (see "The shell's slots" below).
//
// Which page a class gets:
// - a page registered in window.PC_REGISTER_PAGES (the wizard classes: LLM in Task 12, Embedding and Cross Encoding in Task 14, TTS and ASR in Task 15);
// - otherwise a panel class gets its own create form (PC_PANEL_FORMS), handed { existing, frame, onSaved, onClose, pushToast };
// - otherwise the schema-driven PC_ProviderForm in its framed layout (the form classes, and a wizard class whose own page is not loaded).
//
// The parts a page hands to frame: `body` (the page's content), `footerLeft` (Cancel) and `footerRight` (the action; Test and the submit) for the two sides of
// the footer, `error` (a refusal, shown as the body's last line) and `busy` (true while a request of the page's own, a save or a test, is out: the overlay keeps
// the type selector shut, and holds Delete off, until it is answered, because a switch then would unmount the page and swallow the answer). The page is handed the same
// word the other way round: `blocked` is true while the overlay's own request, a Delete, is out, and the page holds its actions off meanwhile (`<Btn blocked>`: aria-disabled,
// a refused click, no aria-busy), so a Save or a Test cannot run beside a Delete of the same row.
//
// Seam I-3: PC_RegisterOverlay({ initialClass, allowedClasses?, excludeKinds?, editRow?, onClose(), onSaved(row), onDeleted?(id) }). `excludeKinds` is
// added to the built-in kinds the Type select leaves out; without `onDeleted` the overlay closes itself after a delete. In edit mode a class that has a
// draft Test (`draftTest`) still offers none: the draft holds the served MASK of every secret and a `_test` route builds its adapter from the draft alone.

// The wizard classes' pages, merged in by the files that define them (each does Object.assign(window.PC_REGISTER_PAGES || {}, { <key>: Page })). A page
// takes { klass, editRow, excludeKinds, frame, blocked, onSaved(row), onClose() } and returns frame({ body, footerLeft?, footerRight, error?, busy? }).
window.PC_REGISTER_PAGES = Object.assign(window.PC_REGISTER_PAGES || {}, {});

// The panel classes' own create forms, read when they are needed (their files load after this one). Each takes { existing, frame, blocked, onSaved, onClose,
// pushToast } (Tasks 17 to 19 give them `frame`, `blocked` and `onSaved`).
const PC_PANEL_FORMS = {
  ssp: () => window.SSPCreateModal,
  workspace: () => window.WorkspaceProviderCreateModal,
  channel: () => window.NewChannelProviderModal,
};

// The class the overlay opens on: an edit opens on its row's class; a caller's allowed classes are honoured (the first of them when the asked-for class
// is not among them); otherwise a Providers class, and LLM when opened from All, from a hidden class or from nothing (design 6.1).
function PC_startClass(initialClass, allowedClasses, editRow) {
  if (editRow) return initialClass;
  if (Array.isArray(allowedClasses) && allowedClasses.length) {
    return allowedClasses.indexOf(initialClass) >= 0 ? initialClass : allowedClasses[0];
  }
  const klass = window.PC_classByKey(initialClass);
  return klass && klass.nav ? initialClass : "llm";
}

function PC_selectorOption(klass) {
  return { value: klass.key, label: klass.noun + " provider", iconPath: klass.iconPath };
}

// The selector's options: the Providers classes in the sidebar order, or exactly the classes a caller allows (in that same order).
function PC_selectorOptions(allowedClasses) {
  const allowed = Array.isArray(allowedClasses) && allowedClasses.length ? allowedClasses : null;
  return window.PC_CLASSES
    .filter((c) => (allowed ? allowed.indexOf(c.key) >= 0 : c.nav))
    .map(PC_selectorOption);
}

// A request to open the overlay (the command palette's "Register Provider" verb, Task 20) may come before a page that can open it has mounted: it waits
// until a mounted page takes it, and is taken once. A page takes requests with PC_useRegisterRequest(handler(classKey)); the most recently mounted one
// takes a request made while it is mounted.
const PC_REGISTER_REQUESTS = { pending: null, takers: [] };

function PC_requestRegister(classKey) {
  const req = { classKey: classKey == null ? null : classKey };
  const takers = PC_REGISTER_REQUESTS.takers;
  if (takers.length) {
    takers[takers.length - 1](req.classKey);
    return;
  }
  PC_REGISTER_REQUESTS.pending = req;
}

function PC_useRegisterRequest(handler) {
  const handlerRef = React.useRef(handler);
  handlerRef.current = handler;
  React.useEffect(() => {
    const take = (classKey) => {
      if (typeof handlerRef.current === "function") handlerRef.current(classKey);
    };
    PC_REGISTER_REQUESTS.takers.push(take);
    const pending = PC_REGISTER_REQUESTS.pending;
    if (pending) {
      PC_REGISTER_REQUESTS.pending = null;
      take(pending.classKey);
    }
    return () => {
      const at = PC_REGISTER_REQUESTS.takers.indexOf(take);
      if (at >= 0) PC_REGISTER_REQUESTS.takers.splice(at, 1);
    };
  }, []);
}

// ---- the shell's slots --------------------------------------------------------------------------------------------------------------------------------
//
// The overlay owns ONE shell, the PK_FormOverlay (a Modal), for as long as it is open. A page is rendered BESIDE it, not inside it. The page is keyed by its class, so that a
// pick drops its state (design 6.1); a shell that sat in the page's markup would be dropped with it: a new dialog and scrim node, the open animation played again (fadeIn, and
// sheet-slide-up on a phone), the focus on Close instead of the selector, and a new focus trap that takes the selector it has just destroyed as the element to return focus to,
// so that Cancel and Escape left the focus on <body> (review of #773, measured in Chromium). It would also drop the page's state when a phone is turned across the 640px band
// and the Modal swaps between its dialog and sheet trees.
//
// What a page draws reaches the dialog through slots. frame(parts) returns a PC_FramePublisher, which writes the parts into a small store; the three PC_FrameSlot components in
// the shell (the body, the left and the right of the footer) draw them, so the markup inside the dialog is what it always was. A slot draws only the parts of ITS class: for
// the length of a pick the store still holds the old page's parts, and a page that never publishes must leave the shell empty, not showing a page that is gone. Each slot keys
// what it draws by the class, so the page's components (and their own state) are replaced with the page.
//
// The page comes BEFORE the shell in the overlay's markup, so on any render of both the publisher writes before the slots read and they show the page's parts in the same
// pass. A page that re-renders ALONE (its own state changed) is not rendered with the shell: its layout effect then refreshes the store, and a slot that is showing something
// older renders again, before the browser paints.
function PC_makeFrameStore() {
  const store = { parts: null, slots: [] };
  store.attach = (slot) => {
    store.slots.push(slot);
    return () => {
      const at = store.slots.indexOf(slot);
      if (at >= 0) store.slots.splice(at, 1);
    };
  };
  store.refresh = () => {
    store.slots.slice().forEach((slot) => slot.refresh());
  };
  return store;
}

function PC_FrameSlot({ store, part, classKey }) {
  const [, again] = React.useReducer((n) => n + 1, 0);
  const shown = React.useRef(null);
  const parts = store.parts;
  shown.current = parts;
  React.useLayoutEffect(() => {
    const slot = { refresh: () => { if (shown.current !== store.parts) again(); } };
    const detach = store.attach(slot);
    slot.refresh();
    return detach;
  }, [store]);
  if (!parts || parts.classKey !== classKey) return null;
  return [<React.Fragment key={parts.classKey}>{parts[part]}</React.Fragment>];
}

// What frame() returns: it draws nothing itself. `onBusy(busy)` tells the overlay whether the page has a request out. The parts it already holds are kept while nothing it
// was handed has changed, so a slot that shows them is not asked to render again. When the publisher unmounts (the page stops calling frame(), or the page is replaced by the next
// type's) it takes back what it published: the parts, if they are still ITS parts (on a pick the new page's publisher has already written by the time the old one's cleanup runs, and
// those are not the old one's to take), and the page's `busy`; the slots then draw nothing, so a handler of a page that has stopped asking to be drawn cannot be pressed.
function PC_FramePublisher({ store, classKey, body, left, right, busy, onBusy }) {
  const held = store.parts;
  if (!held || held.classKey !== classKey || held.body !== body || held.left !== left || held.right !== right) {
    store.parts = { classKey, body, left, right };
  }
  const mine = React.useRef(null);
  mine.current = store.parts;
  const onBusyRef = React.useRef(onBusy);
  onBusyRef.current = onBusy;
  React.useLayoutEffect(() => {
    store.refresh();
    onBusy(busy);
  });
  React.useLayoutEffect(() => () => {
    if (store.parts === mine.current) {
      store.parts = null;
      store.refresh();
    }
    onBusyRef.current(false);
  }, [store]);
  return null;
}

// The page of a form class (and of a wizard class whose own page is not loaded): the schema-driven form in its framed layout. An edit starts from the full
// served row (PC_draftFromRow), so every field the form does not show is sent back unchanged; the body goes out with the wire names (PC_wireBody). A
// built-in row whose kind has nothing to configure is shown read-only.
function PC_RegisterFormPage({ klass, editRow, excludeKinds, frame, blocked, onSaved, onClose }) {
  const editing = !!editRow;
  const [draft, setDraft] = React.useState(() => (editing ? window.PC_draftFromRow(klass.plural, editRow) : {}));
  const types = window.PC_useTypes(klass.plural);
  const shape = types.types && draft.provider ? types.types[draft.provider] : null;
  const nothingToConfigure = !!shape && !(shape.config_fields || []).length && !(shape.row_fields || []).length && !shape.limits;
  const readOnly = editing && window.PC_isBuiltin(klass.key, editRow.id) && nothingToConfigure;
  const submit = async (body) => {
    const { apiFetch } = window.primerApi;
    const wire = window.PC_wireBody(klass.plural, body);
    const row = editing
      ? await apiFetch("PUT", `/${klass.plural}/${encodeURIComponent(editRow.id)}`, wire)
      : await apiFetch("POST", `/${klass.plural}`, wire);
    onSaved(row && typeof row === "object" ? row : wire);
  };
  // Test posts the DRAFT to `/{plural}/_test`, and the route builds its adapter from the draft alone (it never reads the stored row). An edit's draft is the served row,
  // whose secrets are the MASK: Test would report a failure for a provider that works. So an edit has no draft Test, whatever the class offers (plan 02: "No masked
  // secret reaches a probe. Edit mode never sends a draft to `_discover_models`, `_test` or the Hub check").
  return (
    <window.PC_ProviderForm plural={klass.plural} value={draft} onChange={setDraft} onSubmit={submit} onCancel={onClose}
      editing={editing} existingId={editing ? editRow.id : null} frame={frame} classKey={klass.key} kindSelect
      excludeKinds={excludeKinds} canTest={!!klass.draftTest && !editing} submitLabel={editing ? "Save" : "Register"} readOnly={readOnly} blocked={blocked} />
  );
}

// A class whose page cannot be shown (a class this console does not know, a panel form that is not loaded): the shell with a line saying so and Cancel.
function PC_RegisterNotice({ frame, onClose, children }) {
  return frame({
    body: <div className="nv-form-error" role="alert">{children}</div>,
    footerLeft: <Btn kind="ghost" data-testid="pc-register-cancel" onClick={onClose}>Cancel</Btn>,
  });
}

function PC_RegisterOverlay({ initialClass, allowedClasses, excludeKinds, editRow, onClose, onSaved, onDeleted }) {
  const api = window.primerApi;
  const PK_FormOverlay = window.PK_FormOverlay;
  const editing = !!editRow;
  const [classKey, setClassKey] = React.useState(() => PC_startClass(initialClass, allowedClasses, editRow));
  const [deleting, setDeleting] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState(null);
  const [pageBusy, setPageBusy] = React.useState(false);
  const wasBusy = React.useRef(false);
  const storeRef = React.useRef(null);
  if (!storeRef.current) storeRef.current = PC_makeFrameStore();
  const store = storeRef.current;
  const live = React.useRef(true);
  React.useEffect(() => {
    live.current = true;
    return () => { live.current = false; };
  }, []);

  const klass = window.PC_classByKey(classKey);
  const close = () => {
    if (typeof onClose === "function") onClose();
  };
  const toast = (t) => {
    if (typeof api.toastPush === "function") api.toastPush(t);
  };

  // The page says, on every render, whether a request of its own (a save, a test) is out. A request STARTING clears a refused Delete's message: the line would
  // otherwise keep it over the answer to the request that follows.
  const pageBusyChanged = (next) => {
    if (next && !wasBusy.current) setDeleteError(null);
    wasBusy.current = next;
    setPageBusy(next);
  };

  // An edit opens on its row's class, which is the one option shown (a hidden class such as Web Fetch is named too); a create offers the classes.
  const options = editing ? (klass ? [PC_selectorOption(klass)] : []) : PC_selectorOptions(allowedClasses);
  const selector = {
    prefix: editing ? undefined : "Register a",
    value: classKey,
    options,
    onChange: (next) => {
      setDeleteError(null);
      setClassKey(next);
    },
    // A switch while a request is out would unmount the page that is waiting for the answer, and the failure the answer carries would never be seen.
    locked: editing || options.length <= 1 || pageBusy || deleting,
    testid: "pc-register-type",
  };
  const title = editing ? `Edit ${editRow.id}` : "Register provider";

  // A page's save: one toast, then the caller hears the row, then the overlay closes.
  const saved = (row) => {
    const id = (row && row.id) || (editRow && editRow.id) || "";
    toast({ kind: "success", title: editing ? "Provider saved" : "Provider registered", detail: id });
    if (typeof onSaved === "function") onSaved(row);
    close();
  };

  // Delete lives in edit mode, behind a confirm (design 6.9); never on a built-in row, which the server refuses (design 6.8). A refusal is said on the
  // error line and the overlay stays open; the click handler's promise never rejects.
  const remove = async () => {
    if (!editing || !klass || deleting) return;
    const ok = await window.confirmDialog({
      title: `Delete ${editRow.id}?`,
      message: `This removes the ${klass.noun} provider. Anything that uses it stops working.`,
      confirmLabel: "Delete provider",
      danger: true,
    });
    if (!ok) return;
    setDeleteError(null);
    setDeleting(true);
    try {
      await api.apiFetch("DELETE", `/${klass.plural}/${encodeURIComponent(editRow.id)}`);
    } catch (err) {
      if (live.current) {
        setDeleteError(api.readRefusal(err, String(err)).message);
        setDeleting(false);
      }
      return;
    }
    if (live.current) setDeleting(false);
    toast({ kind: "success", title: "Provider deleted", detail: editRow.id });
    if (typeof onDeleted === "function") onDeleted(editRow.id);
    else close();
  };

  // Held off (not busy: its own request is not the one out) while the page has a request out, so that a refusal of this one cannot land on the line in the middle of the other.
  const deleteButton = editing && klass && !window.PC_isBuiltin(classKey, editRow.id) ? (
    <Btn kind="danger" data-testid="pc-register-delete" busy={deleting} blocked={pageBusy} onClick={remove}>Delete provider</Btn>
  ) : null;
  const invalidate = editing && klass && klass.invalidate ? (
    <div data-testid="pc-register-invalidate">
      <window.PC_InvalidateAction plural={klass.plural} existingId={editRow.id} />
    </div>
  ) : null;

  // What a page calls: the overlay adds what it owns (Invalidate and the error line under the body, Delete after the page's own left footer) and hands the whole to the
  // shell's slots. A refused Delete's message is the newer word until a request of the page's starts (pageBusyChanged), so it takes the line while it stands.
  const frame = (parts) => {
    const p = parts || {};
    const error = deleteError || p.error || null;
    return (
      <PC_FramePublisher store={store} classKey={classKey} busy={!!p.busy} onBusy={pageBusyChanged}
        body={(
          <React.Fragment>
            {p.body || null}
            {invalidate}
            {error ? <div className="nv-form-error" role="alert" data-testid="pc-register-error">{error}</div> : null}
          </React.Fragment>
        )}
        left={(p.footerLeft || deleteButton) ? <React.Fragment>{p.footerLeft || null}{deleteButton}</React.Fragment> : null}
        right={p.footerRight || null} />
    );
  };

  let page;
  if (!klass) {
    page = <PC_RegisterNotice key={String(classKey)} frame={frame} onClose={close}>This console has no provider class "{String(classKey)}".</PC_RegisterNotice>;
  } else {
    const pageProps = { klass, editRow: editRow || null, excludeKinds, frame, blocked: deleting, onSaved: saved, onClose: close };
    const Page = (window.PC_REGISTER_PAGES || {})[classKey];
    if (Page) {
      page = <Page key={classKey} {...pageProps} />;
    } else if (klass.form === "panel") {
      const panelOf = PC_PANEL_FORMS[classKey];
      const Panel = panelOf ? panelOf() : null;
      page = Panel ? (
        <Panel key={classKey} existing={editRow || null} frame={frame} blocked={deleting} onSaved={saved} onClose={close} pushToast={api.toastPush} />
      ) : (
        <PC_RegisterNotice key={classKey} frame={frame} onClose={close}>The {klass.noun} provider form is not loaded in this console.</PC_RegisterNotice>
      );
    } else {
      page = <PC_RegisterFormPage key={classKey} {...pageProps} />;
    }
  }

  return (
    <React.Fragment>
      {page}
      <PK_FormOverlay title={title} selector={selector} onClose={close} testid="pc-register"
        footer={{
          left: <PC_FrameSlot store={store} part="left" classKey={classKey} />,
          right: <PC_FrameSlot store={store} part="right" classKey={classKey} />,
        }}>
        <PC_FrameSlot store={store} part="body" classKey={classKey} />
      </PK_FormOverlay>
    </React.Fragment>
  );
}

Object.assign(window, {
  PC_RegisterOverlay, PC_startClass, PC_selectorOptions, PC_PANEL_FORMS, PC_requestRegister, PC_useRegisterRequest,
});
