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
  // no message so `detail` is the code itself, and the profile pre-write 422s {error, field, message}), extensions.errors[] (request validation: one entry per
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
    if (sentence) message = errors && field ? `${field}: ${_plainSentence(sentence)}` : _plainSentence(sentence);
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
