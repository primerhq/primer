---
title: "Time, ids, hashes and math"
summary: "Get the date and time, mint UUIDs, hash text and calculate safely."
order: 1
tools: [misc__get_datetime, misc__uuid_v4, misc__hash, misc__calculate]
related: [utilities/sleep-and-watch-files]
---
## What it is
The built-in `misc` toolset holds four read-only helpers: `misc__get_datetime` for the real current time, `misc__uuid_v4` for fresh random identifiers, `misc__hash` for digests and `misc__calculate` for arithmetic. All four are user-tier tools that never pause the turn, and each call is stateless.

## Concepts
- `misc__get_datetime` takes an optional `timezone`, an IANA name such as `UTC`, `America/Los_Angeles` or `Asia/Tokyo`; it defaults to UTC, and an invalid name answers `type=bad-request`. The answer is `{datetime, timezone, unix}`: ISO 8601 plus Unix epoch seconds. Use it instead of estimating or reusing a time from earlier in the conversation.
- `misc__uuid_v4` takes `count` (1 to 100, default 1) and answers `{uuids: [...]}`. Each UUID comes from the operating system's CSPRNG. Do not invent ids yourself: model-generated "random" strings are low entropy.
- `misc__hash` takes `input` (encoded as UTF-8 before digesting; the empty string is allowed) and `algorithm`, one of `sha256` (default), `sha1` and `md5`. It answers `{algorithm, hex_digest}`. `sha256` is for content addressing; `sha1` and `md5` exist to interop with legacy values, and neither is cryptographically safe.
- `misc__calculate` takes `expression`, at most 1024 characters: the operators `+ - * / // % **`, parentheses and unary plus/minus, an allowlist of functions (`abs, round, min, max, pow, sqrt, log, log2, log10, exp, sin, cos, tan, asin, acos, atan, floor, ceil`) and the constants `pi, e, tau`. It is not a Python eval: no attribute access, no subscripts, no comprehensions, no string operations. It answers `{expression, result}`.

## How to
1. Call `misc__get_datetime` for the current time, with a `timezone` when the time belongs to a place.
2. Call `misc__uuid_v4` with the `count` you need for fresh identifiers.
3. Call `misc__hash` on the exact text you are addressing, under `sha256` unless a legacy value forces `sha1` or `md5`.
4. Call `misc__calculate` for arithmetic; do not compute by hand.

## Tool calls
### Get the current time
`misc__get_datetime` returns the wall-clock time in the named timezone, UTC when omitted.

```json
{"toolset_id": "misc", "tool_name": "get_datetime", "arguments": {"timezone": "America/New_York"}}
```

Reference: [`misc__get_datetime`](reference/tools/misc/get_datetime)

### Mint UUIDs
`misc__uuid_v4` returns `count` random UUIDv4 strings from the OS CSPRNG.

```json
{"toolset_id": "misc", "tool_name": "uuid_v4", "arguments": {"count": 3}}
```

Reference: [`misc__uuid_v4`](reference/tools/misc/uuid_v4)

### Hash text
`misc__hash` returns the hex digest of the UTF-8 input under the named algorithm, `sha256` by default.

```json
{"toolset_id": "misc", "tool_name": "hash", "arguments": {"input": "hello", "algorithm": "sha256"}}
```

Reference: [`misc__hash`](reference/tools/misc/hash)

### Calculate
`misc__calculate` evaluates the expression against the allowlist, with no code execution.

```json
{"toolset_id": "misc", "tool_name": "calculate", "arguments": {"expression": "(100 - 32) * 5 / 9"}}
```

Reference: [`misc__calculate`](reference/tools/misc/calculate)

## Gotchas
- The digest covers the UTF-8 encoding of `input`: hash the exact text you mean to address.
- `misc__calculate` refuses anything outside the allowlist (a name that is not one of the functions or constants, attribute access, subscripts); split the work into several calls rather than fighting the parser.
- A `sha1` or `md5` answer is not a security-grade substitute for `sha256`.
- None of these tools waits: for a fixed pause or a file-change wait, use the `workspace_ext` tools (utilities/sleep-and-watch-files).

## Related
[Sleep and watch files](utilities/sleep-and-watch-files).
