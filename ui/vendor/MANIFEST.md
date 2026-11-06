# Vendored third-party code

Every file in `ui/vendor/` MUST appear here with its origin (or
"hand-written, no upstream"), version / commit, sha256, license, and
date added. If a file is updated, update its sha256 in the same
commit.

This file is the operator's audit trail for the Shai-Hulud mitigation:
the only JS dependencies allowed in the console are the SRI-pinned
self-hosted scripts below. Any new entry requires a human review — no
transitive resolution, no surprise additions.

## ui/vendor/ — vendored third-party (loaded same-origin)

| Path | Origin | Version / commit | sha256 | License | Date added |
|---|---|---|---|---|---|
| highlight-python.js | hand-written, no upstream | n/a | `3a01e309ca889135224055ed7b4a83a046909094f5976d053ff85f4db8c34db8` | MIT (this repo) | 2026-05-16 |
| highlight-json.js   | hand-written, no upstream | n/a | `7f3b2b8d93c7e7251a66d4f73cae3cec855b82597fb741583824cb9dc4722b62` | MIT (this repo) | 2026-05-16 |
| highlight-code.js   | hand-written, no upstream | n/a | `ad2ca5f36a503d1475f26b964f9854c489407bf5853dcb708863d30d55134d38` | MIT (this repo) | 2026-07-05 |
| highlight-curl.js   | hand-written, no upstream | n/a | `a6e835901b75391ab7224791ebf9fc2da1c68b5fc2c773b78848d419cdfdcaaf` | MIT (this repo) | 2026-06-04 |
| highlight-jsx.js    | hand-written, no upstream | n/a | `feabf4b43a5dcb5a3ec10802ed037fa6f24f8ac7640470140670292e655ea82e` | MIT (this repo) | 2026-06-04 |
| markdown.jsx        | hand-written, no upstream | n/a | `aeb569ca5a1f774820cd653861cd8c7df85c002e1f7f743a0fab309977470bf6` | MIT (this repo) | 2026-05-26 |
| sparkline.js        | hand-written, no upstream | n/a | `6b013eaffb3acf457b9237c7e105eb7561beb386d89f3ec95413eaec29473722` | MIT (this repo) | 2026-05-16 |
| auto-layout.js      | hand-written, no upstream | n/a | `9b7ee4a59f35e9e1b9e4e0350453ebaa86b32936959d9694b346eba323a49a8a` | MIT (this repo) | 2026-05-17 |
| react.min.js        | https://unpkg.com/react@18.3.1/umd/react.production.min.js | 18.3.1 (production build) | `d949f1c3687aedadcedac85261865f29b17cd273997e7f6b2bfc53b2f9d4c4dd` | MIT | 2026-05-29 |
| react-dom.min.js    | https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js | 18.3.1 (production build) | `35f4f974f4b2bcd44da73963347f8952e341f83909e4498227d4e26b98f66f0d` | MIT | 2026-05-29 |
| babel.min.js        | https://unpkg.com/@babel/standalone@7.29.0/babel.min.js | 7.29.0 | `2623a9e22809915ce789b4461154e277ddce520d5a4320c14d44332a5d0dcea0` | MIT | 2026-05-29 |
| html2canvas.min.js  | https://cdn.jsdelivr.net/npm/html2canvas-pro@1.5.10/dist/html2canvas-pro.min.js | html2canvas-pro 1.5.10 (drop-in fork with oklch/oklab/lab/lch support) | `a55a357587e4634f9c456e8dc7adf186a9bda945280e5f23c311873015f5ac18` | MIT | 2026-06-02 |
| g6.min.js           | https://unpkg.com/@antv/g6@5.1.1/dist/g6.min.js | 5.1.1 (UMD build) | `3e091a94fd08994a383ff34bfba256bb8e382e4be4042197a206d2ecc0957331` | MIT | 2026-06-28 |
| mermaid.min.js      | https://cdn.jsdelivr.net/npm/mermaid@10.9.6/dist/mermaid.min.js | 10.9.6 | `eda3a0ad572bbe69a318c1be0163e8233dd824f3f12939e5168feba207767151` | MIT | 2026-06-04 |
| fonts/IBMPlexSans-VF-latin.woff2 | Google Fonts subset of @ibm/plex@v23 (latin, variable weight 100-700) | v23 | `e2291e842cf5af167122a22881a740c7f2dda7716f1e8cd76680264f4a859470` | SIL OFL 1.1 | 2026-05-29 |
| fonts/IBMPlexMono-Regular-latin.woff2 | Google Fonts subset of @ibm/plex@v20 (latin, weight 400) | v20 | `08949f728dc52d528e69b1667d15c89a5686a4ee9a296ff90983985f99c380f7` | SIL OFL 1.1 | 2026-05-29 |
| fonts/IBMPlexMono-Medium-latin.woff2  | Google Fonts subset of @ibm/plex@v20 (latin, weight 500) | v20 | `01d285447409c8a588692162439a038b8cbd7871309ee20267b0d2d91c6e8e22` | SIL OFL 1.1 | 2026-05-29 |
| xterm.min.js        | https://unpkg.com/@xterm/xterm@5.5.0/lib/xterm.js | 5.5.0 | `1f991ac3b4b283ebf96e60ae23a00a52765dd3a2e46fa6fdda9f1aab032f7495` | MIT | 2026-07-02 |
| xterm.min.css       | https://unpkg.com/@xterm/xterm@5.5.0/css/xterm.css | 5.5.0 | `ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6` | MIT | 2026-07-02 |
| xterm-addon-fit.min.js | https://unpkg.com/@xterm/addon-fit@0.11.0/lib/addon-fit.js | 0.11.0 | `ba3ea256ce0620a0992a197d6c9baea64823fc93d8da07a9e366ca9943c18527` | MIT | 2026-07-02 |
| codemirror.min.js   | bundled from npm, see "CodeMirror 6" below | 16 pinned packages (@codemirror/state 6.5.2 et al) | `11684404b4478b1b3f1b143475ee7aa5a0703759deeb903455dc1fa834b47ee4` | MIT | 2026-08-01 |
| codemirror.entry.js | hand-written, no upstream (bundle entry, NOT loaded by the console) | n/a | `874e887276421f0860250687cc2b8d97194e96c269a7ab33c06a9971d91c445b` | MIT (this repo) | 2026-08-01 |
| parse-partial-json.js | hand-written, no upstream | n/a | `b97ffbb316d4f570e1c1c63f871c1099f2be7d15db850a533180c9d422e5ccc3` | MIT (this repo) | 2026-08-28 |

