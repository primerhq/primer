var WEB_KINDS = {
  search: {
    title: "Web search",
    testid: "web-search",
    noun: "provider",
    builtinId: "DuckDuckGo",
    activePath: "/web_search_active_config",
    listPath: "/web_search_providers?limit=200",
    types: { duckduckgo: "DuckDuckGo", tavily: "Tavily", firecrawl: "Firecrawl", exa: "Exa" },
    defaultLabel: "Default provider",
    defaultHelp: "Every web search goes to this provider.",
    orderLabel: "Provider order",
    orderHelp: "Providers are tried in this order. The next one is used when one fails.",
    loading: "Loading web search settings\u2026",
    loadError: "Web search settings did not load",
    saved: "Web search updated",
    notSaved: "Web search not saved",
  },
  fetch: {
    title: "Web fetch",
    testid: "web-fetch",
    noun: "fetcher",
    builtinId: "local",
    activePath: "/web_fetch_active_config",
    listPath: "/web_fetch_providers?limit=200",
    types: { local: "Local", jina: "Jina Reader", firecrawl: "Firecrawl", exa: "Exa" },
    defaultLabel: "Default fetcher",
    defaultHelp: "Every page fetch goes to this fetcher.",
    orderLabel: "Fetcher order",
    orderHelp: "Fetchers are tried in this order. The next one is used when one fails or returns too little text.",
    loading: "Loading web fetch settings\u2026",
    loadError: "Web fetch settings did not load",
    saved: "Web fetch updated",
    notSaved: "Web fetch not saved",
  },
};

function WEB_typeLabel(kind, providerType) {
  if (!providerType) return "";
  var k = WEB_KINDS[kind];
  if (k && k.types) {
    if (k.types[providerType]) return k.types[providerType];
    var lower = String(providerType).toLowerCase();
    if (k.types[lower]) return k.types[lower];
  }
  return String(providerType);
}

function WEB_optionLabel(kind, row) {
  if (!row) return "";
  if (row.missing) {
    return row.id + " (missing)";
  }
  var k = WEB_KINDS[kind];
  if (k && row.id === k.builtinId) {
    return (kind === "fetch" ? "Local" : "DuckDuckGo") + " (built in)";
  }
  var type = WEB_typeLabel(kind, row.provider_type || (row.config && row.config.type));
  return row.id + (type ? " (" + type + ")" : "");
}

function WEB_sortProviders(kind, rows) {
  if (!rows || !Array.isArray(rows)) return [];
  var builtin = WEB_KINDS[kind] ? WEB_KINDS[kind].builtinId : null;
  return rows.slice().sort(function (a, b) {
    var aBuiltin = a.id === builtin;
    var bBuiltin = b.id === builtin;
    if (aBuiltin && !bBuiltin) return -1;
    if (!aBuiltin && bBuiltin) return 1;
    return a.id.localeCompare(b.id);
  });
}

function WEB_switchMode(config, mode, remembered) {
  if (!config || config.mode === mode) return config;
  if (mode === "aggregated") {
    var currentId = config.provider_id || (config.provider_ids && config.provider_ids[0]) || "";
    var ids = currentId ? [currentId] : [];
    if (Array.isArray(remembered)) {
      for (var i = 0; i < remembered.length; i++) {
        var rid = remembered[i];
        if (rid && ids.indexOf(rid) === -1) {
          ids.push(rid);
        }
      }
    }
    return { mode: "aggregated", provider_ids: ids };
  }
  if (mode === "single") {
    var firstId = (config.provider_ids && config.provider_ids[0]) || config.provider_id || "";
    return { mode: "single", provider_id: firstId };
  }
  return config;
}

function WEB_toggleMember(config, id) {
  if (!config || config.mode !== "aggregated") return config;
  var list = Array.isArray(config.provider_ids) ? config.provider_ids.slice() : [];
  var idx = list.indexOf(id);
  if (idx >= 0) {
    if (list.length <= 1) {
      return null;
    }
    list.splice(idx, 1);
  } else {
    list.push(id);
  }
  return { mode: "aggregated", provider_ids: list };
}

