// Sparkline SVG-path math.
// Pure function: takes an array of numeric values and target
// dimensions, returns the SVG path strings to render. The React
// component wrapper lives in ui/components/shared.jsx.
//
// First-party code; no upstream. Extracted from the inline math in
// shared.jsx's Sparkline component per Foundation Task 11.

(function () {
  // buildSparkline(values, width, height) -> { path, area, width, height } | null
  //   values  : Array<number>, may be empty
  //   width   : px (default 80)
  //   height  : px (default 24)
  //
  // Returns null for empty input (caller renders nothing).
  // path : the `d=` for the line stroke
  // area : the `d=` for the filled area beneath the line
  // width/height: echoed back so caller can set the SVG viewBox.
  //
  // Y-axis: 0 at top (SVG convention), max value at the top, min at
  // the bottom, with a 2px padding top and bottom.
  function buildSparkline(values, width, height) {
    if (!values || values.length === 0) return null;
    width = width || 80;
    height = height || 24;
    const max = Math.max.apply(null, values.concat([1]));
    const min = Math.min.apply(null, values.concat([0]));
    const range = max - min || 1;
    const step = width / (values.length - 1 || 1);
    const pts = values.map(function (v, i) {
      const x = i * step;
      const y = height - 2 - ((v - min) / range) * (height - 4);
      return [x, y];
    });
    const path = pts.map(function (p, i) {
      return (i === 0 ? "M" : "L") + p[0] + "," + p[1];
    }).join(" ");
    const area = path + " L" + width + "," + height + " L0," + height + " Z";
    return { path: path, area: area, width: width, height: height };
  }

  window.matrixVendor = window.matrixVendor || {};
  window.matrixVendor.buildSparkline = buildSparkline;
})();
