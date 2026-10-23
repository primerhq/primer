/* global React, window, Btn, Banner, PK_PageHeader, PK_CardGrid, PK_Card, PK_usePagedFilter, PK_Fold, PK_cardFacts */
// ============================================================================
// Unified provider catalog (Plan 01 kit switch-over, Plan 02 Task 20).
//
// Mount contract: props are exactly { initialClass, initialInstanceId, onNavigate }
// Navigation leaves through onNavigate as a structured ref.
// ============================================================================

const PROVIDER_CLASSES = window.PC_CLASSES;

function PC_ActiveWebSearchPanel() {
  const { apiFetch, useResource } = window.primerApi;

  const active = useResource(
    "catalog:active-web-search",
    (signal) => apiFetch("GET", "/web_search_active_config", null, { signal }),
    { pollMs: 0 },
  );
  const rows = useResource(
    "catalog:web-search-rows",
    (signal) => apiFetch("GET", "/web_search_providers?limit=200", null, { signal }),
    { pollMs: 0 },
  );

  const config = (active.data && active.data.config) || {};
  const mode = config.mode || "single";
  const items = (rows.data && rows.data.items) || [];

  const save = async (next) => {
    await apiFetch("PUT", "/web_search_active_config", { config: next });
    active.refetch();
  };

  const toggleMember = (id, on) => {
    const current = config.provider_ids || [];
    const next = on ? current.concat([id]) : current.filter((x) => x !== id);
    save({ mode: "aggregated", provider_ids: next });
  };

  return (
    <section className="catalog-defaults" data-testid="active-web-search-config">
      <h3>Active web search provider</h3>
      <label className="field">
        <span>Mode</span>
        <select
          data-testid="active-web-search-mode"
          value={mode}
          onChange={(e) =>
            save(
              e.target.value === "aggregated"
                ? { mode: "aggregated", provider_ids: config.provider_id ? [config.provider_id] : [] }
                : { mode: "single", provider_id: (config.provider_ids || [])[0] || "" },
            )
          }
        >
          <option value="single">single</option>
          <option value="aggregated">aggregated</option>
        </select>
      </label>
      {mode === "single" ? (
        <label className="field">
          <span>Provider</span>
          <select
            value={config.provider_id || ""}
            onChange={(e) => e.target.value && save({ mode: "single", provider_id: e.target.value })}
          >
            <option value="">(none)</option>
            {items.map((row) => (
              <option key={row.id} value={row.id}>{row.id}</option>
            ))}
          </select>
        </label>
      ) : (
        <div className="field" data-testid="active-web-search-members">
          <span>Providers (results are merged across all of them)</span>
          {items.map((row) => (
            <label key={row.id} className="check">
              <input
                type="checkbox"
                checked={(config.provider_ids || []).indexOf(row.id) >= 0}
                onChange={(e) => toggleMember(row.id, e.target.checked)}
              />
              <span className="mono">{row.id}</span>
            </label>
          ))}
        </div>
      )}
    </section>
  );
}

window.PC_TYPE_EXTRAS = window.PC_TYPE_EXTRAS || {};
window.PC_TYPE_EXTRAS.web_search = PC_ActiveWebSearchPanel;

