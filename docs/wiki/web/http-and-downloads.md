---
title: "HTTP requests and downloads"
summary: "Call an HTTP API and download a file into the workspace."
order: 2
tools: [web__http_request, web__download]
related: [web/search-and-fetch, approvals/policies]
---
## What it is
`web__http_request` performs a raw HTTP call and returns the status, headers and a byte-capped body; `web__download` saves a file at a URL into the session's workspace. Both are user-tier tools. They run on the platform, not in your workspace, so every outbound connection passes an egress guard.

## Concepts
- `web__http_request` takes `url` (http or https), `method` (GET by default, or POST, PUT, PATCH, DELETE, HEAD, OPTIONS), `headers` (a string-to-string map), `body` (a string; you serialise JSON yourself) and `timeout_seconds` (default 30, at most 300, one deadline for the whole call including the body). It answers `{status, headers, body, truncated}`. The body is read only to a 1 MB byte cap; anything past it is dropped and `truncated` is `true`. Invalid UTF-8 bytes are replaced, not an error.
- Redirects are not followed: a 3xx is the answer, so read `headers.location` and call it again yourself.
- Unless you set your own `Accept-Encoding`, the tool sends `Accept-Encoding: gzip, deflate`. One gzip or deflate layer is decoded, and the cap counts decoded bytes; a stacked or unknown content encoding is refused before the body is read.
- `web__download` takes `url`, an optional workspace-relative `path` and an optional `max_bytes`. It needs a workspace session. A `path` ending in `/` (or an omitted one) takes the file name from the URL's last segment. `max_bytes` can only lower the 100 MB cap. The 300 second deadline is fixed, covering every hop and the body. The bytes are written only in full: over the cap, a non-2xx answer or a timeout means nothing is written. It answers `{path, bytes, url}`.
- The egress guard resolves the host to all its addresses and refuses the name when any one of them is not a public unicast address: loopback, RFC1918 (10/8, 172.16/12, 192.168/16), link-local (169.254/16, which includes the cloud metadata address 169.254.169.254), CGNAT, IPv6 ULA, multicast, unspecified or reserved, and the IPv4-mapped forms of these. It then connects to the vetted address itself, so a second DNS answer (rebinding) cannot swap in an internal one; the `Host` header and the TLS certificate check still use the original name. A host with one public and one private address is refused as a whole.
- An operator opts internal targets back in with `PRIMER_EGRESS_ALLOW`: a JSON list of CIDRs, IPs or exact host names, empty by default. You cannot allow a target yourself, and a refusal is not transient: the same URL, a redirect to it or a short link to it gets the same answer.

## How to
1. For an API, call `web__http_request` with the method and the serialised body.
2. Read `status` and `truncated` before trusting `body`.
3. To save a file, call `web__download` with a workspace-relative `path`, then check the answer's `path` and `bytes`.
4. To reach a service inside your own workspace, use the workspace tools (for example `workspace__exec`), not the web tools.

## Tool calls
### Call an HTTP API
`web__http_request` performs the request and returns the status, headers and the capped body.

```json
{"toolset_id": "web", "tool_name": "http_request", "arguments": {"url": "https://api.github.com/repos/python/cpython", "method": "GET"}}
```

Reference: [`web__http_request`](reference/tools/web/http_request)

### Download a file into the workspace
`web__download` streams the file under the byte cap and writes it into the session's workspace.

```json
{"toolset_id": "web", "tool_name": "download", "arguments": {"url": "https://example.com/a.pdf", "path": "docs/a.pdf"}}
```

Reference: [`web__download`](reference/tools/web/download)

## Gotchas
- An egress refusal answers `http-request refused: <host> resolves to a private address (<ip>); an operator can allow it with PRIMER_EGRESS_ALLOW` (and `download refused: ...`, nothing written). Retrying does not help: ask an operator to add the target to `PRIMER_EGRESS_ALLOW` if the access is intended.
- A 3xx is not followed: the moved API answers with its `location` header, and a redirect to a non-http scheme (for example `data:`) is a failed request, not a hop.
- `web__download` writes nothing on a non-2xx (`download failed: server returned <status> for <url>`) or over the cap (`download: file exceeds the maximum of <N> bytes; nothing was written`). A partial file is a corrupt file, so the write is all or nothing.
- The tools run on the platform: they can reach what the platform can reach, so the guard is the only thing between them and your cluster, databases and metadata service.
- An approval policy can gate `web__http_request` (and any tool) per toolset and tool name: a gated call parks until a person decides (approvals/policies).

## Related
[Search and fetch](web/search-and-fetch), [approval policies](approvals/policies).
