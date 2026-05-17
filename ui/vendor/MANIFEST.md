# Vendored third-party code

Every file in `ui/vendor/` MUST appear here with its origin (or
"hand-written, no upstream"), version / commit, sha256, license, and
date added. If a file is updated, update its sha256 in the same
commit.

This file is the operator's audit trail for the Shai-Hulud mitigation:
the only JS dependencies allowed in the console are the three SRI-pinned
CDN scripts in `ui/index.html` (catalogued below) plus the hand-written
helpers under `ui/vendor/`. Any new entry in either table requires a
human review — no transitive resolution, no surprise additions.

## ui/vendor/ — hand-written helpers (loaded same-origin)

| Path | Origin | Version / commit | sha256 | License | Date added |
|---|---|---|---|---|---|
| highlight-python.js | hand-written, no upstream | n/a | `9ccd88e06cd5b81405dae98725bc1e5994adf99151fb9b12fb06f324bbd6e70b` | MIT (this repo) | 2026-05-16 |
| highlight-json.js   | hand-written, no upstream | n/a | `813aaa2046f5ddcf0f71f3c74a4dc68dec7a24bbb4a538f76fe14e3b55e2729a` | MIT (this repo) | 2026-05-16 |
| sparkline.js        | hand-written, no upstream | n/a | `b37449b0745995d38484eaa2facaf06258bd1dbd7c3b6586c3f7c05fc7a1fdda` | MIT (this repo) | 2026-05-16 |
| auto-layout.js      | hand-written, no upstream | n/a | `bb5944d5aea3a8d91c6e9c3bff64ccb06848c191450030805e285c6971c63f90` | MIT (this repo) | 2026-05-17 |

To recompute (from repo root):

```bash
for f in ui/vendor/highlight-python.js ui/vendor/highlight-json.js ui/vendor/sparkline.js ui/vendor/auto-layout.js; do
  printf '%s  ' "$f"; sha256sum "$f" | awk '{print $1}'
done
```

## CDN-pinned third-party code (loaded from `ui/index.html`, NOT under ui/vendor/)

| Package | Version | CDN URL | SRI (sha384) |
|---|---|---|---|
| react             | 18.3.1 | https://unpkg.com/react@18.3.1/umd/react.development.js | `hD6/rw4ppMLGNu3tX5cjIb+uRZ7UkRJ6BPkLpg4hAu/6onKUg4lLsHAs9EBPT82L` |
| react-dom         | 18.3.1 | https://unpkg.com/react-dom@18.3.1/umd/react-dom.development.js | `u6aeetuaXnQ38mYT8rp6sbXaQe3NL9t+IBXmnYxwkUI2Hw4bsp2Wvmx4yRQF1uAm` |
| @babel/standalone | 7.29.0 | https://unpkg.com/@babel/standalone@7.29.0/babel.min.js | `m08KidiNqLdpJqLq95G/LEi8Qvjl/xUYll3QILypMoQ65QorJ9Lvtp2RXYGBFj1y` |

The SRI hashes were re-verified against the live CDN on 2026-05-15
(see `docs/ui/02-implementation-loop.md` §7 entry "F — CDN SRI hashes
verified, all three match"). The browser will refuse to execute the
script if a future re-publish under the same version mismatches the
hash here.

To re-verify (from any environment with curl + openssl):

```bash
for url in \
  https://unpkg.com/react@18.3.1/umd/react.development.js \
  https://unpkg.com/react-dom@18.3.1/umd/react-dom.development.js \
  https://unpkg.com/@babel/standalone@7.29.0/babel.min.js
do
  printf '%s\n  sha384: ' "$url"
  curl -sL "$url" | openssl dgst -sha384 -binary | openssl base64 -A
  echo
done
```
