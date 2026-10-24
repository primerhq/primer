/* global React, window, Btn, PK_Field, PK_Section, NV_useConsole, PR_fetchAllPages, PR_PagedNote */
// artifact-store-selector.jsx - "Store new files in": which artifact store NEW files are written to (plan 09, Task 11; seam I-4). Providers >
// Artifact Store draws it above the cards through window.PC_TYPE_EXTRAS.artifact_storage. It reads GET /artifact_storage_active_config and
// GET /artifact_storage_providers and writes PUT /artifact_storage_active_config; files stored earlier stay where they are and are still read.
//
// - Admin only. The three routes are, so anyone else gets nothing: no control that can only fail, and no request that can only be refused.
// - A selection the server refuses (a filesystem root this process cannot use, an S3 row with half a key pair, a store deleted meanwhile)
//   is said under the field in the server's words (readRefusal, never a code), and the select shows the stored store again.
// - While a selection is out the select is aria-busy, NOT natively disabled (a focused control that turns disabled drops the focus to
//   <body>), and a second change is ignored: no second request.
// Loaded after the kit and right before provider-catalog.jsx (ui/index.html). Tests: tests/ui/test_artifact_store_selector.py.

const AS_KIND_LABELS = { db: "database", filesystem: "filesystem", s3: "S3" };
const AS_ACTIVE_KEY = "as:active-store";
const AS_STORES_KEY = "as:stores";
const AS_LABEL = "Store new files in";
const AS_HELP = "New files go here. Files stored earlier stay where they are and can still be read.";
const AS_NO_OTHER = "No other artifact store is registered. Register a filesystem or S3 store to send new files somewhere else.";
const AS_NONE = "No artifact stores are registered.";

// What a person reads for a failed request: the server's sentence, else the HTTP title; never the machine code (codeAfterTitle: false).
function AS_words(err) {
  return window.primerApi.readRefusal(err, "The request failed.", { codeAfterTitle: false }).message;
}

function AS_toast(toast) {
  if (typeof window.primerApi.toastPush === "function") window.primerApi.toastPush(toast);
}

// The select's options: every listed store as "<id> (<kind>)", and the active id first when the list lacks it (a list read before it was
// registered, or a stored id whose row is gone), so the select always names the store new files go to.
function AS_options(items, activeId) {
  const rows = (Array.isArray(items) ? items : []).filter((row) => row && row.id);
  const options = rows.map((row) => {
    const kind = AS_KIND_LABELS[row.provider] || String(row.provider || "unknown kind");
    return { value: String(row.id), label: String(row.id) + " (" + kind + ")" };
  });
  if (activeId && !options.some((option) => option.value === activeId)) options.unshift({ value: activeId, label: activeId });
  return { options, listed: rows.length };
}

function AS_ActiveStoreField() {
  const api = window.primerApi;
  const active = api.useResource(AS_ACTIVE_KEY, (signal) => api.apiFetch("GET", "/artifact_storage_active_config", null, { signal }), { pollMs: 0 });
  // Every store, every page (PR_fetchAllPages, ui/foundation/api.js); a list cut short says so under the field.
  const stores = api.useResource(AS_STORES_KEY, (signal) => PR_fetchAllPages("/artifact_storage_providers", { signal, apiFetch: api.apiFetch }), { pollMs: 0 });
  // `local` is the server's answer to this page's last successful switch, shown until the active row is read again; `pending` is the
  // choice being saved; `refusal` is the server's words for the last refused one.
  const [local, setLocal] = React.useState(null);
  const [pending, setPending] = React.useState(null);
  const [refusal, setRefusal] = React.useState(null);
  const outRef = React.useRef(false);
  const servedRef = React.useRef(active.data);

  React.useEffect(() => {
    if (active.data === servedRef.current) return;
    servedRef.current = active.data;
    setLocal(null);
  }, [active.data]);

  const loaded = active.data !== undefined && stores.data !== undefined;
  const loadError = active.error || stores.error;
  const retry = () => {
    active.refetch();
    stores.refetch();
  };
  const retryButton = loadError ? (
    <Btn kind="ghost" size="sm" icon="refresh" data-testid="as-active-store-retry" onClick={retry}>Retry</Btn>
  ) : null;
  const loadWords = loadError ? "Could not load the artifact stores: " + AS_words(loadError) : null;

  if (!loaded) {
    return (
      <PK_Section title="DEFAULT STORE" testid="as-active-store" cols={1}>
        {loadError ? (
          <PK_Field label={AS_LABEL} error={loadWords}>{retryButton}</PK_Field>
        ) : (
          <PK_Field label={AS_LABEL}>
            <select data-testid="as-active-store-select" value="" disabled style={{ width: "100%" }}>
              <option value="">Loading stores</option>
            </select>
          </PK_Field>
        )}
      </PK_Section>
    );
  }

  const stored = local || (active.data && active.data.provider_id) || "";
  const { options, listed } = AS_options(stores.data && stores.data.items, stored);
  const help = listed === 0 ? AS_NONE : options.every((option) => option.value === stored) ? AS_NO_OTHER : AS_HELP;
  const saving = pending !== null;

  const choose = (event) => {
    const next = event && event.target ? event.target.value : "";
    if (outRef.current || !next || next === stored) return undefined;
    outRef.current = true;
    setPending(next);
    setRefusal(null);
    return api.apiFetch("PUT", "/artifact_storage_active_config", { provider_id: next }).then(
      (answer) => {
        setLocal((answer && answer.provider_id) || next);
        AS_toast({ kind: "success", title: "New files now go to " + next });
        active.refetch();
      },
      (err) => {
        const words = AS_words(err);
        setRefusal(words);
        AS_toast({ kind: "error", title: "Could not switch the artifact store", detail: words });
        // Someone else may have changed the selection or removed the store: read both again.
        active.refetch();
        stores.refetch();
      },
    ).finally(() => {
      outRef.current = false;
      setPending(null);
    });
  };

  return (
    <PK_Section title="DEFAULT STORE" testid="as-active-store" cols={1}>
      <PK_Field label={AS_LABEL} help={help} error={refusal || loadWords}>
        <select data-testid="as-active-store-select" value={saving ? pending : stored} aria-busy={saving ? "true" : undefined}
          style={{ width: "100%" }} onFocus={() => stores.refetch()} onChange={choose}>
          {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
        {retryButton}
      </PK_Field>
      <PR_PagedNote list={stores.data} noun="artifact stores" testid="as-stores-note" />
    </PK_Section>
  );
}

// The extra itself: an admin gets the field, anyone else nothing (and so no request). The catalog hands every extra {rows, reload}; this one
// reads its own list, so it can ask again when the select takes the focus.
function AS_ActiveStoreSelector() {
  const con = typeof NV_useConsole === "function" ? NV_useConsole() : null;
  if (!con || con.role !== "admin") return null;
  return <AS_ActiveStoreField />;
}

window.AS_ActiveStoreSelector = AS_ActiveStoreSelector;
(window.PC_TYPE_EXTRAS = window.PC_TYPE_EXTRAS || {}).artifact_storage = AS_ActiveStoreSelector;
