/* global React, Icon, Btn, Banner */

// In-UI documentation page. Two-column layout: left nav (260px) +
// fluid article + optional right TOC (200px). See
// docs/superpowers/specs/2026-06-04-user-documentation-system-design.md
// for the design.

const DOCS_CACHE_MANIFEST = "user-docs:manifest";

function _docsLazyMermaid() {
  if (typeof window === "undefined") return;
  if (window.mermaid) return;
  if (document.querySelector('script[data-vendor="mermaid"]')) return;
  const script = document.createElement("script");
  // Served by the console static handler at /console/vendor/...
  script.src = "/console/vendor/mermaid.min.js";
  script.async = true;
  script.dataset.vendor = "mermaid";
  script.onload = () => {
    if (window.mermaid && typeof window.mermaid.initialize === "function") {
      window.mermaid.initialize({
        startOnLoad: false,
        theme: "default",
        securityLevel: "strict",
      });
      window.dispatchEvent(new CustomEvent("mermaid:loaded"));
    }
  };
  document.head.appendChild(script);
}

function DocsPage({ section, slug, pushToast }) {
  const { useResource, apiFetch } = window.primerApi;
  const manifest = useResource(
    DOCS_CACHE_MANIFEST,
    (s) => apiFetch("GET", "/user_docs/manifest", null, { signal: s }),
    { pollMs: null },
  );

  React.useEffect(() => { _docsLazyMermaid(); }, []);

  const [searchQuery, setSearchQuery] = React.useState("");

  if (manifest.loading && !manifest.data) {
    return <div className="muted text-sm" style={{ padding: 24 }}>Loading docs...</div>;
  }
  if (manifest.error) {
    return (
      <Banner
        kind="error"
        title="Could not load docs manifest"
        detail={manifest.error.message || ""}
        actions={<Btn size="sm" icon="refresh" onClick={manifest.refetch}>Retry</Btn>}
      />
    );
  }

  const sections = manifest.data?.sections ?? [];

  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: "260px 1fr",
      height: "calc(100vh - 220px)",
      minHeight: 480,
      fontSize: 12.5,
    }}>
      <WSP_DocsLeftNav
        sections={sections}
        currentSection={section}
        currentSlug={slug}
        searchQuery={searchQuery}
        setSearchQuery={setSearchQuery}
      />
      <div style={{ overflow: "auto", padding: "0 24px", minHeight: 0 }}>
        <WSP_DocsArticle
          sections={sections}
          currentSection={section}
          currentSlug={slug}
          pushToast={pushToast}
        />
      </div>
    </div>
  );
}

function WSP_DocsLeftNav({ sections, currentSection, currentSlug, searchQuery, setSearchQuery }) {
  const { useRouter } = window.primerApi;
  const { navigate } = useRouter();
  const ql = (searchQuery || "").toLowerCase().trim();

  const filteredSections = React.useMemo(() => {
    if (!ql) return sections;
    return sections
      .map((sec) => {
        const titleHit = sec.title.toLowerCase().includes(ql);
        if (titleHit) return sec;
        const docs = (sec.docs || []).filter((d) => {
          const t = d.title.toLowerCase().includes(ql);
          const s = (d.summary || "").toLowerCase().includes(ql);
          const h = (d.headings || []).some((hh) => hh.text.toLowerCase().includes(ql));
          const g = (d.tags || []).some((tt) => tt.toLowerCase().includes(ql));
          return t || s || h || g;
        });
        return { ...sec, docs };
      })
      .filter((sec) => (sec.docs || []).length > 0);
  }, [sections, ql]);

  return (
    <div style={{
      borderRight: "1px solid var(--border)",
      overflow: "auto",
      padding: "14px 0",
      minHeight: 0,
    }}>
      <div style={{ padding: "0 12px 12px" }}>
        <input
          className="input"
          placeholder="Search docs..."
          value={searchQuery || ""}
          onChange={(e) => setSearchQuery(e.target.value)}
          style={{ width: "100%", fontSize: 12 }}
        />
      </div>
      {filteredSections.map((sec) => (
        <div key={sec.id} style={{ marginBottom: 8 }}>
          <div
            onClick={() => navigate(`/docs/${sec.id}`)}
            style={{
              padding: "4px 16px",
              fontSize: 11,
              fontWeight: 600,
              color: "var(--text-3)",
              textTransform: "uppercase",
              letterSpacing: "0.06em",
              cursor: "pointer",
            }}
          >
            <Icon name={sec.icon || "doc"} size={10} className="muted" style={{ marginRight: 6 }} />
            {sec.title}
          </div>
          {(sec.docs || []).map((doc) => {
            const isActive = doc.slug === `${currentSection}/${currentSlug}`;
            return (
              <a
                key={doc.slug}
                onClick={() => navigate(`/docs/${doc.slug}`)}
                style={{
                  display: "block",
                  padding: "5px 16px 5px 28px",
                  cursor: "pointer",
                  color: isActive ? "var(--accent)" : "var(--text-2)",
                  background: isActive ? "var(--bg-2)" : "transparent",
                  borderLeft: isActive
                    ? "2px solid var(--accent)"
                    : "2px solid transparent",
                  fontWeight: isActive ? 600 : 400,
                }}
              >
                {doc.title}
              </a>
            );
          })}
        </div>
      ))}
    </div>
  );
}

