---
slug: first-login
title: First login
section: getting-started
summary: Register the admin account, log in, and obtain a bearer token for automation.
---

## Registration

On a fresh install the console presents a registration form. The first user who registers becomes the admin. Registration is locked for subsequent visitors -- all additional users must be created by the admin in the Users section.

```callout:warning
Complete registration immediately after install. Until the admin account exists, anyone who can reach the console can claim it.
```

## Logging in

After registration, subsequent visits check for a valid session cookie. Log in with the email and password set during registration. The session cookie is HMAC-signed; its lifetime defaults to 7 days (`PRIMER_AUTH__SESSION_TTL_DAYS`).

```callout:tip
Running behind TLS? Set `PRIMER_AUTH__COOKIE_SECURE=true` so the browser will not send the session cookie over plain HTTP.
```

## Bearer tokens for automation

Scripts and CI pipelines should authenticate with a long-lived API token rather than a session cookie. Create one from the console:

```embed:api-token-create
```

Copy the token immediately -- it is shown only once. Pass it as an `Authorization: Bearer <token>` header on every API request.

## Next steps

For the full picture of auth configuration, token scopes, and rotation:

```ref:features/auth-and-tokens
Auth configuration, token scopes, and rotation.
```

Ready to create your first agent and run a session:

```ref:getting-started/first-agent
Create an agent, run a session, and see the result.
```