To recompute hand-written hashes (from repo root):

```bash
for f in ui/vendor/highlight-python.js ui/vendor/highlight-json.js ui/vendor/highlight-code.js ui/vendor/sparkline.js ui/vendor/auto-layout.js; do
  printf '%s  ' "$f"; sha256sum "$f" | awk '{print $1}'
done
```

To recompute / re-verify vendored React + Babel:

```bash
for f in ui/vendor/react.min.js ui/vendor/react-dom.min.js ui/vendor/babel.min.js; do
  printf '%s  ' "$f"; sha256sum "$f" | awk '{print $1}'
done
```

To recompute / re-verify vendored xterm.js:

```bash
for f in ui/vendor/xterm.min.js ui/vendor/xterm.min.css ui/vendor/xterm-addon-fit.min.js; do
  printf '%s  ' "$f"; sha256sum "$f" | awk '{print $1}'
done
```

## xterm.js (Studio integrated terminal, Phase 7)

`xterm.min.js` is the `@xterm/xterm` UMD build; loaded as a plain
`<script>` it spreads its exports directly onto `globalThis`, so the
terminal class is `window.Terminal` (not namespaced). `xterm-addon-fit.min.js`
(`@xterm/addon-fit`) instead assigns a single namespace object —
`window.FitAddon` — whose `FitAddon` property is the actual class, so
construction is `new window.FitAddon.FitAddon()` (verified by loading both
bundles in a bare `vm` context: `typeof window.Terminal === "function"` but
`typeof window.FitAddon === "object"` with `window.FitAddon.FitAddon` the
constructor). `xterm.min.css` is the addon-free base stylesheet for the
terminal's DOM structure (cursor, selection, viewport); it does not need the
CSP/self-host treatment scripts get since it's a same-origin `<link>`.