function WSP_DocsArticle({ sections, currentSection, currentSlug, pushToast }) {
  const { useResource, apiFetch, useRouter } = window.primerApi;
  const { navigate } = useRouter();

  // /docs/_ai/<slug> — AI-doc mirror render.
  if (currentSection === "_ai" && currentSlug) {
    return <WSP_AiDocMirror slug={currentSlug} pushToast={pushToast} />;
  }

  // /docs — pick the first doc.
  let effectiveSection = currentSection;
  let effectiveSlug = currentSlug;
  if (!effectiveSection && sections.length > 0) {
    const first = sections.find((s) => (s.docs || []).length > 0);
    if (first) {
      effectiveSection = first.id;
      effectiveSlug = first.docs[0].slug.split("/")[1];
    }
  }

  // /docs/<section> — section index card grid.
  if (effectiveSection && !effectiveSlug) {
    return <WSP_DocsSectionIndex section={effectiveSection} sections={sections} navigate={navigate} />;
  }

  if (!effectiveSection || !effectiveSlug) {
    return <div style={{ padding: 24 }} className="muted">No docs available.</div>;
  }

  const fullSlug = `${effectiveSection}/${effectiveSlug}`;
  return <WSP_DocsArticleBody fullSlug={fullSlug} pushToast={pushToast} />;
}

