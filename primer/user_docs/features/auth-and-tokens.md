---
slug: auth-and-tokens
title: Auth and API tokens
section: features
summary: Register the operator account on first boot, log in, and create or revoke bearer tokens for programmatic access.
---

## Overview

Primer is single-operator in v1. The first visit to the console presents a one-time registration screen that creates the only account. Subsequent visits show the login screen. Bearer tokens let automated clients authenticate without a browser session.

## Registering the operator account

Registration is available only once -- the form is replaced by the login screen after the first account is created.

1. Open the console URL in your browser. The registration screen appears automatically when no operator account exists yet.
2. Enter a username (lowercase letters, digits, `.`, `_`, `-`).
3. Enter a password of at least eight characters. Confirm it in the second field.
4. Click **Create account**. The console reloads and you are signed in.

```callout:warning
Only one account exists in v1. If a colleague creates the account before you reach the registration screen, the form is gone and you will see the login screen instead. Coordinate with whoever runs the first boot.
```

## Logging in

1. Open the console URL. The login screen shows when an account exists but no session cookie is present.
2. Enter your username and password.
3. Optionally check **Keep me signed in on this device** to extend the session cookie lifetime.
4. Click **Sign in**. The console loads and your username initial appears in the avatar bubble in the top-right corner.

## Creating an API token

API tokens let scripts, the MCP bridge, and other automated clients authenticate with a bearer token instead of a browser session.

1. Navigate to **API tokens** in the left nav.
2. Click **Create token** (top-right of the filter bar).
3. In the **Create API token** dialog, enter a unique name (for example, `mcp-bridge-prod`). Names must be unique and at most 128 characters.
4. Under **Scopes**, check the scopes the token needs. The `mcp` scope permits calls to the MCP bridge endpoints. Tokens without any scope can authenticate but cannot reach scope-gated endpoints.
5. Optionally set an expiry date and time under **Expires at**. Leave blank for a token that does not expire.
6. Click **Create token**.

The dialog switches to the one-time reveal view:

```embed:api-token-create
```

7. Click **Copy token** to copy the plaintext to your clipboard.
8. Click **I have saved it -- close** once you have stored the value.

The token is never shown again. If you lose it, revoke it and create a new one.

## Managing tokens

The API tokens page lists every token with its name, prefix, scopes, last-used time, expiry, and status (active / revoked / expired).

To revoke a token, click the **Revoke** button on the token's row and confirm. The token stops working immediately. Existing in-flight requests using that token complete; all future requests receive a 401. The row remains in the list for audit purposes and the Revoke button becomes disabled.

## Automate this

```ref:reference/api-auth-tokens
Full token resource schema, list and create endpoints, and revoke endpoint.
```
