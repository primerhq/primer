// Mobile-first viewport hook with band-based memoisation.
// Exposed as window.primerApi.useViewport.

(function () {
  const { useState, useEffect } = window.React;

  const MOBILE_MAX = 639;
  const TABLET_MAX = 1023;

  function bandFor(width) {
    if (width <= MOBILE_MAX) return "mobile";
    if (width <= TABLET_MAX) return "tablet";
    return "desktop";
  }

  // ?force-desktop=1 escape hatch, persisted to localStorage.
  function forceDesktopActive() {
    try {
      const u = new URL(window.location.href);
      if (u.searchParams.get("force-desktop") === "1") {
        localStorage.setItem("primer.force-desktop", "1");
        return true;
      }
      return localStorage.getItem("primer.force-desktop") === "1";
    } catch {
      return false;
    }
  }

  function useViewport() {
    const initial = window.innerWidth || 1024;
    const [width, setWidth] = useState(initial);
    const [band, setBand] = useState(bandFor(initial));

    useEffect(() => {
      let rafId = null;
      const onResize = () => {
        if (rafId != null) return;
        rafId = requestAnimationFrame(() => {
          rafId = null;
          const w = window.innerWidth;
          const b = bandFor(w);
          setWidth(w);
          setBand((prev) => (prev === b ? prev : b));
        });
      };
      window.addEventListener("resize", onResize);
      return () => {
        if (rafId != null) cancelAnimationFrame(rafId);
        window.removeEventListener("resize", onResize);
      };
    }, []);

    const forceDesktop = forceDesktopActive();
    return {
      width,
      isMobile: !forceDesktop && band === "mobile",
      isTablet: !forceDesktop && band === "tablet",
      isDesktop: forceDesktop || band === "desktop",
    };
  }

  const ns = (window.primerApi = window.primerApi || {});
  ns.useViewport = useViewport;
})();