function WSP_DocsArticleBody({ fullSlug, pushToast }) {
  const { useResource, apiFetch } = window.primerApi;
  const doc = useResource(
    `user-docs:doc:${fullSlug}`,
    (s) => apiFetch("GET", `/user_docs/${fullSlug}`, null, { signal: s }),
    { deps: [fullSlug] },
  );

  const [activeAnchor, setActiveAnchor] = React.useState(null);

  React.useEffect(() => {
    const headings = doc.data?.headings ?? [];
    const onScroll = () => {
      let current = null;
      for (const h of headings) {
        const el = document.getElementById(h.anchor);
        if (!el) continue;
        const rect = el.getBoundingClientRect();
        if (rect.top < 120) current = h.anchor;
        else break;
      }
      setActiveAnchor(current);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, [doc.data && doc.data.headings]);

  React.useEffect(() => {
    if (!doc.data) return;
    const hash = window.location.hash.split("#").slice(-1)[0];
    if (!hash) return;
    const t = setTimeout(() => {
      const el = document.getElementById(hash);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 200);
    return () => clearTimeout(t);
  }, [doc.data && doc.data.slug]);

  if (doc.loading && !doc.data) {
    return <div style={{ padding: 24 }} className="muted">Loading...</div>;
  }
  if (doc.error) {
    return (
      <Banner
        kind="error"
        title={doc.error.title || "Could not load doc"}
        detail={doc.error.detail || doc.error.message || ""}
        actions={<Btn size="sm" icon="refresh" onClick={doc.refetch}>Retry</Btn>}
      />
    );
  }

  const data = doc.data || {};
  return (
    <div style={{ display: "flex", gap: 16, maxWidth: 1100, margin: "0 auto" }}>
      <article style={{ flex: 1, maxWidth: 760, padding: "24px 0" }}>
        <h1 style={{ margin: 0 }}>{data.title || fullSlug}</h1>
        {data.summary && (
          <div className="muted text-sm" style={{ marginTop: 6, marginBottom: 20 }}>
            {data.summary}
          </div>
        )}
        <div className="md-body">
          {typeof window.renderMarkdown === "function"
            ? window.renderMarkdown(data.source || "")
            : <pre>{data.source || ""}</pre>}
        </div>
      </article>
      <WSP_DocsRightToc headings={data.headings} activeAnchor={activeAnchor} />
    </div>
  );
}

function WSP_DocsRightToc({ headings, activeAnchor }) {
  if (!headings || headings.length < 3) return null;
  return (
    <aside style={{
      position: "sticky",
      top: 24,
      width: 200,
      paddingLeft: 16,
      fontSize: 11.5,
      borderLeft: "1px solid var(--border)",
      alignSelf: "flex-start",
    }}>
      <div style={{
        fontSize: 10,
        fontWeight: 600,
        textTransform: "uppercase",
        color: "var(--text-3)",
        letterSpacing: "0.06em",
        marginBottom: 8,
      }}>
        On this page
      </div>
      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {headings.map((h) => (
          <li key={h.anchor} style={{
            paddingLeft: h.level === 3 ? 12 : 0,
            margin: "4px 0",
          }}>
            <a
              href={`#${h.anchor}`}
              onClick={(e) => {
                e.preventDefault();
                const target = document.getElementById(h.anchor);
                if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
                window.location.hash = `#${h.anchor}`;
              }}
              style={{
                color: activeAnchor === h.anchor ? "var(--accent)" : "var(--text-2)",
                textDecoration: "none",
                fontWeight: activeAnchor === h.anchor ? 600 : 400,
              }}
            >
              {h.text}
            </a>
          </li>
        ))}
      </ul>
    </aside>
  );
}

function WSP_DocsSectionIndex({ section, sections, navigate }) {
  const sec = sections.find((s) => s.id === section);
  if (!sec) {
    return <div style={{ padding: 24 }} className="muted">Unknown section.</div>;
  }
  if (sec.id === "cookbook") {
    return <WSP_DocsCookbookIndex section={sec} navigate={navigate} />;
  }
  return (
    <div style={{ padding: "24px 0", maxWidth: 980, margin: "0 auto" }}>
      <h1 style={{ margin: 0 }}>{sec.title}</h1>
      <div className="muted text-sm" style={{ marginTop: 6, marginBottom: 20 }}>
        {(sec.docs || []).length} doc(s) in this section.
      </div>
      <div style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
        gap: 12,
      }}>
        {(sec.docs || []).map((doc) => (
          <div
            key={doc.slug}
            onClick={() => navigate(`/docs/${doc.slug}`)}
            style={{
              padding: "14px 16px",
              border: "1px solid var(--border)",
              borderRadius: 6,
              cursor: "pointer",
              background: "var(--bg)",
            }}
          >
            <div style={{ fontWeight: 600, fontSize: 14 }}>{doc.title}</div>
            <div className="muted text-sm" style={{ marginTop: 6 }}>
              {doc.summary}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function WSP_DocsCookbookIndex({ section, navigate }) {
  const allDocs = section.docs || [];
  const [difficulty, setDifficulty] = React.useState("");
  const [feature, setFeature] = React.useState("");
  const [textQ, setTextQ] = React.useState("");

  const allFeatures = React.useMemo(() => {
    const s = new Set();
    for (const d of allDocs) {
      for (const f of d.features || []) s.add(f);
    }
    return Array.from(s).sort();
  }, [allDocs]);

  const filtered = allDocs.filter((d) => {
    if (difficulty && d.difficulty !== difficulty) return false;
    if (feature && !(d.features || []).includes(feature)) return false;
    if (textQ) {
      const q = textQ.toLowerCase();
      const hit = d.title.toLowerCase().includes(q) || (d.summary || "").toLowerCase().includes(q);
      if (!hit) return false;
    }
    return true;
  });

  const order = { beginner: 0, intermediate: 1, advanced: 2 };
  const sorted = [...filtered].sort(
    (a, b) => (order[a.difficulty] ?? 9) - (order[b.difficulty] ?? 9),
  );

  return (
    <div style={{ padding: "24px 0", maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ margin: 0 }}>{section.title}</h1>
      <div className="muted text-sm" style={{ marginTop: 6, marginBottom: 16 }}>
        End-to-end recipes that combine multiple features.
      </div>
      <div style={{
        display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap",
        padding: "12px 14px", background: "var(--bg-2)",
        borderRadius: 6, marginBottom: 16,
      }}>
        <input
          className="input"
          placeholder="Filter..."
          value={textQ}
          onChange={(e) => setTextQ(e.target.value)}
          style={{ flex: 1, minWidth: 160, fontSize: 12 }}
        />
        <select className="select" value={difficulty} onChange={(e) => setDifficulty(e.target.value)} style={{ fontSize: 12 }}>
          <option value="">all difficulties</option>
          <option value="beginner">beginner</option>
          <option value="intermediate">intermediate</option>
          <option value="advanced">advanced</option>
        </select>
        {allFeatures.length > 0 && (
          <select className="select" value={feature} onChange={(e) => setFeature(e.target.value)} style={{ fontSize: 12 }}>
            <option value="">all features</option>
            {allFeatures.map((f) => <option key={f} value={f}>{f}</option>)}
          </select>
        )}
      </div>
      {sorted.length === 0 ? (
        <div className="muted" style={{ textAlign: "center", padding: 40 }}>
          No recipes match the current filters.
        </div>
      ) : (
        <div style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
          gap: 12,
        }}>
          {sorted.map((doc) => (
            <div
              key={doc.slug}
              onClick={() => navigate(`/docs/${doc.slug}`)}
              style={{
                padding: "14px 16px",
                border: "1px solid var(--border)",
                borderRadius: 6,
                cursor: "pointer",
                background: "var(--bg)",
              }}
            >
              <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 6 }}>
                {doc.title}
              </div>
              <div className="muted text-sm" style={{ marginBottom: 8 }}>
                {doc.summary}
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                {doc.difficulty && (
                  <span className="pill" style={{ fontSize: 10 }}>{doc.difficulty}</span>
                )}
                {doc.time_minutes && (
                  <span className="pill" style={{ fontSize: 10 }}>~{doc.time_minutes} min</span>
                )}
                {(doc.tags || []).slice(0, 3).map((t) => (
                  <span key={t} className="pill" style={{ fontSize: 10 }}>{t}</span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function WSP_AiDocMirror({ slug, pushToast }) {
  const { useResource, apiFetch } = window.primerApi;
  const aiDoc = useResource(
    `user-docs:_ai:${slug}`,
    (s) => apiFetch("GET", `/user_docs/_ai/${slug}`, null, { signal: s }),
    { deps: [slug] },
  );

  if (aiDoc.loading && !aiDoc.data) {
    return <div style={{ padding: 24 }} className="muted">Loading agent-facing doc...</div>;
  }
  if (aiDoc.error) {
    return (
      <Banner
        kind="error"
        title="Could not load agent-facing doc"
        detail={aiDoc.error.message || ""}
        actions={<Btn size="sm" icon="refresh" onClick={aiDoc.refetch}>Retry</Btn>}
      />
    );
  }
  const data = aiDoc.data || {};
  return (
    <div style={{
      background: "color-mix(in srgb, var(--violet) 4%, var(--bg))",
      minHeight: "100%",
    }}>
      <div style={{ maxWidth: 760, margin: "0 auto", padding: "24px 0" }}>
        <Banner
          kind="info"
          title="This is the agent-facing reference"
          detail="It's written for LLM agents reading via MCP, so the prose is terse and dense."
        />
        <article style={{ marginTop: 20 }}>
          <h1 style={{ margin: 0 }}>{data.title || slug}</h1>
          {data.summary && (
            <div className="muted text-sm" style={{ marginTop: 6, marginBottom: 20 }}>
              {data.summary}
            </div>
          )}
          <div className="md-body">
            {typeof window.renderMarkdown === "function"
              ? window.renderMarkdown(data.source || "")
              : <pre>{data.source || ""}</pre>}
          </div>
        </article>
      </div>
    </div>
  );
}

window.DocsPage = DocsPage;
