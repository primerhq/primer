// primer UI — API client (apiFetch + ApiError).
// Loaded via <script type="text/babel"> in ui/index.html. No imports.
// Contributes to the shared window.primerApi namespace.

(function () {
  const T0103A_DETAIL = /pg_type_typname_nsp_index|relation .* does not exist/i;

  class ApiError extends Error {
    constructor(envelope) {
      super(envelope.title || `HTTP ${envelope.status}`);
      this.name = "ApiError";
      this.type = envelope.type;
      this.title = envelope.title;
      this.detail = envelope.detail;
      this.status = envelope.status;
      this.requestId = envelope.extensions?.request_id ?? null;
      this.fieldErrors = envelope.extensions?.errors ?? null;
      this.envelope = envelope;
    }
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
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
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
  ns.ApiError = ApiError;
})();