function PC_cardFor(klass, row, ctx) {
  const isBuiltin = window.PC_isBuiltin(klass.key, row.id);
  const kind = row.provider || row.provider_type || row.backend;
  const kindLabel = window.PC_kindLabel(klass.key, kind);
  const subParts = [klass.label];
  if (kindLabel) subParts.push(kindLabel);
  const flavor = row.config && row.config.flavor;
  if (flavor && flavor !== "other") {
    const flavorLabel = (window.PC_FLAVOR_LABELS && window.PC_FLAVOR_LABELS[flavor]) || flavor;
    subParts.push(flavorLabel);
  }

  let status = null;
  if (klass.key === "llm" && row.last_probe_at != null) {
    status = row.last_probe_ok
      ? { label: "reachable", tone: "ok" }
      : { label: "unreachable", tone: "error" };
  }

  const rawFacts = [];
  if (klass.key === "llm") {
    const url = (row.config && (row.config.url || row.config.base_url)) || row.url;
    if (url) {
      try {
        rawFacts.push(new URL(url).host);
      } catch (e) {
        rawFacts.push(url);
      }
    }
    const count = (ctx.profileCounts && ctx.profileCounts[row.id]) || 0;
    rawFacts.push(window.PC_countOf ? window.PC_countOf(count, "profile", "profiles") : `${count} ${count === 1 ? "profile" : "profiles"}`);
    if (row.last_probe_at != null && !row.last_probe_ok && row.last_error) {
      rawFacts.push(String(row.last_error).slice(0, 80));
    }
  } else if (klass.key === "embedding" || klass.key === "cross_encoder") {
    const models = row.models || [];
    rawFacts.push(window.PC_countOf ? window.PC_countOf(models.length, "model", "models") : `${models.length} ${models.length === 1 ? "model" : "models"}`);
    if (models.length > 0) {
      const first = models[0];
      const firstName = typeof first === "string" ? first : (first && first.name) || "";
      if (firstName) rawFacts.push(firstName);
    }
  } else if (klass.key === "ssp") {
    const cfg = row.config || {};
    if (cfg.path) {
      rawFacts.push(String(cfg.path));
    } else if (cfg.hostname && cfg.database) {
      rawFacts.push(`${cfg.hostname}/${cfg.database}`);
    } else if (cfg.hostname) {
      rawFacts.push(String(cfg.hostname));
    }
  } else if (klass.key === "workspace") {
    const cfg = row.config || {};
    const k = row.provider || "";
    if (k === "local") {
      rawFacts.push(cfg.root_path || cfg.path || "");
    } else if (k === "container") {
      rawFacts.push(cfg.runtime || "");
    } else if (k === "kubernetes") {
      rawFacts.push(`namespace ${cfg.namespace || "default"}`);
    }
  } else if (klass.key === "web_search") {
    if (isBuiltin) rawFacts.push("cannot be deleted");
  } else if (klass.key === "channel") {
    // channel has no facts
  } else if (klass.key === "artifact_storage") {
    const cfg = row.config || {};
    if (cfg.root) rawFacts.push(String(cfg.root));
    else if (cfg.bucket) rawFacts.push(String(cfg.bucket));
  } else if (klass.key === "tts") {
    if (row.default_model) rawFacts.push(`model ${row.default_model}`);
    if (row.default_voice) rawFacts.push(`voice ${row.default_voice}`);
  } else if (klass.key === "stt") {
    if (row.default_model) rawFacts.push(`model ${row.default_model}`);
  }

  const menu = isBuiltin ? null : [
    {
      label: "Delete",
      danger: true,
      testid: `pc-card-delete:${klass.key}:${row.id}`,
      onClick: () => ctx.onDelete(klass, row),
    },
  ];

  return {
    id: `${klass.key}:${row.id}`,
    icon: { path: klass.iconPath },
    title: row.id,
    subtitle: subParts.join(" · "),
    badge: isBuiltin ? "built in" : null,
    status: status,
    facts: rawFacts,
    menu: menu,
    onOpen: () => ctx.onOpen(klass, row),
  };
}

function resolveClassKey(initialClass) {
  if (initialClass && window.PC_CLASSES && window.PC_CLASSES.some((c) => c.nav && c.key === initialClass)) {
    return initialClass;
  }
  return "all";
}

