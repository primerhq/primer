---
title: "Accounts, API tokens and sign-in"
summary: "Understand cookies, scoped API tokens, role-aware welcomes and the human sign-in boundary."
order: 8
related: [start-here/sharing-and-access, toolsets/mcp-exposure, toolsets/sign-in]
---

## What it is

Authentication identifies the person a request and its agent turn act for. A browser signs in with a session cookie; a programmatic client presents an API token with `Authorization: Bearer <token>`. Neither mechanism grants a role, entity access or an approval exemption beyond that person's actual permissions.

## Concepts

- Cookie sessions implicitly carry every API scope. API tokens have an immutable scope list; an external MCP client needs `mcp`. The token acts as its owner, not as an independently privileged agent.
- A token's plaintext is returned once at creation. Later lists expose metadata and a prefix, never a recoverable secret. Names are unique within one user's tokens. Expired and revoked rows remain for audit and cannot authenticate; mint a replacement rather than trying to recover or reactivate one.
- A password change, admin password reset or sign-out-everywhere advances the user's session epoch. Previously signed cookies then fail. Signing out one browser clears its cookie only; none of these operations revokes API tokens.
- Open streams are revalidated against account existence, enabled state, role, cookie epoch/lifetime or token revocation/expiry. A terminal WebSocket closes with `4401` on lost authentication; the session tap is HTTP SSE, not a WebSocket. Authentication failure is not a retryable tool failure.
- Roles and entity sharing are separate. Admin account/SSO management does not grant an ordinary user access to another user's private workspace. See [sharing and access](start-here/sharing-and-access).
- The welcome is personal and role-aware. Acknowledging or reopening it changes only the current role's acknowledgement; a stale role is refused, and existing sign-in sessions remain valid.
- In the operator session, the initial suggestions collapse when a message is accepted, including a message you write yourself. They stay dismissed on later visits after the welcome state is saved. Use "Show Me What You Can Do" to deliberately reopen them; a failed send keeps the suggestions and your draft available.

## How to

1. A person completes password or SSO sign-in in the console. First-boot registration, login/logout, password changes and SSO browser redirects are human sign-in operations, not target tools.
2. To connect an outside MCP client, mint a named token through the authorized token surface, copy its plaintext once, give it the `mcp` scope and configure the client's bearer header. A bearer token cannot mint another token: token creation requires a cookie session, even when a generated operation is discoverable.
3. Separately enable and configure [MCP exposure](toolsets/mcp-exposure). The exposure, live role, toolset grant and approval policy still constrain every target call.
4. On a 401 or terminal `4401`, stop calls and ask the person to authenticate or replace the token. For a revoked token, reconnecting with the same value cannot recover access.

## Tool calls

Use the operation references below to inspect the caller's token metadata, welcome state, linked SSO identities, and the admin account/SSO configuration. Human sign-in routes are deliberately absent from this target catalogue. A generated reference describes an operation and its route checks; it does not promise every authentication mechanism can execute it.

## Gotchas

- Password login and password changes are throttled. Respect `Retry-After`; a busy password service can return 503 rather than queueing without bound. Programmatic clients should use API tokens.
- Disabling authentication does not make session-signing flows available: password/SSO login and related cookie flows refuse before issuing credentials. Requests already use the synthetic system identity.
- SSO provider configuration and linked-identity metadata are distinct from the browser redirect that authenticates a person.
- Do not paste tokens, passwords, OAuth codes or provider secrets into wiki examples, prompts or shared files.

## Additional operation reference

The following operations belong to this feature family. The role shown is the minimum role, not a grant of entity access or an approval exemption. Read the linked live argument, result and default-policy facts before a call; owner checks, cookie-only routes and human decision rules still apply. Generated writes can require approval even when an older curated read or operation does not.

| Operation | Use for | Role floor |
|---|---|---|
| [`admin__admin_list_user_tokens`](reference/tools/admin/admin_list_user_tokens) | List a user's API tokens (admin; plaintext omitted). | admin |
| [`admin__admin_revoke_user_token`](reference/tools/admin/admin_revoke_user_token) | Revoke a user's API token (admin). Idempotent. | admin |
| [`admin_users__create_admin_user`](reference/tools/admin_users/create_admin_user) | Provision a user account. | admin |
| [`admin_users__delete_admin_user`](reference/tools/admin_users/delete_admin_user) | Delete a user account. | admin |
| [`admin_users__get_admin_user`](reference/tools/admin_users/get_admin_user) | Get a user account by id. | admin |
| [`admin_users__list_admin_users`](reference/tools/admin_users/list_admin_users) | List user accounts. | admin |
| [`admin_users__update_admin_user`](reference/tools/admin_users/update_admin_user) | Update a user account (partial). | admin |
| [`auth__acknowledge_welcome`](reference/tools/auth/acknowledge_welcome) | Remember that you have seen the welcome for your current role. | user |
| [`auth__auth_status`](reference/tools/auth/auth_status) | auth_status. | user |
| [`auth__create_api_token`](reference/tools/auth/create_api_token) | Create an API token. Plaintext returned ONCE. | user |
| [`auth__list_api_tokens`](reference/tools/auth/list_api_tokens) | List the caller's API tokens (plaintext omitted). | user |
| [`auth__list_my_sso_identities`](reference/tools/auth/list_my_sso_identities) | list_my_sso_identities. | user |
| [`auth__list_sso_providers`](reference/tools/auth/list_sso_providers) | list_sso_providers. | user |
| [`auth__rename_api_token`](reference/tools/auth/rename_api_token) | Rename an API token. | user |
| [`auth__reopen_welcome`](reference/tools/auth/reopen_welcome) | Show your role's welcome and suggested prompts again. | user |
| [`auth__revoke_api_token`](reference/tools/auth/revoke_api_token) | Revoke an API token. Idempotent. | user |
| [`auth__unlink_sso_identity`](reference/tools/auth/unlink_sso_identity) | unlink_sso_identity. | user |
| [`oidc_providers__create`](reference/tools/oidc_providers/create) | Create OidcProvider. | admin |
| [`oidc_providers__delete`](reference/tools/oidc_providers/delete) | Delete OidcProvider. | admin |
| [`oidc_providers__find`](reference/tools/oidc_providers/find) | Find OidcProvider with predicate. | admin |
| [`oidc_providers__get`](reference/tools/oidc_providers/get) | Get OidcProvider by id. | admin |
| [`oidc_providers__list`](reference/tools/oidc_providers/list) | List OidcProvider. | admin |
| [`oidc_providers__update`](reference/tools/oidc_providers/update) | Replace OidcProvider. | admin |
| [`sso_settings__get_sso_settings`](reference/tools/sso_settings/get_sso_settings) | Read the SSO JIT-provisioning settings. | admin |
| [`sso_settings__put_sso_settings`](reference/tools/sso_settings/put_sso_settings) | Update the SSO JIT-provisioning settings. | admin |
| [`users__lookup_users`](reference/tools/users/lookup_users) | Find users to share with by username prefix, or name the ids the console's badges show. | user |

## Related

[Roles and approvals](start-here/roles-and-approvals), [sharing and access](start-here/sharing-and-access), [MCP exposure](toolsets/mcp-exposure), [upstream MCP sign-in](toolsets/sign-in).
