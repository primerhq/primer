/* global React, window, Btn */
// register-overlay.jsx - window.PC_RegisterOverlay, the one Register provider overlay (plan 02 Task 11; seam I-3 of the master plan; design 6).
//
// One overlay registers and edits a provider of every class. It is the kit's PK_FormOverlay: the title ("Register provider", or "Edit <id>"), the type
// selector at the centre top ("Register a [icon] <Type> provider", the classes in the sidebar order; Web Fetch only when a caller allows it), the page of
// the chosen class below it, and the page's footer: Cancel on the left, the action on the right. A page renders INTO the overlay: it is handed a `frame`
// function and returns frame({ body, footerLeft, footerRight, error }), so each page owns its own state and footer while the overlay owns the shell, the
// error line, Invalidate and Delete. Changing the type remounts the page (`key`), so values typed for the previous type are dropped (design 6.1).
//
// Which page a class gets:
// - a page registered in window.PC_REGISTER_PAGES (the wizard classes: LLM in Task 12, Embedding and Cross Encoding in Task 14, TTS and ASR in Task 15);
// - otherwise a panel class gets its own create form (PC_PANEL_FORMS), handed { existing, frame, onSaved, onClose, pushToast };
// - otherwise the schema-driven PC_ProviderForm in its framed layout (the form classes, and a wizard class whose own page is not loaded).
//
// Seam I-3: PC_RegisterOverlay({ initialClass, allowedClasses?, excludeKinds?, editRow?, onClose(), onSaved(row), onDeleted?(id) }). `excludeKinds` is
// added to the built-in kinds the Type select leaves out (Plan 04 passes ["local"]); without `onDeleted` the overlay closes itself after a delete.

// The wizard classes' pages, merged in by the files that define them (each does Object.assign(window.PC_REGISTER_PAGES || {}, { <key>: Page })). A page
// takes { klass, editRow, excludeKinds, frame, onSaved(row), onClose() } and returns frame({ body, footerLeft?, footerRight, error? }).
window.PC_REGISTER_PAGES = Object.assign(window.PC_REGISTER_PAGES || {}, {});

// The panel classes' own create forms, read when they are needed (their files load after this one). Each takes { existing, frame, onSaved, onClose,
// pushToast } (Tasks 17 to 19 give them `frame` and `onSaved`).
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

// The page of a form class (and of a wizard class whose own page is not loaded): the schema-driven form in its framed layout. An edit starts from the full
// served row (PC_draftFromRow), so every field the form does not show is sent back unchanged; the body goes out with the wire names (PC_wireBody). A
// built-in row whose kind has nothing to configure is shown read-only.
function PC_RegisterFormPage({ klass, editRow, excludeKinds, frame, onSaved, onClose }) {
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
  return (
    <window.PC_ProviderForm plural={klass.plural} value={draft} onChange={setDraft} onSubmit={submit} onCancel={onClose}
      editing={editing} existingId={editing ? editRow.id : null} frame={frame} classKey={klass.key} kindSelect
      excludeKinds={excludeKinds} canTest={!!klass.draftTest} submitLabel={editing ? "Save" : "Register"} readOnly={readOnly} />
  );
}

function PC_RegisterOverlay({ initialClass, allowedClasses, excludeKinds, editRow, onClose, onSaved, onDeleted }) {
  const api = window.primerApi;
  const PK_FormOverlay = window.PK_FormOverlay;
  const editing = !!editRow;
  const [classKey, setClassKey] = React.useState(() => PC_startClass(initialClass, allowedClasses, editRow));
  const [deleting, setDeleting] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState(null);
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
    locked: editing || options.length <= 1,
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

  const deleteButton = editing && klass && !window.PC_isBuiltin(classKey, editRow.id) ? (
    <Btn kind="danger" data-testid="pc-register-delete" busy={deleting} onClick={remove}>Delete provider</Btn>
  ) : null;
  const invalidate = editing && klass && klass.invalidate ? (
    <div data-testid="pc-register-invalidate">
      <window.PC_InvalidateAction plural={klass.plural} existingId={editRow.id} />
    </div>
  ) : null;

  const frame = (parts) => {
    const p = parts || {};
    const error = deleteError || p.error || null;
    return (
      <PK_FormOverlay title={title} selector={selector} onClose={close} testid="pc-register"
        footer={{
          left: (p.footerLeft || deleteButton) ? <React.Fragment>{p.footerLeft || null}{deleteButton}</React.Fragment> : null,
          right: p.footerRight || null,
        }}>
        {p.body || null}
        {invalidate}
        {error ? <div className="nv-form-error" role="alert" data-testid="pc-register-error">{error}</div> : null}
      </PK_FormOverlay>
    );
  };

  if (!klass) {
    return frame({
      body: <div className="nv-form-error" role="alert">This console has no provider class "{String(classKey)}".</div>,
      footerLeft: <Btn kind="ghost" data-testid="pc-register-cancel" onClick={close}>Cancel</Btn>,
    });
  }
  const pageProps = { klass, editRow: editRow || null, excludeKinds, frame, onSaved: saved, onClose: close };
  const Page = (window.PC_REGISTER_PAGES || {})[classKey];
  if (Page) return <Page key={classKey} {...pageProps} />;
  if (klass.form === "panel") {
    const panelOf = PC_PANEL_FORMS[classKey];
    const Panel = panelOf ? panelOf() : null;
    if (Panel) {
      return <Panel key={classKey} existing={editRow || null} frame={frame} onSaved={saved} onClose={close} pushToast={api.toastPush} />;
    }
    return frame({
      body: <div className="nv-form-error" role="alert">The {klass.noun} provider form is not loaded in this console.</div>,
      footerLeft: <Btn kind="ghost" data-testid="pc-register-cancel" onClick={close}>Cancel</Btn>,
    });
  }
  return <PC_RegisterFormPage key={classKey} {...pageProps} />;
}

Object.assign(window, {
  PC_RegisterOverlay, PC_startClass, PC_selectorOptions, PC_PANEL_FORMS, PC_requestRegister, PC_useRegisterRequest,
});