function ProviderCatalog({ initialClass, initialInstanceId, onNavigate }) {
  const { apiFetch, useViewport, toastPush } = window.primerApi || {};
  const viewport = typeof useViewport === "function" ? useViewport() : { isMobile: false };
  const isMobile = !!(viewport && viewport.isMobile);

  const [classKey, setClassKey] = React.useState(() => resolveClassKey(initialClass));
  const [instanceId, setInstanceId] = React.useState(initialInstanceId || null);
  const [reloadKey, setReloadKey] = React.useState(0);
  const [loading, setLoading] = React.useState(true);
  const [classData, setClassData] = React.useState({});
  const [classErrors, setClassErrors] = React.useState({});
  const [profileCounts, setProfileCounts] = React.useState({});

  const [overlayState, setOverlayState] = React.useState({
    open: false,
    initialClass: null,
    editRow: null,
  });
  // PC_RegisterOverlay reports a successful save and then reports its close.
  // A panel-class save uses the first callback to open the created provider's
  // detail; the following close must not replace that navigation with the list.
  const savedPanelRef = React.useRef(false);

  const refetch = React.useCallback(() => {
    setReloadKey((k) => k + 1);
  }, []);

  React.useEffect(() => {
    setClassKey(resolveClassKey(initialClass));
  }, [initialClass]);

  React.useEffect(() => {
    setInstanceId(initialInstanceId || null);
  }, [initialInstanceId]);

  const classesToFetch = React.useMemo(() => {
    if (!window.PC_CLASSES) return [];
    if (classKey === "all") {
      return window.PC_CLASSES.filter((c) => c.nav);
    }
    const found = window.PC_classByKey ? window.PC_classByKey(classKey) : window.PC_CLASSES.find((c) => c.key === classKey);
    return found ? [found] : [];
  }, [classKey]);

  React.useEffect(() => {
    let cancelled = false;
    setLoading(true);

    const fetches = classesToFetch.map(async (c) => {
      try {
        const res = await apiFetch("GET", `/${c.plural}?limit=200`);
        return { key: c.key, items: (res && res.items) || [], error: null };
      } catch (err) {
        return { key: c.key, items: [], error: err };
      }
    });

    const hasLlm = classesToFetch.some((c) => c.key === "llm");
    const profilesPromise = hasLlm
      ? apiFetch("GET", "/model_profiles?limit=200").catch(() => ({ items: [] }))
      : Promise.resolve({ items: [] });

    Promise.all([Promise.all(fetches), profilesPromise]).then(([results, profRes]) => {
      if (cancelled) return;
      const nextData = {};
      const nextErrors = {};
      results.forEach((r) => {
        nextData[r.key] = r.items;
        if (r.error) nextErrors[r.key] = r.error;
      });
      const counts = {};
      ((profRes && profRes.items) || []).forEach((p) => {
        if (p.provider_id) {
          counts[p.provider_id] = (counts[p.provider_id] || 0) + 1;
        }
      });
      setClassData(nextData);
      setClassErrors(nextErrors);
      setProfileCounts(counts);
      setLoading(false);
    });

    return () => {
      cancelled = true;
    };
  }, [classesToFetch, reloadKey, apiFetch]);

  const allRows = React.useMemo(() => {
    const rows = [];
    classesToFetch.forEach((c) => {
      const items = classData[c.key] || [];
      items.forEach((item) => {
        rows.push({
          key: `${c.key}:${item.id}`,
          klass: c,
          row: item,
        });
      });
    });
    return rows;
  }, [classesToFetch, classData]);

  // Deep-link initial instance resolution once loaded
  React.useEffect(() => {
    if (!instanceId || loading) return;
    const item = allRows.find((r) => r.row.id === instanceId);
    if (!item) return;
    if (item.klass.form !== "panel") {
      setOverlayState({
        open: true,
        initialClass: item.klass.key,
        editRow: item.row,
      });
    }
  }, [instanceId, loading, allRows]);

  // Command palette / register request listener
  React.useEffect(() => {
    if (typeof window.PC_useRegisterRequest === "function") {
      const take = (requestedKey) => {
        const target = requestedKey || (classKey === "all" ? "llm" : classKey);
        setOverlayState({ open: true, initialClass: target, editRow: null });
      };
      if (window.PC_REGISTER_REQUESTS && Array.isArray(window.PC_REGISTER_REQUESTS.takers)) {
        window.PC_REGISTER_REQUESTS.takers.push(take);
        if (window.PC_REGISTER_REQUESTS.pending) {
          const p = window.PC_REGISTER_REQUESTS.pending;
          window.PC_REGISTER_REQUESTS.pending = null;
          take(p.classKey);
        }
        return () => {
          const idx = window.PC_REGISTER_REQUESTS.takers.indexOf(take);
          if (idx >= 0) window.PC_REGISTER_REQUESTS.takers.splice(idx, 1);
        };
      }
    }
  }, [classKey]);

  const openRegister = (targetKey) => {
    setOverlayState({
      open: true,
      initialClass: targetKey || (classKey === "all" ? "llm" : classKey),
      editRow: null,
    });
  };

  const closeOverlay = () => {
    if (savedPanelRef.current) {
      savedPanelRef.current = false;
      return;
    }
    setOverlayState({ open: false, initialClass: null, editRow: null });
    setInstanceId(null);
    refetch();
    if (typeof onNavigate === "function") {
      onNavigate({ kind: "provider-class", classKey: classKey });
    }
  };

  const openProvider = (k, r) => {
    if (typeof onNavigate === "function") {
      onNavigate({ kind: "provider-instance", classKey: k.key, id: r.id });
    }
    if (k.form === "panel") {
      setInstanceId(r.id);
    } else {
      setOverlayState({
        open: true,
        initialClass: k.key,
        editRow: r,
      });
    }
  };

  const deleteProvider = async (klass, row) => {
    if (typeof window.confirmDialog !== "function") return;
    const ok = await window.confirmDialog({
      title: `Delete ${row.id}?`,
      message: `This removes the ${klass.noun} provider. Anything that uses it stops working.`,
      confirmLabel: "Delete provider",
      danger: true,
    });
    if (!ok) return;

    try {
      await apiFetch("DELETE", `/${klass.plural}/${encodeURIComponent(row.id)}`);
      if (typeof toastPush === "function") {
        toastPush({ kind: "success", title: "Provider deleted", detail: row.id });
      }
      refetch();
    } catch (err) {
      const refusal = (window.primerApi && window.primerApi.readRefusal && window.primerApi.readRefusal(err)) || {
        message: err.message || "Delete failed",
      };
      if (typeof toastPush === "function") {
        toastPush({ kind: "error", title: "Delete refused", detail: refusal.message });
      }
    }
  };

  const cardCtx = React.useMemo(() => ({
    profileCounts: profileCounts,
    onOpen: openProvider,
    onDelete: deleteProvider,
  }), [profileCounts]);

  const paged = PK_usePagedFilter(allRows, {
    pageSize: 12,
    text: (r) => [r.row.id, r.klass.label, window.PC_kindLabel ? window.PC_kindLabel(r.klass.key, r.row.provider || r.row.provider_type || r.row.backend) : ""].join(" "),
  });

  const activeKlass = classKey !== "all" ? (window.PC_classByKey ? window.PC_classByKey(classKey) : (window.PC_CLASSES || []).find((c) => c.key === classKey)) : null;

  const savedFromOverlay = (row) => {
    if (activeKlass && activeKlass.form === "panel" && row && row.id) {
      savedPanelRef.current = true;
      setOverlayState({ open: false, initialClass: null, editRow: null });
      refetch();
      openProvider(activeKlass, row);
      return;
    }
    closeOverlay();
  };

  // Panel class inline detail
  if (instanceId && activeKlass && activeKlass.form === "panel") {
    const klass = activeKlass;
    const Detail = klass.key === "ssp" ? window.SSPDetail : klass.key === "workspace" ? window.WorkspaceProviderDetail : klass.key === "channel" ? window.ChannelProviderDetail : (klass.detail ? klass.detail() : null);
    const detailProps = {
      pushToast: toastPush,
      onBack: () => {
        setInstanceId(null);
        if (typeof onNavigate === "function") {
          onNavigate({ kind: "provider-class", classKey: classKey });
        }
      },
      onDeleted: () => {
        setInstanceId(null);
        if (typeof onNavigate === "function") {
          onNavigate({ kind: "provider-class", classKey: classKey });
        }
        refetch();
      },
    };
    detailProps[klass.detailProp || "providerId"] = instanceId;
    return (
      <div data-testid={`provider-body-${classKey}`}>
        {Detail ? <Detail {...detailProps} /> : null}
      </div>
    );
  }

  const totalCount = allRows.length;
  const headerTitle = classKey === "all" ? "Providers" : `${(activeKlass && activeKlass.label) || ""} providers`;
  const headerCount = `${totalCount} ${totalCount === 1 ? "provider" : "providers"}`;

  const ExtraComponent = classKey !== "all" && window.PC_TYPE_EXTRAS ? window.PC_TYPE_EXTRAS[classKey] : null;
  const classRows = classKey !== "all" ? (classData[classKey] || []) : [];
  const PC_RegisterOverlay = window.PC_RegisterOverlay;

  return (
    <div className="col pc-catalog" style={{ gap: 16 }}>
      <PK_PageHeader
        title={headerTitle}
        count={headerCount}
        filter={{
          value: paged.q,
          onChange: paged.setQ,
          label: "Filter providers",
          testid: "pc-filter",
        }}
        pager={{ ...paged, onPage: paged.setPage }}
        action={{
          label: "+ Register",
          onClick: () => openRegister(classKey === "all" ? "llm" : classKey),
          testid: "pc-register-open",
        }}
      />

      {ExtraComponent ? (
        <div data-testid={`pc-extras:${classKey}`}>
          {isMobile ? (
            <PK_Fold label="Defaults">
              <ExtraComponent rows={classRows} reload={refetch} />
            </PK_Fold>
          ) : (
            <ExtraComponent rows={classRows} reload={refetch} />
          )}
        </div>
      ) : null}

      <div data-testid={`provider-body-${classKey}`}>
        {classesToFetch.map((c) => {
          const err = classErrors[c.key];
          if (!err) return null;
          return (
            <div key={`err-${c.key}`} data-testid={`pc-error:${c.key}`} style={{ marginBottom: 12 }}>
              <Banner
                kind="error"
                title={`Could not load ${c.label} providers`}
                detail={window.primerApi.readRefusal(err, err && err.message).message}
                actions={
                  <Btn size="sm" icon="refresh" onClick={refetch}>Retry</Btn>
                }
              />
            </div>
          );
        })}

        {classesToFetch.map((c) => {
          const items = classData[c.key] || [];
          if (items.length < 200) return null;
          return (
            <div key={`trunc-${c.key}`} className="pc-truncated text-sm muted" data-testid={`pc-truncated:${c.key}`} style={{ marginBottom: 8 }}>
              Showing the first 200 {c.label} providers
            </div>
          );
        })}

        {loading && totalCount === 0 && Object.keys(classErrors).length === 0 ? (
          <div className="pc-loading" role="status" data-testid="pc-loading">
            Loading providers…
          </div>
        ) : totalCount === 0 ? (
          <div className="pc-empty" data-testid={`pc-empty:${classKey}`}>
            No {classKey === "all" ? "providers registered" : `${(activeKlass && activeKlass.label) || ""} providers`} yet. Use + Register to add one.
          </div>
        ) : paged.visible.length === 0 ? (
          <div className="pc-filter-empty" data-testid="pc-filter-empty">
            No providers match "{paged.q}".
          </div>
        ) : (
          <PK_CardGrid testid="pc-cards">
            {paged.visible.map((item) => (
              <PK_Card key={item.key} {...PC_cardFor(item.klass, item.row, cardCtx)} />
            ))}
          </PK_CardGrid>
        )}
      </div>

      {overlayState.open && typeof PC_RegisterOverlay === "function" ? (
        <PC_RegisterOverlay
          initialClass={overlayState.initialClass}
          allowedClasses={classKey === "all" ? undefined : [classKey]}
          editRow={overlayState.editRow}
          onClose={closeOverlay}
          onSaved={savedFromOverlay}
          onDeleted={() => {
            closeOverlay();
          }}
        />
      ) : null}
    </div>
  );
}

window.PROVIDER_CLASSES = PROVIDER_CLASSES;
window.ProviderCatalog = ProviderCatalog;
window.PC_cardFor = PC_cardFor;
