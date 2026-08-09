"""The served gateway client (/svc/_client/primer.js).

Phases 1-2 expose functions only; ``Primer.tool`` arrives with the tool
dispatch in phase 3. The client resolves gateway URLs RELATIVE to the
page (``/svc/{name}/``), so a bundle needs no configuration to call its
own service.
"""

PRIMER_JS = """\
// primer.js - service gateway client (functions only for now).
(function () {
  "use strict";
  window.Primer = {
    async fn(name, args) {
      const url = new URL(
        "_gateway/functions/" + encodeURIComponent(name),
        window.location.href
      );
      const res = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(args || {}),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        const err = new Error(
          (body && (body.message || body.title || body.detail)) || res.statusText
        );
        err.status = res.status;
        err.problem = body;
        throw err;
      }
      return body;
    },
  };
})();
"""

__all__ = ["PRIMER_JS"]
