/* global React, Icon, Btn */

// Shared, reusable pagination primitive for the console's list/table views.
//
// The backend list endpoints already paginate: they accept `?limit=&offset=`
// and return an OffsetPageResponse — `{ kind:"offset", offset, length,
// total (nullable), items:[...] }`. Historically the UI fetched a hard
// `limit=200` and dumped everything with no controls; this module replaces
// that with a real offset pager.
//
// Two exports (both on window for the no-build shared global scope):
//   usePagedList({ key, path, pageSize, pollMs, params, resetKey }) -> state
//   <Pager pager={...} label="agents" />                            -> control
//
// `total` may be null (backends that can't count cheaply). When it is, we
// infer "has next" from a full page: a page that came back full (length ===
// pageSize) MIGHT have more behind it, so Next stays enabled; a short page
// is definitively the last one.

(function () {
  const { useState, useEffect, useMemo, useCallback } = window.React;

  const DEFAULT_PAGE_SIZE = 50;

  function _appendQuery(path, limit, offset, params) {
    const parts = ["limit=" + limit, "offset=" + offset];
    if (params && typeof params === "object") {
      for (const k of Object.keys(params)) {
        const v = params[k];
        if (v === null || v === undefined || v === "") continue;
        parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
      }
    }
    return path + (path.indexOf("?") >= 0 ? "&" : "?") + parts.join("&");
  }

  // usePagedList — owns limit/offset for a single server-paginated list and
  // wraps window.primerApi.useResource so callers keep the same polling,
  // dedupe and stale-while-error behaviour they already rely on.
  //
  // opts:
  //   key       cache key base (required, e.g. "agents:list")
  //   path      list path base (required, e.g. "/agents"); extra query allowed
  //   pageSize  rows per page (default 50, server caps at 200)
  //   pollMs    optional poll cadence forwarded to useResource
  //   params    optional extra query params object ({} filters/scopes)
  //   resetKey  when this changes, offset snaps back to page 0 (filters/chips)
  //
  // Returns: { items, total, data, loading, error, refetch, offset, pageSize,
  //            page, hasNext, hasPrev, rangeStart, rangeEnd, next, prev,
  //            reset, setOffset }.
  function usePagedList(opts) {
    const api = window.primerApi || {};
    const useResource = api.useResource;
    const apiFetch = api.apiFetch;
    const pageSize = opts.pageSize || DEFAULT_PAGE_SIZE;
    const params = opts.params || null;
    const paramsKey = params ? JSON.stringify(params) : "";
    const resetKey =
      opts.resetKey && typeof opts.resetKey === "object"
        ? JSON.stringify(opts.resetKey)
        : opts.resetKey;

    const [offset, setOffset] = useState(0);

    // Reset to the first page whenever the caller's filter/search/chip
    // selection changes. Skipped on first mount (offset already 0).
    useEffect(() => {
      setOffset(0);
    }, [resetKey, pageSize, paramsKey]);

    const url = useMemo(
      () => _appendQuery(opts.path, pageSize, offset, params),
      [opts.path, pageSize, offset, paramsKey] // eslint-disable-line
    );

    const res = useResource(
      opts.key,
      (signal) => apiFetch("GET", url, null, { signal }),
      {
        pollMs: opts.pollMs != null ? opts.pollMs : null,
        pauseWhile: opts.pauseWhile,
        deps: [offset, pageSize, paramsKey],
      }
    );

    const data = res.data;
    const items = data && Array.isArray(data.items) ? data.items : [];
    const total =
      data && typeof data.total === "number" ? data.total : null;

    const hasPrev = offset > 0;
    // total known → we're at the end once offset+returned reaches total.
    // total unknown → a full page implies there may be more behind it.
    const hasNext =
      total != null
        ? offset + items.length < total
        : items.length >= pageSize;

    const page = Math.floor(offset / pageSize);
    const rangeStart = items.length === 0 ? 0 : offset + 1;
    const rangeEnd = offset + items.length;

    const next = useCallback(() => {
      setOffset((o) => o + pageSize);
    }, [pageSize]);
    const prev = useCallback(() => {
      setOffset((o) => Math.max(0, o - pageSize));
    }, [pageSize]);
    const reset = useCallback(() => setOffset(0), []);

    return {
      items,
      total,
      data,
      loading: res.loading,
      error: res.error,
      refetch: res.refetch,
      offset,
      pageSize,
      page,
      hasNext,
      hasPrev,
      rangeStart,
      rangeEnd,
      next,
      prev,
      reset,
      setOffset,
    };
  }

  // Pager — Prev / range / Next control. Pass the object returned by
  // usePagedList as `pager`. Renders nothing when there is no data and no
  // navigation is possible, so single-request/empty pages stay clean.
  //
  // testids: pager (root), pager-range, pager-prev, pager-next.
  function Pager(props) {
    const p = props.pager;
    const label = props.label || "";
    const className = props.className || "";
    if (!p) return null;

    const count = p.items ? p.items.length : 0;
    // Nothing to show and nowhere to go — don't render an empty control.
    if (count === 0 && !p.hasPrev && !p.hasNext) return null;

    const rangeText =
      p.total != null
        ? p.rangeStart + "–" + p.rangeEnd + " of " + p.total
        : count === 0
          ? "0"
          : p.rangeStart + "–" + p.rangeEnd;

    return (
      <div className={"list-pager " + className} data-testid="pager">
        <span className="pager-range muted text-sm" data-testid="pager-range">
          {rangeText}
          {label ? " " + label : ""}
        </span>
        <div className="pager-controls">
          <Btn
            size="sm"
            kind="ghost"
            icon="chevron-left"
            disabled={!p.hasPrev || (p.loading && count === 0)}
            onClick={p.prev}
            data-testid="pager-prev"
          >
            Prev
          </Btn>
          <Btn
            size="sm"
            kind="ghost"
            iconRight="chevron-right"
            disabled={!p.hasNext || (p.loading && count === 0)}
            onClick={p.next}
            data-testid="pager-next"
          >
            Next
          </Btn>
        </div>
      </div>
    );
  }

  window.usePagedList = usePagedList;
  window.Pager = Pager;
  const ns = (window.primerApi = window.primerApi || {});
  ns.usePagedList = usePagedList;
  ns.Pager = Pager;
})();
