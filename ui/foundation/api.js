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
//   GET <path>?limit=<pageSize>&offset=<n * pageSize> with the path's own query after it, until
//   - a page shorter than the page size, or the server's `total` reached (the list has ended),
//   - a full page that adds no new id (a server that ignores the offset answers every page with the first one), or
//   - maxPages pages (the cap: the rows read are kept and `truncated` is set).
// Each id is listed once: a row created between two pages shifts the next page back by one and repeats a row. A page that fails after the first keeps what was
// read, with `truncated` set and `error` the failure in words; a failure of the first page, or of a read the caller aborted, rejects as the request did.
// The answer is {items, total: items.length, pages: the requests answered, truncated, limit: maxPages * pageSize, serverTotal: the server's last count or null,
// error}. Options: signal, pageSize (at most 200), maxPages (default 50), idKey (default "id"; a workspace's sessions are keyed by "session_id") and apiFetch
// (default window.primerApi.apiFetch, read when called). Pinned by tests/ui/test_fetch_all_pages.py.
var PR_PAGE_SIZE = 200;
var PR_MAX_PAGES = 50;

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
  var items = [];
  var seen = Object.create(null);
  var serverTotal = null;
  function answer(pages, truncated, error) {
    return {
      items: items, total: items.length, pages: pages, truncated: truncated, limit: maxPages * size, serverTotal: serverTotal, error: error,
    };
  }
  function page(n) {
    return fetchPage("GET", PR_pageUrl(path, size, n * size), null, { signal: o.signal }).then(function (body) {
      var rows = body && Array.isArray(body.items) ? body.items : [];
      if (body && typeof body.total === "number") serverTotal = body.total;
      var added = 0;
      rows.forEach(function (row) {
        var id = row && row[idKey] != null ? String(row[idKey]) : null;
        if (id !== null && seen[id]) return;
        if (id !== null) seen[id] = true;
        items.push(row);
        added += 1;
      });
      var read = n * size + rows.length;
      if (rows.length < size || added === 0 || (serverTotal !== null && read >= serverTotal)) return answer(n + 1, false, null);
      if (n + 1 >= maxPages) return answer(n + 1, true, null);
      return page(n + 1);
    }, function (err) {
      var aborted = (o.signal && o.signal.aborted) || (err && err.name === "AbortError");
      if (n === 0 || aborted) throw err;
      var said = window.primerApi.readRefusal ? window.primerApi.readRefusal(err).message : (err && err.message) || "Request failed";
      return answer(n, true, said);
    });
  }
  return page(0);
}

// What a list cut short says, or null for a whole list: "Showing the first 10000 of 12345 agents.", "Showing the first 10000 agents; there may be more." (the
// cap with no count from the server), or "Showing the first 400 agents; the rest did not load: <why>". Never a silent truncation (ruling 3).
function PR_pagedNote(list, noun) {
  if (!list || !list.truncated) return null;
  var n = (list.items || []).length;
  var what = noun || "rows";
  if (list.error) return "Showing the first " + n + " " + what + "; the rest did not load: " + list.error;
  if (typeof list.serverTotal === "number" && list.serverTotal > n) return "Showing the first " + n + " of " + list.serverTotal + " " + what + ".";
  return "Showing the first " + n + " " + what + "; there may be more.";
}

// The note under a list that was cut short (nothing for a whole one): props {list: what PR_fetchAllPages answered, noun, testid (default pr-paged-note)}.
function PR_PagedNote(props) {
  var text = PR_pagedNote(props.list, props.noun);
  if (!text) return null;
  return window.React.createElement("div", { className: "pr-paged-note", role: "note", "data-testid": props.testid || "pr-paged-note" }, text);
}

// useResource for a list read with PR_fetchAllPages, polled at most one page per base tick (ruling 4): the poll is `pollMs` times the pages the last read took,
// so a list of three pages polled every 15 s is read every 45 s, and a list of one page polls exactly as before. Every page is read on each poll, so a change on
// any page still shows, only later; a refetch (after a write, on a Refresh, on wake) is not slowed. A pollMs of 0 or null is passed through.
function PR_usePagedResource(key, fetcher, opts) {
  var o = opts || {};
  var pagesState = window.React.useState(1);
  var pages = pagesState[0];
  var setPages = pagesState[1];
  var base = o.pollMs;
  var res = window.primerApi.useResource(key, fetcher, Object.assign({}, o, { pollMs: base > 0 ? base * pages : base }));
  var read = res.data && res.data.pages > 1 ? res.data.pages : 1;
  window.React.useEffect(function () {
    if (read !== pages) setPages(read);
  }, [read, pages]);
  return res;
}

window.PR_PAGE_SIZE = PR_PAGE_SIZE;
window.PR_MAX_PAGES = PR_MAX_PAGES;
window.PR_fetchAllPages = PR_fetchAllPages;
window.PR_pagedNote = PR_pagedNote;
window.PR_PagedNote = PR_PagedNote;
window.PR_usePagedResource = PR_usePagedResource;