function WEB_moveMember(config, id, delta) {
  if (!config || config.mode !== "aggregated") return config;
  var list = Array.isArray(config.provider_ids) ? config.provider_ids.slice() : [];
  var idx = list.indexOf(id);
  if (idx < 0) return config;
  var target = idx + delta;
  if (target < 0 || target >= list.length) return config;
  var temp = list[idx];
  list[idx] = list[target];
  list[target] = temp;
  return { mode: "aggregated", provider_ids: list };
}

function WEB_chainRows(kind, config, rows) {
  var out = [];
  var list = (config && Array.isArray(config.provider_ids)) ? config.provider_ids : [];
  var rowList = Array.isArray(rows) ? rows : [];
  var byId = {};
  for (var i = 0; i < rowList.length; i++) {
    byId[rowList[i].id] = rowList[i];
  }
  var seen = {};
  for (var j = 0; j < list.length; j++) {
    var id = list[j];
    seen[id] = true;
    var row = byId[id];
    if (row) {
      out.push({
        key: id,
        label: WEB_optionLabel(kind, row),
        checked: true,
        missing: false,
      });
    } else {
      out.push({
        key: id,
        label: id + " (missing)",
        checked: true,
        missing: true,
      });
    }
  }
  var remaining = [];
  for (var k = 0; k < rowList.length; k++) {
    if (!seen[rowList[k].id]) {
      remaining.push(rowList[k]);
    }
  }
  remaining.sort(function (a, b) {
    return a.id.localeCompare(b.id);
  });
  for (var m = 0; m < remaining.length; m++) {
    out.push({
      key: remaining[m].id,
      label: WEB_optionLabel(kind, remaining[m]),
      checked: false,
      missing: false,
    });
  }
  return out;
}

function WEB_summary(kind, config, rows) {
  if (!config) return "";
  var rowList = Array.isArray(rows) ? rows : [];
  var byId = {};
  for (var i = 0; i < rowList.length; i++) {
    byId[rowList[i].id] = rowList[i];
  }
  if (config.mode === "single") {
    var pid = config.provider_id;
    var row = byId[pid] || { id: pid, missing: true };
    return "Single: " + WEB_optionLabel(kind, row);
  }
  if (config.mode === "aggregated") {
    var ids = Array.isArray(config.provider_ids) ? config.provider_ids : [];
    var labels = [];
    for (var j = 0; j < ids.length; j++) {
      var id = ids[j];
      var r = byId[id] || { id: id, missing: true };
      labels.push(WEB_optionLabel(kind, r));
    }
    return "Aggregated: " + labels.join(" \u203a ");
  }
  return "";
}

function WEB_refusalText(err, fallback) {
  var env = err && err.envelope;
  var ext = (env && env.extensions) || {};
  var det = (env && env.detail && typeof env.detail === "object") ? env.detail : {};
  var code = ext.error || ext.code || det.error || det.code || (err && err.code);
  if (code === "unknown_provider_ids" || code === "unknown_fetcher_ids") {
    var ids = ext.unknown_ids || det.unknown_ids || [];
    return "No longer exists: " + ids.join(", ") + ". The list has been reloaded.";
  }
  if (window.primerApi && typeof window.primerApi.readRefusal === "function") {
    return window.primerApi.readRefusal(err, fallback).message;
  }
  return fallback || "An error occurred";
}

