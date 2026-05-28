"""UI E2E: Channels New-provider modal inline 422 validation journey.

Closes the §3 feature directive's "Pin the discriminated config form
(Slack / Telegram / Discord) + 422 inline field errors" item for the
UI side. T0860 (API loop) pins the server-side 422 for each
platform's token validation; U0115 pins the matching UI surface —
inline error rendering under the offending field on submit failure,
modal stays open, retry with valid input succeeds.

Multi-state UI journey on the New-provider modal:

  1. Navigate /channels/providers → click "New provider" → modal opens
     (default platform=Slack).
  2. Fill an `app_token` with the WRONG prefix
     ("wrongprefix-abc" instead of "xapp-..."), plus a placeholder
     bot_token, then submit.
  3. Server returns 422 /errors/validation-error with
     loc=("body","app_token") (the Slack field_validator's emission
     point — see primer/model/channel.py:SlackChannelProviderConfig).
  4. Modal stays OPEN; inline error renders under the App-token
     field (the modal's per-field err lookup matches
     `body.${f.key}` after the loc-prefix correction).
  5. Assert NO global error toast (422 routes inline, not via
     toast).
  6. Fix the app_token (replace prefix to "xapp-test-token"),
     keep the bot_token valid; submit again.
  7. Modal closes, the URL navigates to the new provider's
     detail page (onSuccess), and the Probe button on the detail
     page renders disabled with the documented hint
     (roadmap §8 — Probe endpoint not yet implemented).

Multi-page: modal → detail page after success.
Multi-state: invalid submit → retry → valid submit.

Covers backlog item U0115. Sibling of U0114 (policy modal Rego
inline) on the channels axis; together with U0108 (channels
onboarding) + U0111 (per-platform forms + Probe disabled) the
suite now pins the full channels-modal contract: happy-path
create across all three platforms, per-platform field schemas,
Probe disabled, AND inline 422 validation rendering.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect


def _cleanup(base_url: str, provider_ids: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for pid in provider_ids:
            try:
                c.delete(f"/v1/channel_providers/{pid}")
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0115 — Channels New-provider modal inline 422 validation + retry
# ===========================================================================


def test_u0115_channel_provider_modal_invalid_app_token_inline_error(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0115 — Multi-state UI journey: submit invalid Slack
    app_token, see inline error, then fix + retry → success +
    navigate to provider detail page.

    Pinned invariants:
      * The modal's per-field err lookup matches server loc:
        Slack app_token validation raises with
        loc=("body","app_token") (model/channel.py
        field_validator). UI errKey = `body.${f.key}` (channels.jsx
        after the loc-correction landed in this commit).
      * 422 errors render inline under the field — NO global toast
        (channels.jsx error handler routes 422→fieldErrors map).
      * Modal stays OPEN on 422 — operator can fix + retry.
      * Successful retry routes through the same modal; on 201
        the modal closes, onCreated navigates to the provider's
        detail page.
      * Provider detail renders the Probe button disabled with the
        roadmap §8 hint (covered by U0111 too, asserted here as a
        sanity check that the success path landed cleanly).
    """
    cp_id = f"u115-cp-{unique_suffix}"

    try:
        # ----- 1. /channels/providers → New provider modal ----------
        page.goto(
            f"{console_url}#/channels/providers",
            wait_until="domcontentloaded",
        )
        new_btn = page.get_by_role(
            "button", name="New provider", exact=True,
        )
        expect(new_btn).to_be_visible(timeout=20_000)
        new_btn.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)

        # ----- 2. Fill id + invalid app_token + placeholder bot_token --
        modal.get_by_placeholder("auto-generated", exact=False).first.fill(cp_id)

        # Default platform is Slack — fields rendered: app_token,
        # bot_token, signing_secret (all type=password). Fill the
        # first two; leave signing_secret blank (it's optional).
        slack_passwords = modal.locator("input[type=password]")
        expect(slack_passwords).to_have_count(3, timeout=5_000)
        # app_token (first) — INVALID prefix
        slack_passwords.nth(0).fill("wrongprefix-not-xapp")
        # bot_token (second) — valid placeholder
        slack_passwords.nth(1).fill("xoxb-test-placeholder")

        # ----- 3. Submit → server 422 ------------------------------
        submit = modal.get_by_role(
            "button", name="Create provider", exact=True,
        )
        expect(submit).to_be_enabled(timeout=5_000)
        submit.click()

        # ----- 4. Inline error renders under app_token -------------
        # The modal renders `fieldErrors[errKey]` where errKey is now
        # `body.${f.key}` (channels.jsx after the loc-correction).
        # The Slack app_token field_validator emits loc=("body",
        # "app_token") so the matched key is "body.app_token".
        # Field errors render as `.field-help` divs in red.
        # Look for the error TEXT inside the modal — robust against
        # markup tweaks.
        app_token_field = modal.get_by_text(
            "App token", exact=False,
        ).first.locator("xpath=..")
        # The Pydantic ValidationError message format is "Value error,
        # <our message>" — assert our substring is present.
        expect(
            app_token_field.get_by_text("xapp-", exact=False).last,
        ).to_be_visible(timeout=10_000)

        # ----- 5. No global error toast (422 routes inline) ---------
        # If channels.jsx's onError mis-routes 422 → toast path,
        # a kind=error toast would render. None should fire on 422.
        error_toasts = page.locator(".toast.toast-error")
        expect(error_toasts).to_have_count(0, timeout=2_000)

        # ----- 6. Modal stays OPEN — operator can retry ------------
        expect(modal).to_be_visible(timeout=2_000)

        # ----- 7. Fix the app_token + retry ------------------------
        slack_passwords.nth(0).fill("xapp-test-token-fixed")
        submit.click()

        # ----- 8. Success path — modal closes + navigates ----------
        expect(modal).not_to_be_visible(timeout=10_000)
        page.wait_for_url(
            f"**/console/#/channels/providers/{cp_id}**", timeout=15_000,
        )

        # ----- 9. Detail page sanity: Probe disabled with hint -----
        probe = page.get_by_role("button", name="Probe", exact=True).first
        expect(probe).to_be_visible(timeout=15_000)
        expect(probe).to_be_disabled()
        hint = probe.get_attribute("title")
        assert hint and "Probe endpoint not yet implemented" in hint, (
            f"Probe button should carry the roadmap §8 hint; "
            f"got: {hint!r}"
        )
    finally:
        _cleanup(base_url, [cp_id])
