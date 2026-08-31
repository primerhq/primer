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
    // Mostly static, but not entirely. The extras block IS per-process:
    // which optional packages are installed cannot change under a
    // running server. The speech block is not: stt_configured and
    // tts_configured are "does a provider row exist", and rows come and
    // go while the console is open.
    //
    // Fetched once, that made the answer a snapshot from whenever the
    // page happened to load. Registering a speech provider and going
    // back to a session left the mic and the speaker toggle missing
    // until the page was reloaded, because the affordance was gated on
    // a fact the console had stopped asking about.
    //
    // apiFetch is read off the namespace at call time, not captured at
    // module scope, so the docs embeds' stub api still works (see
    // providers.jsx's note).
    return window.primerApi.useResource(
      "capabilities",
      (signal) => window.primerApi.apiFetch("GET", "/capabilities", null, { signal }),
      { pollMs: 10000 },
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
