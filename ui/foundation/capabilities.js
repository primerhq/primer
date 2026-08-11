// primer UI - capability discovery (modular-monolith spec).
// Wraps GET /v1/capabilities in a shared hook plus a gate component so
// pages can render honest "not installed" states instead of features
// that only fail when configured. Loaded via <script type="text/babel">.

(function () {
  const ns = (window.primerApi = window.primerApi || {});

  // Provider-type option value -> extra that backs it.
  const EXTRA_FOR_PROVIDER_TYPE = {
    huggingface: "huggingface",
    lance: "lance",
    container: "docker",
    kubernetes: "kubernetes",
  };

  function capabilityHint(extra) {
    return (
      "Not installed on this server. Enable it with: pip install " +
      "'primer-ai[" + extra + "]' (then restart the server). See the " +
      "install docs for details."
    );
  }

  function useCapabilities() {
    // Static per-process data: fetch once, no polling. apiFetch is read
    // off the namespace at call time, not captured at module scope, so
    // the docs embeds' stub api still works (see providers.jsx's note).
    return window.primerApi.useResource(
      "capabilities",
      (signal) => window.primerApi.apiFetch("GET", "/capabilities", null, { signal }),
      { pollMs: 0 },
    );
  }

  function extraInstalled(caps, extra) {
    // Unknown/loading reads as installed so pages never flash the gate.
    if (!caps || !caps.data || !caps.data.extras) return true;
    const status = caps.data.extras[extra];
    return !status || !!status.installed;
  }

  function CapabilityGate({ extra, feature, children }) {
    const caps = useCapabilities();
    if (extraInstalled(caps, extra)) return children || null;
    return (
      <div className="empty-state" data-capability-gate={extra}>
        <h3>{feature} is not installed</h3>
        <p>{capabilityHint(extra)}</p>
        <pre><code>{"pip install 'primer-ai[" + extra + "]'"}</code></pre>
      </div>
    );
  }

  ns.useCapabilities = useCapabilities;
  ns.capabilityHint = capabilityHint;
  ns.extraInstalled = extraInstalled;
  ns.CapabilityGate = CapabilityGate;
  ns.EXTRA_FOR_PROVIDER_TYPE = EXTRA_FOR_PROVIDER_TYPE;
})();