function WEB_ActiveConfigSection(props) {
  var kind = props.kind || "search";
  var active = props.active;
  var providers = props.providers;
  var cols = props.cols !== undefined ? props.cols : 2;
  var onManageProviders = props.onManageProviders;
  var children = props.children;

  var [saving, setSaving] = React.useState(false);
  var [fieldError, setFieldError] = React.useState(null);
  var [remembered, setRemembered] = React.useState([]);
  var savingRef = React.useRef(false);

  var info = WEB_KINDS[kind] || WEB_KINDS.search;
  var t = info.testid;

  var cfg = (active && active.data && active.data.config) ? active.data.config : (active && active.data);
  var providerRows = (providers && providers.data) ? (Array.isArray(providers.data) ? providers.data : (providers.data.items || [])) : [];

  React.useEffect(function () {
    if (cfg && cfg.mode === "aggregated" && Array.isArray(cfg.provider_ids)) {
      setRemembered(cfg.provider_ids);
    }
  }, [cfg]);

  var isLoading = (!active || (!active.data && !active.error)) || (!providers || (!providers.data && !providers.error));
  var hasError = (active && active.error && !active.data) || (providers && providers.error && !providers.data);

  if (isLoading) {
    return (
      <div className="muted" data-testid={t + "-loading"}>
        {info.loading}
      </div>
    );
  }

  if (hasError) {
    var err = (active && active.error) || (providers && providers.error);
    var detailMsg = "";
    if (window.primerApi && typeof window.primerApi.readRefusal === "function") {
      detailMsg = window.primerApi.readRefusal(err, "Load failed").message;
    } else if (err && err.message) {
      detailMsg = err.message;
    }
    var isRetrying = !!((active && active.loading) || (providers && providers.loading));
    return (
      <div data-testid={t + "-error"}>
        <Banner
          kind="error"
          title={info.loadError}
          detail={detailMsg}
          actions={
            <Btn
              data-testid={t + "-retry"}
              busy={isRetrying}
              onClick={function () {
                if (active && typeof active.refetch === "function") active.refetch();
                if (providers && typeof providers.refetch === "function") providers.refetch();
              }}
            >
              Retry
            </Btn>
          }
        />
      </div>
    );
  }

  function doSave(nextConfig) {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setFieldError(null);

    window.primerApi.apiFetch("PUT", info.activePath, { config: nextConfig })
      .then(function () {
        savingRef.current = false;
        setSaving(false);
        setFieldError(null);
        if (window.primerApi && typeof window.primerApi.toastPush === "function") {
          window.primerApi.toastPush({
            kind: "success",
            title: info.saved,
            detail: WEB_summary(kind, nextConfig, providerRows),
          });
        }
        if (active && typeof active.refetch === "function") active.refetch();
      })
      .catch(function (err) {
        savingRef.current = false;
        setSaving(false);
        var text = WEB_refusalText(err, info.notSaved);
        setFieldError(text);
        if (window.primerApi && typeof window.primerApi.toastPush === "function") {
          var reqId = (err && (err.requestId || (err.envelope && err.envelope.extensions && err.envelope.extensions.request_id))) || null;
          window.primerApi.toastPush({
            kind: "error",
            title: info.notSaved,
            detail: text,
            requestId: reqId,
          });
        }
        if (active && typeof active.refetch === "function") active.refetch();
        if (providers && typeof providers.refetch === "function") providers.refetch();
      });
  }

  function handleModeChange(newMode) {
    if (newMode === (cfg ? cfg.mode : "single")) return;
    var rowIds = providerRows.map(function (r) { return r.id; });
    var validRemembered = [];
    for (var i = 0; i < remembered.length; i++) {
      if (rowIds.indexOf(remembered[i]) >= 0) {
        validRemembered.push(remembered[i]);
      }
    }
    var nextCfg = WEB_switchMode(cfg, newMode, validRemembered);
    doSave(nextCfg);
  }

  function handleDefaultChange(e) {
    var val = e.target.value;
    doSave({ mode: "single", provider_id: val });
  }

  function handleToggle(id) {
    var next = WEB_toggleMember(cfg, id);
    if (!next) {
      setFieldError("Keep at least one " + info.noun + ". Switch to Single to use one " + info.noun + ".");
      return;
    }
    setFieldError(null);
    doSave(next);
  }

  function handleToggleAll(checked, keys) {
    if (!checked) {
      setFieldError("Keep at least one " + info.noun + ". Switch to Single to use one " + info.noun + ".");
      return;
    }
    var nextIds = (cfg && Array.isArray(cfg.provider_ids)) ? cfg.provider_ids.slice() : [];
    for (var i = 0; i < keys.length; i++) {
      if (nextIds.indexOf(keys[i]) === -1) {
        nextIds.push(keys[i]);
      }
    }
    setFieldError(null);
    doSave({ mode: "aggregated", provider_ids: nextIds });
  }

  var sortedProviders = WEB_sortProviders(kind, providerRows);
  var currentSingleId = cfg ? (cfg.provider_id || "") : "";
  var isCurrentMissing = currentSingleId !== "" && !providerRows.some(function (r) { return r.id === currentSingleId; });
  var defaultError = fieldError || (isCurrentMissing ? (currentSingleId + " no longer exists. Pick another " + info.noun + ".") : null);

  var baseChainRows = WEB_chainRows(kind, cfg, providerRows);
  var memberIds = (cfg && Array.isArray(cfg.provider_ids)) ? cfg.provider_ids : [];
  var memberCount = memberIds.length;

  var checkTableRows = baseChainRows.map(function (r) {
    if (!r.checked) {
      return {
        key: r.key,
        label: r.label,
        checked: false,
        disabled: false,
        extra: null,
      };
    }
    var idx = memberIds.indexOf(r.key);
    var pos = idx >= 0 ? idx + 1 : 1;
    var isFirst = idx === 0;
    var isLast = idx === memberCount - 1;

    var extraNode = (
      <span className="row" style={{ alignItems: "center", gap: 4 }}>
        <span data-testid={t + "-pos:" + r.key} className="muted" style={{ minWidth: 20 }}>
          {pos}
        </span>
        <Btn
          size="sm"
          kind="ghost"
          data-testid={t + "-up:" + r.key}
          aria-label={"Move " + r.key + " up"}
          disabled={isFirst}
          blocked={saving}
          onClick={function () {
            var next = WEB_moveMember(cfg, r.key, -1);
            if (next) doSave(next);
          }}
        >
          {"\u2191"}
        </Btn>
        <Btn
          size="sm"
          kind="ghost"
          data-testid={t + "-down:" + r.key}
          aria-label={"Move " + r.key + " down"}
          disabled={isLast}
          blocked={saving}
          onClick={function () {
            var next = WEB_moveMember(cfg, r.key, 1);
            if (next) doSave(next);
          }}
        >
          {"\u2193"}
        </Btn>
      </span>
    );

    return {
      key: r.key,
      label: r.label,
      checked: true,
      disabled: false,
      extra: extraNode,
    };
  });

  return (
    <PK_Section title={info.title} testid={t + "-section"}>
      <PK_Grid cols={cols}>
        <PK_Field label="Mode">
          <PK_Segmented
            options={[
              { value: "single", label: "Single" },
              { value: "aggregated", label: "Aggregated" },
            ]}
            value={cfg ? cfg.mode : "single"}
            onChange={handleModeChange}
            testid={t + "-mode"}
            ariaLabel={info.title + " mode"}
          />
        </PK_Field>
        {cfg && cfg.mode === "aggregated" ? (
          <PK_Field label={info.orderLabel} help={info.orderHelp} error={fieldError}>
            <PK_CheckTable
              rows={checkTableRows}
              onToggle={handleToggle}
              onToggleAll={handleToggleAll}
              extraLabel="Order"
              testid={t + "-chain"}
            />
          </PK_Field>
        ) : (
          <PK_Field label={info.defaultLabel} help={info.defaultHelp} error={defaultError}>
            <select
              data-testid={t + "-default"}
              aria-label={info.defaultLabel}
              value={currentSingleId}
              onChange={handleDefaultChange}
              aria-busy={saving ? "true" : "false"}
            >
              {isCurrentMissing ? (
                <option key={currentSingleId} value={currentSingleId}>
                  {currentSingleId + " (missing)"}
                </option>
              ) : null}
              {sortedProviders.map(function (r) {
                return (
                  <option key={r.id} value={r.id}>
                    {WEB_optionLabel(kind, r)}
                  </option>
                );
              })}
            </select>
          </PK_Field>
        )}
      </PK_Grid>
      {kind === "search" ? (
        <div style={{ marginTop: 8 }}>
          <Btn
            kind="ghost"
            size="sm"
            data-testid="web-search-manage"
            onClick={onManageProviders || function () {}}
          >
            {"Manage search providers in Providers \u203a Web Search \u2192"}
          </Btn>
        </div>
      ) : null}
      {children}
    </PK_Section>
  );
}
