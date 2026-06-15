/* Primer Docs static site - directive client behaviours.
 *
 * The static site is multi-page (one index.html per doc), not the SPA
 * mockup, so this only enhances the directives the build emits:
 *   - code-tabs widgets (.tabs/.tab/.tab-panel), ported from the mockup
 *     docs.js wireTabs() helper;
 *   - mermaid diagrams (<pre class="mermaid">), rendered client-side;
 *   - theme toggle, kept in sync with localStorage.
 */
(function () {
  "use strict";

  // ---- code-tabs (ported from the mockup docs.js wireTabs) -----------
  function wireTabs() {
    document.querySelectorAll(".tabs").forEach(function (tabs) {
      tabs.querySelectorAll(".tab").forEach(function (tab) {
        tab.addEventListener("click", function () {
          var target = tab.dataset.tab;
          tabs.querySelectorAll(".tab").forEach(function (t) {
            t.classList.toggle("active", t === tab);
          });
          tabs.querySelectorAll(".tab-panel").forEach(function (p) {
            p.classList.toggle("active", p.id === target);
          });
        });
      });
    });
  }

  // ---- mermaid -------------------------------------------------------
  // The pinned mermaid script (see page.html) is loaded with startOnLoad
  // off; render every <pre class="mermaid"> once the DOM and library are
  // ready, picking the mermaid theme from the document's data-theme.
  function runMermaid() {
    if (!window.mermaid || !document.querySelector("pre.mermaid")) return;
    var theme =
      document.documentElement.getAttribute("data-theme") === "light"
        ? "default"
        : "dark";
    try {
      window.mermaid.initialize({ startOnLoad: false, theme: theme });
      window.mermaid.run({ querySelector: "pre.mermaid" });
    } catch (_e) {
      /* leave the source visible if rendering fails */
    }
  }

  // ---- theme toggle --------------------------------------------------
  function wireTheme() {
    var toggle = document.getElementById("themeToggle");
    var saved = localStorage.getItem("primer-docs-theme");
    if (saved) document.documentElement.setAttribute("data-theme", saved);
    if (!toggle) return;
    toggle.addEventListener("click", function () {
      var cur = document.documentElement.getAttribute("data-theme");
      var next = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("primer-docs-theme", next);
      runMermaid();
    });
  }

  function init() {
    wireTabs();
    wireTheme();
    runMermaid();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
