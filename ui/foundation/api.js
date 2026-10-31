// primer UI — API client (apiFetch + ApiError).
// Loaded via <script type="text/babel"> in ui/index.html. No imports.
// Contributes to the shared window.primerApi namespace.

(function () {
  const T0103A_DETAIL = /pg_type_typname_nsp_index|relation .* does not exist/i;

  // Strip the leading "body."/"query."/"path." segment Pydantic adds
  // and convert the remaining loc into a readable dotted path.
  function _humanizeFieldPath(loc) {
    if (!Array.isArray(loc) || loc.length === 0) return "field";
    const parts = loc.slice();
    if (parts.length > 1 && (parts[0] === "body" || parts[0] === "query" || parts[0] === "path")) {
      parts.shift();
    }
    return parts.map(String).join(".") || "field";
  }

  // Build the friendly 422 summary: "Missing or invalid: a, b (max 4)".
  // Callers must guarantee a non-empty array (ApiError's constructor,
  // the only caller, checks `fieldErrors.length > 0` before calling).
  function _friendlyValidationDetail(fieldErrors) {
    const seen = new Set();
    const labels = [];
    for (const fe of fieldErrors) {
      const path = _humanizeFieldPath(fe?.loc);
      if (seen.has(path)) continue;
      seen.add(path);
      labels.push(path);
    }
    const head = labels.slice(0, 4).join(", ");
    const overflow = labels.length > 4 ? ` (+${labels.length - 4} more)` : "";
    return `Missing or invalid: ${head}${overflow}.`;
  }

  class ApiError extends Error {
    constructor(envelope) {
      super(envelope.title || `HTTP ${envelope.status}`);
      this.name = "ApiError";
      this.type = envelope.type;
      this.status = envelope.status;
      this.requestId = envelope.extensions?.request_id ?? null;
      this.fieldErrors = envelope.extensions?.errors ?? null;
      this.envelope = envelope;
      // Holistic 422 friendliness applies ONLY to genuine field-shaped
      // validation errors (FastAPI's own RequestValidationError, which
      // populates extensions.errors) - presented as "Data is incomplete"
      // + a humanized field list, so form toasts/inline errors that fall
      // back on title/detail show something useful instead of
      // "Validation Error". A domain-level PrimerError mapped to 422
      // (primer/api/errors.py's _make_primer_error_handler) never
      // populates extensions.errors - it has no field list, but it DOES
      // have its own specific, correct `detail` message (e.g. "seq 1 is
      // the newest visible record; nothing to discard"), which branching
      // on status alone used to discard in favor of the generic
      // fallback. Branch on the shape of the payload instead.
      if (Array.isArray(this.fieldErrors) && this.fieldErrors.length > 0) {
        this.title = "Data is incomplete";
        this.detail = _friendlyValidationDetail(this.fieldErrors);
      } else {
        this.title = envelope.title;
        this.detail = envelope.detail;
      }
    }
  }

  // ---- the ONE reader for a refused write (ticket 01a11cd1-7aaf) --------------------------------------------------------------------------------------
  // Seven components used to read {code, message} out of the problem envelope their own way and disagreed. The API puts a refusal's pieces in four places:
  // extensions.code (routers raise HTTPException(detail={code, message}); `detail` is then the message STRING), extensions.error (the auth gate, which sends
  // no message so `detail` is the code itself, and the {error, field, message} 422s of the pre-write checks: the profile router's and the agent router's
  // model_profile_not_found), extensions.errors[] (request validation: one entry per
  // field, `loc` the field and `msg` its words; its `type` is a pydantic type or a check's name, NOT a code to print) and, for a reference block, only the
  // sentence ("in_use_by: ...").

  const _SNAKE_CODE = /^[a-z][a-z0-9]*(_[a-z0-9]+)+$/;

  // What to say when the server sent a code and no sentence: the auth gate answers a session that ended, a reset password or a sign-out everywhere (401)
  // and a role that may not do this (403) with only the code.
  const _BARE_SENTENCES = {
    auth_required: "Your session has ended; sign in again.",
    forbidden_role: "Your role does not allow this.",
    // ACL-P2-T10: a 403 whose code is access_denied (the require() refusal of the access routes: a level
    // below the one the write needs). With the server's own words it says them; bare, it says this.
    access_denied: "You don't have enough access for this.",
    // ACL-P3-T7: a 409 (harness_managed) on a row the harness installed: its sharing is the harness's to change, so
    // a direct share or share-edit of it is refused by name.
    harness_managed: "This is shared through its harness; share the harness instead.",
  };

  // "in_use_by: 1 agent(s) reference 'p-1' (first: 'builder')" (primer/api/routers/_references.py): the count is the size of a one-row page, so it only
  // means "at least one". Quotes are Python's repr, which uses double quotes for a value that holds a single quote.
  // A kind is words (or words and one parenthetical: "model_profile (aggregate member)") and an id never holds the quote that closes it, so a second id, a second
  // block or extra words after the first one make no match and the sentence is left as it is.
  const _IN_USE_BY = /^in_use_by:\s*\d+\s+(\w[\w ]*(?: \([\w ]+\))?)\(s\)\s+reference\s+(['"])((?:(?!\2).)+)\2\s+\(first:\s*(['"])((?:(?!\4).)+)\4\)\s*$/;
  // "Channel with provider_id='rev-slack', external_id='C0AAAA0001' already exists (id='channel-1')" (primer/channel/checks.py).
  const _ALREADY_EXISTS = /^(\w[\w ]*?) with (.+?) already exists \(id=(['"])((?:(?!\3).)+)\3\)\s*$/;
  const _ASSIGNMENT = /(\w+)=(['"])(.*?)\2/g;

  const _PLAIN_MAX = 500;

  function _article(word) {
    return /^[aeiou]/i.test(word) ? "an" : "a";
  }

  // A kind is a Python identifier on the server (tool_approval_policy, workspace_template): said as words.
  function _words(kind) {
    return kind.replace(/_/g, " ");
  }

  // The server's sentence as a person reads it. Two sentences are code-shaped (admin review ADM-12 and ADM-20); every other sentence is returned as it is.
  function _plainSentence(sentence) {
    // Both shapes are short (two ids and a kind); a long sentence is somebody else's text, and the lazy parts of the patterns are not meant for it.
    if (sentence.length > _PLAIN_MAX) return sentence;
    const block = sentence.startsWith("in_use_by:") ? _IN_USE_BY.exec(sentence) : null;
    if (block) {
      const kind = _words(block[1]);
      return `${block[2]}${block[3]}${block[2]} is still in use by ${_article(kind)} ${kind}, for example ${block[5]}. Remove or change that first.`;
    }
    if (/^in_use_by:\s*\S/.test(sentence)) {
      const rest = sentence.replace(/^in_use_by:\s*/, "").trim();
      return rest.charAt(0).toUpperCase() + rest.slice(1);
    }
    const dup = sentence.includes(" already exists (id=") ? _ALREADY_EXISTS.exec(sentence) : null;
    if (dup) {
      const pairs = [];
      let m;
      _ASSIGNMENT.lastIndex = 0;
      while ((m = _ASSIGNMENT.exec(dup[2])) !== null) pairs.push([m[0], m[1], m[3]]);
      if (pairs.length > 0 && pairs.map((p) => p[0]).join(", ") === dup[2]) {
        const spoken = _words(dup[1]);
        const kind = spoken.charAt(0).toLowerCase() + spoken.slice(1);
        return `${_article(kind).replace(/^a/, "A")} ${kind} with ${pairs.map((p) => `${p[1]} ${p[2]}`).join(" and ")} already exists: ${dup[4]}.`;
      }
    }
    return sentence;
  }

  // {code, field, sentence, message} of a failed request, from the thrown ApiError (or any Error).
  //   code      the machine code, or null. A request-validation error has none: its `type` is a pydantic type (string_too_short) or a check's name.
  //   field     the field the refusal is about (dotted, no "body."), or null.
  //   sentence  the server's own words, or "" when it sent none or only the code. A message equal to the code is not a sentence; one that merely looks like
  //             a code is kept when the code is known and different (a not-found message is a bare id, and an id may hold an underscore). Exactly one
  //             request-validation error is said in its own `msg`; several are not. A caller that draws the error under its field uses this.
  //   message   what a person reads: the sentence (said plainly, see _plainSentence), with the field in front for a single validation error that has one
  //             ("name: Field required": "String should have at least 1 character" alone does not say which field, and a generic toast has no field to draw
  //             it under); else the sentence for a known bare code; else, for request validation, ApiError's "Missing or invalid: a, b."; else the HTTP
  //             title, the error's message, the caller's fallback, "Request failed", with the code after it when there is one ("Forbidden (scope_required)":
  //             a title alone tells nobody what was refused).
  // options.codeAfterTitle === false keeps that last fallback to the title alone: for a caller whose banner must never show a code (the triggers').
  // Whether a request-validation entry points at a FIELD a person can name: not the whole body or query (loc ["body"]: "body: Field required" names nothing) and
  // not a position (loc ["body", 1], FastAPI's "JSON decode error" at character 1: "1: JSON decode error"). What a person can find is a NAME: a location names a field when it
  // holds a string segment after the body / query / path prefix (["body", "items", 2] is a list item of a named field: "items.2: Input should be a valid string"); numbers only after the
  // prefix (["body", 1], ["body", 0, 1]) are positions, not fields.
  function _namesAField(entry) {
    const loc = entry && entry.loc;
    if (!Array.isArray(loc) || loc.length === 0) return false;
    const prefixed = loc[0] === "body" || loc[0] === "query" || loc[0] === "path";
    return (prefixed ? loc.slice(1) : loc).some((segment) => typeof segment === "string");
  }

  function readRefusal(err, fallback, options) {
    const env = err && err.envelope;
    const ext = (env && env.extensions) || {};
    const det = env && env.detail && typeof env.detail === "object" ? env.detail : {};
    const errors = Array.isArray(ext.errors) && ext.errors.length > 0 ? ext.errors : null;
    const first = errors && errors[0] && typeof errors[0] === "object" ? errors[0] : null;
    const text = err && typeof err.detail === "string" ? err.detail.trim() : "";

    let code = ext.code || ext.error || det.code || det.error || null;
    if (!code) {
      if (/^in_use_by:/.test(text)) code = "in_use_by";
      else if (_SNAKE_CODE.test(text)) code = text;
    }
    code = code ? String(code) : null;

    let field = null;
    if (ext.field) field = String(ext.field);
    else if (first && Array.isArray(first.loc) && first.loc.length > 0) field = _humanizeFieldPath(first.loc);

    let sentence = "";
    if (errors) {
      if (errors.length === 1 && first && typeof first.msg === "string" && first.msg.trim()) sentence = first.msg;
    } else {
      const options = [ext.message, det.message, err && typeof err.detail === "string" ? err.detail : ""];
      for (const m of options) {
        if (typeof m === "string" && m.trim() && m.trim() !== code) {
          sentence = m;
          break;
        }
      }
    }

    let message;
    if (sentence) message = errors && _namesAField(first) ? `${field}: ${_plainSentence(sentence)}` : _plainSentence(sentence);
    else if (code && _BARE_SENTENCES[code]) message = _BARE_SENTENCES[code];
    else if (errors && text) message = text;
    else {
      const base = (err && (err.title || err.message)) || fallback || "Request failed";
      message = code && !(options && options.codeAfterTitle === false) ? `${base} (${code})` : base;
    }
    return { code, field, sentence, message };
  }

  function resolvePath(path) {
    if (typeof path !== "string" || path.length === 0) {
      throw new TypeError("apiFetch: path must be a non-empty string");
    }
    if (path.startsWith("/v1/")) return path;
    if (path.startsWith("/")) return "/v1" + path;
    return "/v1/" + path;
  }

  function isT0103aRetryable(envelope) {
    return (
      envelope &&
      envelope.status === 502 &&
      envelope.type === "/errors/provider-error" &&
      typeof envelope.detail === "string" &&
      T0103A_DETAIL.test(envelope.detail)
    );
  }

  async function parseEnvelope(res) {
    let body = null;
    try {
      body = await res.json();
    } catch (_e) {
      body = null;
    }
    if (body && typeof body === "object") {
      if (typeof body.status !== "number") body.status = res.status;
      return body;
    }
    return {
      type: "about:blank",
      title: res.statusText || `HTTP ${res.status}`,
      status: res.status,
      detail: null,
    };
  }

  async function singleFetch(method, url, body, opts) {
    const headers = { Accept: "application/json" };
    const init = {
      method,
      headers,
      credentials: "same-origin",
    };
    if (body !== undefined && body !== null) {
      if (typeof FormData !== "undefined" && body instanceof FormData) {
        // FormData uploads (multipart/form-data). Let fetch synthesise
        // the Content-Type header itself - it carries the multipart
        // boundary the server needs to parse the body.
        init.body = body;
      } else {
        headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(body);
      }
    }
    if (opts.signal) init.signal = opts.signal;

    let res;
    try {
      res = await fetch(url, init);
    } catch (e) {
      throw new ApiError({
        type: "/errors/network-error",
        title: "Network error",
        detail: e && e.message ? e.message : String(e),
        status: 0,
      });
    }

    if (res.status === 204) return { ok: true, data: null, envelope: null };

    if (!res.ok) {
      const envelope = await parseEnvelope(res);
      return { ok: false, data: null, envelope };
    }

    let data = null;
    try {
      data = await res.json();
    } catch (_e) {
      data = null;
    }
    return { ok: true, data, envelope: null };
  }

  async function apiFetch(method, path, body, opts = {}) {
    const upper = String(method || "GET").toUpperCase();
    const url = resolvePath(path);

    let result = await singleFetch(upper, url, body, opts);
    if (!result.ok && isT0103aRetryable(result.envelope)) {
      result = await singleFetch(upper, url, body, opts);
    }

    if (!result.ok) throw new ApiError(result.envelope);
    return result.data;
  }

  const ns = (window.primerApi = window.primerApi || {});
  ns.apiFetch = apiFetch;
  ns.resolvePath = resolvePath;
  ns.ApiError = ApiError;
  ns.readRefusal = readRefusal;
})();

// ---- every page of a list (ticket 01a16dfa) ------------------------------------------------------------------------------------------------------------
// A list route answers ONE page: {kind: "offset", offset, length, total, items}, at most 200 rows (primer/api/pagination.py caps ?limit at 200), and `total`
// is null when the backend cannot count. About 35 console lists read the first page of 200 and stopped, so a 201st workspace, agent or session was reachable
// nowhere in the console. PR_fetchAllPages reads every page, modelled on the templates page's own reader (#886):
//   GET <path>?limit=<pageSize>&offset=<rows answered so far> with the path's own query after it (the offset advances by the rows a page answered, so a
//   server that answers fewer than the limit is read on from where it stopped), until
//   - the list has ended: an empty page; with a `total` from the server, the rows answered reaching it; without one, a page shorter than the page size,
//   - a page that adds nothing new (a server that ignores the offset answers every page with the first one; rows are told apart by idKey, and a page of
//     rows without one that repeats the previous page adds nothing), or
//   - maxPages pages (the cap).
// Each row is listed once: a row created between two pages shifts the next page back and repeats a row. The read is `truncated` (and its note says so) when it
// stopped at the cap with rows possibly left, when a page after the first failed (the rows read are kept, `error` says why), and whenever it ended with fewer
// rows than the server's `total`. A failure of the first page, or of a read the caller aborted, rejects as the request did. A partial answer is for a FIRST
// read only: useResource (ui/foundation/use-resource.js, PR_partialFailure) keeps the list it already shows when a refresh comes back cut short by a failure.
// The answer is {items, total: items.length, pages: the requests answered, truncated, limit: maxPages * pageSize, serverTotal: the server's last count or null,
// error}. Options: signal, pageSize (at most 200), maxPages (default 50), idKey (default "id"; a workspace's sessions are keyed by "session_id") and apiFetch
// (default window.primerApi.apiFetch, read when called). PR_fetchAllFind runs the same read loop over the server's filter query (POST <path>, the resource's
// /find route, the FindRequest body: predicate + offset page + optional sort keys) instead of a whole-table GET, so a filtered panel pays one request per page
// of the answer. Pinned by tests/ui/test_fetch_all_pages.py (the GET form) and tests/ui/test_fetch_all_find.py (the find form).
var PR_PAGE_SIZE = 200;
var PR_MAX_PAGES = 50;
// The longest a lengthened poll may get (ruling 4 of round 2): a session's status a minute stale is the most a list may show.
var PR_MAX_POLL_MS = 60000;

function PR_pageUrl(path, limit, offset) {
  var at = path.indexOf("?");
  var base = at < 0 ? path : path.slice(0, at);
  var query = at < 0 ? "" : path.slice(at + 1);
  return base + "?limit=" + limit + "&offset=" + offset + (query ? "&" + query : "");
}

function PR_fetchAllPages(path, opts) {
  var o = opts || {};
  var size = o.pageSize > 0 ? Math.min(o.pageSize, PR_PAGE_SIZE) : PR_PAGE_SIZE;
  var maxPages = o.maxPages > 0 ? o.maxPages : PR_MAX_PAGES;
  var idKey = o.idKey || "id";
  var fetchPage = o.apiFetch || window.primerApi.apiFetch;
  return PR_pagedLoop(size, maxPages, idKey, o.signal, function (offset) {
    return fetchPage("GET", PR_pageUrl(path, size, offset), null, { signal: o.signal });
  });
}

// The read loop both paged reads share: requestPage(offset) answers one page ({items, total} - a list route's page or a
// find's OffsetPageResponse), and the loop reads on until a short page, the server's total, a full page that adds no new
// id (rows are told apart by idKey), or the page cap. What it read past the cap, or before a page that failed, is kept and
// said (truncated), never dropped in silence; a first-page failure, or a read the caller aborted, rejects as the request did.
function PR_pagedLoop(size, maxPages, idKey, signal, requestPage) {
  var items = [];
  var seen = Object.create(null);
  var serverTotal = null;
  var lastPage = null;
  function answer(pages, cut, error, failure) {
    var short = serverTotal !== null && items.length < serverTotal;
    var out = {
      items: items, total: items.length, pages: pages, truncated: !!(cut || error || short), limit: maxPages * size, serverTotal: serverTotal,
      error: error,
    };
    // The failure itself, for useResource to keep a list it already shows (PR_partialFailure); not enumerable, so it is never compared or copied.
    if (failure) Object.defineProperty(out, "failure", { value: failure, enumerable: false });
    return out;
  }
  function page(n, offset) {
    return requestPage(offset).then(function (body) {
      var rows = body && Array.isArray(body.items) ? body.items : [];
      if (body && typeof body.total === "number") serverTotal = body.total;
      var signature = rows.length ? JSON.stringify(rows) : null;
      var added = 0;
      if (signature === null || signature !== lastPage) {
        rows.forEach(function (row) {
          var id = row && row[idKey] != null ? String(row[idKey]) : null;
          if (id !== null && seen[id]) return;
          if (id !== null) seen[id] = true;
          items.push(row);
          added += 1;
        });
      }
      lastPage = signature;
      var read = offset + rows.length;
      var ended = rows.length === 0 || (serverTotal !== null ? read >= serverTotal : rows.length < size);
      if (ended || added === 0) return answer(n + 1, false, null, null);
      if (n + 1 >= maxPages) return answer(n + 1, true, null, null);
      return page(n + 1, read);
    }, function (err) {
      var aborted = (signal && signal.aborted) || (err && err.name === "AbortError");
      if (n === 0 || aborted) throw err;
      var said = window.primerApi.readRefusal ? window.primerApi.readRefusal(err).message : (err && err.message) || "Request failed";
      return answer(n, true, said, err);
    });
  }
  return page(0, 0);
}

// The find sibling of PR_fetchAllPages (ticket 01a16eaa): the same read of every page, over the server's filter query
// instead of a whole-table GET. POST <path> (the resource's /find route) with the FindRequest body - the predicate (a
// predicate tree, or null for every row), an offset page, optional sort keys - and the answer is the same offset page
// shape, so a panel that wants "the rows of this harness" or "the sessions bound to these agents" pays one request per
// page of the ANSWER, not of the table. Pinned by tests/ui/test_fetch_all_find.py (the GET form by test_fetch_all_pages.py).
function PR_fetchAllFind(path, predicate, opts) {
  var o = opts || {};
  var size = o.pageSize > 0 ? Math.min(o.pageSize, PR_PAGE_SIZE) : PR_PAGE_SIZE;
  var maxPages = o.maxPages > 0 ? o.maxPages : PR_MAX_PAGES;
  var idKey = o.idKey || "id";
  var fetchPage = o.apiFetch || window.primerApi.apiFetch;
  return PR_pagedLoop(size, maxPages, idKey, o.signal, function (offset) {
    var body = { predicate: predicate, page: { kind: "offset", offset: offset, length: size } };
    if (o.order_by) body.order_by = o.order_by;
    return fetchPage("POST", path, body, { signal: o.signal });
  });
}

// The failure that cut an answer of PR_fetchAllPages short (a page after the first failed), or null for any other value. useResource reads it: a refresh that
// comes back cut short by a failure keeps the list already shown and reports the failure as its error (stale-while-error); only a first read shows the
// partial list with its note. The error is the request's own (an ApiError), or one made from the answer's words when the answer was copied on the way.
function PR_partialFailure(data) {
  if (!data || data.truncated !== true || typeof data.error !== "string" || !data.error) return null;
  if (data.failure) return data.failure;
  var err = new Error(data.error);
  err.detail = data.error;
  return err;
}

// The noun for a count: `noun` is [one, many], or a plural such as "agents", "approval policies" or "harnesses" whose singular is worked out.
function PR_nounFor(noun, n) {
  if (Array.isArray(noun)) return n === 1 ? noun[0] : noun[1];
  var many = noun || "rows";
  if (n !== 1) return many;
  if (/ies$/.test(many)) return many.replace(/ies$/, "y");
  if (/sses$/.test(many)) return many.replace(/sses$/, "ss");
  return many.replace(/s$/, "");
}

// What a list cut short says, or null for a whole list: "Showing the first 10000 of 12345 agents.", "Showing the first 10000 agents; there may be more." (the
// cap with no count from the server), or "Showing the first 400 agents; the rest did not load: <why>". Never a silent truncation (ruling 3). `noun` is a
// plural or [one, many].
function PR_pagedNote(list, noun) {
  if (!list || !list.truncated) return null;
  var n = (list.items || []).length;
  if (list.error) return "Showing the first " + n + " " + PR_nounFor(noun, n) + "; the rest did not load: " + list.error;
  if (typeof list.serverTotal === "number" && list.serverTotal > n) {
    return "Showing the first " + n + " of " + list.serverTotal + " " + PR_nounFor(noun, list.serverTotal) + ".";
  }
  return "Showing the first " + n + " " + PR_nounFor(noun, n) + "; there may be more.";
}

// The note under a list read with something to say: a cut-short read speaks for itself (nothing for a whole one);
// a read that FAILED outright (``error``) names the failure instead of the page silently reading 0 (G10-2).
function PR_PagedNote(props) {
  var text = PR_pagedNote(props.list, props.noun);
  if (props.error) {
    var msg = String((props.error && (props.error.detail || props.error.message)) || props.error);
    text = "The " + (props.noun || "rows") + " read failed: " + msg;
  }
  if (!text) return null;
  return window.React.createElement("div", { className: "pr-paged-note", role: "note", "data-testid": props.testid || "pr-paged-note" }, text);
}

// The poll of a list read in `pages` pages: `base` times the pages, so a list of three pages polled every 15 s is read every 45 s (one request per 15 s on
// average, as the one page it replaced), but never longer than PR_MAX_POLL_MS (a minute) unless the base itself is longer. A base of 0 or null is passed through.
function PR_pagedPollMs(base, pages) {
  if (!(base > 0)) return base;
  return Math.max(base, Math.min(base * Math.max(1, pages || 1), PR_MAX_POLL_MS));
}

// useResource for a list read with PR_fetchAllPages, polled at most one page per base tick (ruling 4), within a minute (PR_pagedPollMs). Every page is read on
// each poll, so a change on any page still shows, only later; a refetch (after a write, on a Refresh, on wake) is not slowed.
function PR_usePagedResource(key, fetcher, opts) {
  var o = opts || {};
  var pagesState = window.React.useState(1);
  var pages = pagesState[0];
  var setPages = pagesState[1];
  var res = window.primerApi.useResource(key, fetcher, Object.assign({}, o, { pollMs: PR_pagedPollMs(o.pollMs, pages) }));
  var read = res.data && res.data.pages > 1 ? res.data.pages : 1;
  window.React.useEffect(function () {
    if (read !== pages) setPages(read);
  }, [read, pages]);
  return res;
}

window.PR_PAGE_SIZE = PR_PAGE_SIZE;
window.PR_MAX_PAGES = PR_MAX_PAGES;
window.PR_MAX_POLL_MS = PR_MAX_POLL_MS;
window.PR_fetchAllPages = PR_fetchAllPages;
window.PR_fetchAllFind = PR_fetchAllFind;
window.PR_partialFailure = PR_partialFailure;
window.PR_nounFor = PR_nounFor;
window.PR_pagedNote = PR_pagedNote;
window.PR_PagedNote = PR_PagedNote;
window.PR_pagedPollMs = PR_pagedPollMs;
window.PR_usePagedResource = PR_usePagedResource;