## Why self-hosted (was: CDN-pinned)

Up to 2026-05-29 these three were SRI-pinned `<script>` tags pointing
at `unpkg.com`. They are now vendored under `ui/vendor/` so the console
loads with zero third-party fetches: DNS + TLS handshake to unpkg gone,
strict `Cache-Control: immutable` on the local copies, and the CSP no
longer needs to allow `https://unpkg.com` in `script-src`.

The version pins (React 18.3.1, Babel 7.29.0) and the audit policy
(no transitive deps, no auto-updates) are unchanged. To upgrade, replace
the file, recompute the sha256 above, and bump the version column in
the same commit.


## CodeMirror 6 (codemirror.min.js)

The python toolset editor needs a real code editor: syntax-aware
highlighting, an autocomplete widget, and a gutter that can render
server-side diagnostics. CodeMirror 6 ships as ESM packages on npm, so it
cannot be dropped in like React's UMD build -- it is bundled to a single
IIFE here, matching how `g6.min.js` and `xterm.min.js` are loaded.

**This is the one entry with transitive packages baked in.** The audit
policy elsewhere is "no transitive deps"; the honest description here is
"transitive deps resolved ONCE, pinned by exact version, and frozen into a
file whose sha256 is recorded". Nothing resolves at install or run time --
the console loads one same-origin file. The full inventory is below so the
review is over a list, not over a lockfile.

Everything baked in, all MIT, all from the CodeMirror/lezer ecosystem:

| Package | Version |
|---|---|
| @codemirror/autocomplete | 6.18.6 |
| @codemirror/commands | 6.8.1 |
| @codemirror/lang-python | 6.2.1 |
| @codemirror/language | 6.11.2 |
| @codemirror/lint | 6.8.5 |
| @codemirror/search | 6.5.11 |
| @codemirror/state | 6.5.2 |
| @codemirror/view | 6.38.1 |
| @lezer/common | 1.5.2 |
| @lezer/highlight | 1.2.1 |
| @lezer/lr | 1.4.10 |
| @lezer/python | 1.1.19 |
| @marijn/find-cluster-break | 1.0.3 |
| crelt | 1.0.7 |
| style-mod | 4.1.3 |
| w3c-keyname | 2.2.8 |

### Rebuilding (reproducible)

`codemirror.entry.js` is the bundle's source and is vendored beside the
output so the recipe is auditable. It is NOT loaded by the console. To
rebuild:

```bash
mkdir cm6 && cd cm6 && npm init -y
npm install --save-exact \
  @codemirror/state@6.5.2 @codemirror/view@6.38.1 \
  @codemirror/commands@6.8.1 @codemirror/language@6.11.2 \
  @codemirror/lang-python@6.2.1 @codemirror/autocomplete@6.18.6 \
  @codemirror/lint@6.8.5 @codemirror/search@6.5.11 \
  @lezer/highlight@1.2.1 esbuild@0.25.10
cp <repo>/ui/vendor/codemirror.entry.js entry.js
./node_modules/.bin/esbuild entry.js --bundle --format=iife --minify \
  --target=es2019 --legal-comments=none --outfile=codemirror.min.js
sha256sum codemirror.min.js   # must match the table above
```

The entry exposes exactly one global, `window.CM6`, holding only the names
the editor uses. Anything not on that object is unreachable from `ui/`,
which keeps the vendored surface auditable rather than opening all of
CodeMirror to console code.
