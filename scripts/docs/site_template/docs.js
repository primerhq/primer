/* Primer Docs static site - directive client behaviours.
 *
 * The static site is multi-page (one index.html per doc), not the SPA
 * mockup, so this only enhances the directives the build emits:
 *   - code-tabs widgets (.tabs/.tab/.tab-panel), ported from the mockup
 *     docs.js wireTabs() helper;
 *   - mermaid diagrams (<pre class="mermaid">), rendered client-side;
 *   - theme toggle, kept in sync with localStorage;
 *   - client search over the prebuilt /search-index.json (a results
 *     dropdown under the topbar search box), with the mockup's
 *     "/"-to-focus and Escape-to-clear keybindings.
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

  // ---- client search -------------------------------------------------
  // Fetch the prebuilt index once, match the query against title +
  // headings + excerpt (case-insensitive), and render a small results
  // dropdown under the topbar search box. Degrades to a no-op while the
  // index is still loading (or if the fetch fails).
  var SEARCH_MAX = 8;

  function injectSearchStyles() {
    if (document.getElementById("docs-search-styles")) return;
    var css =
      ".topbar-search { position: relative; }" +
      ".search-results {" +
      "  position: absolute; top: calc(100% + 6px); left: 0; right: 0;" +
      "  z-index: 50; max-height: 60vh; overflow-y: auto;" +
      "  background: var(--bg-2); border: 1px solid var(--border);" +
      "  border-radius: 8px; padding: 4px;" +
      "  box-shadow: 0 8px 24px rgba(0,0,0,0.35);" +
      "}" +
      ".search-results[hidden] { display: none; }" +
      ".search-result {" +
      "  display: block; padding: 8px 10px; border-radius: 6px;" +
      "  text-decoration: none; color: var(--text);" +
      "}" +
      ".search-result:hover, .search-result.active { background: var(--bg); }" +
      ".search-result .sr-title { font-size: 13px; font-weight: 600; }" +
      ".search-result .sr-section {" +
      "  font-size: 11px; color: var(--text-3); text-transform: uppercase;" +
      "  letter-spacing: 0.04em;" +
      "}" +
      ".search-empty { padding: 10px; font-size: 13px; color: var(--text-3); }";
    var style = document.createElement("style");
    style.id = "docs-search-styles";
    style.textContent = css;
    document.head.appendChild(style);
  }

  function wireSearch() {
    var search = document.getElementById("search");
    if (!search) return;

    injectSearchStyles();

    var box = search.closest(".topbar-search") || search.parentElement;
    var results = document.createElement("div");
    results.className = "search-results";
    results.hidden = true;
    box.appendChild(results);

    var index = null;
    fetch("/search-index.json")
      .then(function (r) {
        return r.ok ? r.json() : [];
      })
      .then(function (data) {
        index = Array.isArray(data) ? data : [];
      })
      .catch(function () {
        index = [];
      });

    function hide() {
      results.hidden = true;
      results.innerHTML = "";
    }

    function escapeHtml(s) {
      return String(s).replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    }

    function matches(entry, q) {
      var hay = (
        entry.title +
        " " +
        (entry.headings || []).join(" ") +
        " " +
        (entry.excerpt || "")
      ).toLowerCase();
      return hay.indexOf(q) !== -1;
    }

    function render(q) {
      if (!q) {
        hide();
        return;
      }
      if (index === null) {
        // Index not loaded yet: leave the box closed rather than guess.
        hide();
        return;
      }
      var hits = index.filter(function (e) {
        return matches(e, q);
      });
      if (hits.length === 0) {
        results.innerHTML = '<div class="search-empty">No matches</div>';
        results.hidden = false;
        return;
      }
      results.innerHTML = hits
        .slice(0, SEARCH_MAX)
        .map(function (e) {
          return (
            '<a class="search-result" href="' +
            escapeHtml(e.url) +
            '">' +
            '<div class="sr-section">' +
            escapeHtml(e.section) +
            "</div>" +
            '<div class="sr-title">' +
            escapeHtml(e.title) +
            "</div>" +
            "</a>"
          );
        })
        .join("");
      results.hidden = false;
    }

    search.addEventListener("input", function () {
      render(search.value.trim().toLowerCase());
    });

    // Close on outside click; keep open when interacting within the box.
    document.addEventListener("click", function (e) {
      if (!box.contains(e.target)) hide();
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "/" && document.activeElement !== search) {
        e.preventDefault();
        search.focus();
      } else if (e.key === "Escape") {
        search.value = "";
        hide();
        search.blur();
      }
    });
  }

  function init() {
    wireTabs();
    wireTheme();
    wireSearch();
    runMermaid();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
