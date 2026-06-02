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
| highlight-python.js | hand-written, no upstream | n/a | `9ccd88e06cd5b81405dae98725bc1e5994adf99151fb9b12fb06f324bbd6e70b` | MIT (this repo) | 2026-05-16 |
| highlight-json.js   | hand-written, no upstream | n/a | `813aaa2046f5ddcf0f71f3c74a4dc68dec7a24bbb4a538f76fe14e3b55e2729a` | MIT (this repo) | 2026-05-16 |
| sparkline.js        | hand-written, no upstream | n/a | `b37449b0745995d38484eaa2facaf06258bd1dbd7c3b6586c3f7c05fc7a1fdda` | MIT (this repo) | 2026-05-16 |
| auto-layout.js      | hand-written, no upstream | n/a | `bb5944d5aea3a8d91c6e9c3bff64ccb06848c191450030805e285c6971c63f90` | MIT (this repo) | 2026-05-17 |
| react.min.js        | https://unpkg.com/react@18.3.1/umd/react.production.min.js | 18.3.1 (production build) | `d949f1c3687aedadcedac85261865f29b17cd273997e7f6b2bfc53b2f9d4c4dd` | MIT | 2026-05-29 |
| react-dom.min.js    | https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js | 18.3.1 (production build) | `35f4f974f4b2bcd44da73963347f8952e341f83909e4498227d4e26b98f66f0d` | MIT | 2026-05-29 |
| babel.min.js        | https://unpkg.com/@babel/standalone@7.29.0/babel.min.js | 7.29.0 | `2623a9e22809915ce789b4461154e277ddce520d5a4320c14d44332a5d0dcea0` | MIT | 2026-05-29 |
| html2canvas.min.js  | https://cdn.jsdelivr.net/npm/html2canvas-pro@1.5.10/dist/html2canvas-pro.min.js | html2canvas-pro 1.5.10 (drop-in fork with oklch/oklab/lab/lch support) | `a55a357587e4634f9c456e8dc7adf186a9bda945280e5f23c311873015f5ac18` | MIT | 2026-06-02 |
| fonts/IBMPlexSans-VF-latin.woff2 | Google Fonts subset of @ibm/plex@v23 (latin, variable weight 100-700) | v23 | `e2291e842cf5af167122a22881a740c7f2dda7716f1e8cd76680264f4a859470` | SIL OFL 1.1 | 2026-05-29 |
| fonts/IBMPlexMono-Regular-latin.woff2 | Google Fonts subset of @ibm/plex@v20 (latin, weight 400) | v20 | `08949f728dc52d528e69b1667d15c89a5686a4ee9a296ff90983985f99c380f7` | SIL OFL 1.1 | 2026-05-29 |
| fonts/IBMPlexMono-Medium-latin.woff2  | Google Fonts subset of @ibm/plex@v20 (latin, weight 500) | v20 | `01d285447409c8a588692162439a038b8cbd7871309ee20267b0d2d91c6e8e22` | SIL OFL 1.1 | 2026-05-29 |

To recompute hand-written hashes (from repo root):

```bash
for f in ui/vendor/highlight-python.js ui/vendor/highlight-json.js ui/vendor/sparkline.js ui/vendor/auto-layout.js; do
  printf '%s  ' "$f"; sha256sum "$f" | awk '{print $1}'
done
```

To recompute / re-verify vendored React + Babel:

```bash
for f in ui/vendor/react.min.js ui/vendor/react-dom.min.js ui/vendor/babel.min.js; do
  printf '%s  ' "$f"; sha256sum "$f" | awk '{print $1}'
done
```

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
