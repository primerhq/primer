"""UI E2E: New-policy modal Rego compile-error inline validation journey.

Closes the §2 (ii) feature directive coverage gap. U0110 walked the
LLM-judge path end-to-end; the OTHER inline-422 validation contract
on the policy modal — Rego compile errors render inline under the
`approval.policy` field — was previously uncovered.

The modal lives at /approvals → Policies tab → "New policy". When
the operator picks the "Policy (Rego)" type and submits with broken
Rego, the server's `_validate_approval_config` hook compiles the
Rego eagerly (matrix/api/routers/tool_approval.py:_validate_approval_config),
catches RegoCompileError, and re-raises as RequestValidationError
with loc=("approval", "policy"). The error envelope's fieldErrors
arrive on the client; approvals.jsx routes them under the matching
testid (approval-policy-err-body-approval-policy) and renders red
inline text beneath the textarea — NOT a global toast.

This journey:

  1. Navigate /approvals → click Policies tab.
  2. Click "New policy" → modal opens.
  3. Fill id + toolset + tool name (required fields).
  4. Click "Policy (Rego)" type chip — Rego textarea becomes
     visible.
  5. Type intentionally-broken Rego (no `package` clause +
     unclosed brace).
  6. Click "Create policy" → server rejects 422.
  7. Assert inline error appears under the Rego field
     (data-testid="approval-policy-err-body-approval-policy").
  8. Assert NO error toast appears (422 path routes inline, not
     to toast).
  9. Modal stays OPEN — operator can fix and retry.
  10. Replace the broken Rego with the canonical valid form
      (package primer.tool_approval; default required := false).
  11. Click "Create policy" → modal closes, "Policy created"
      toast appears, policies table contains the new row.

Multi-state UI test exercising the canSubmit gate + inline-error
routing + retry path. Cross-page consistency check at the end
(policies table reflects the create).

Covers backlog item U0114. Sibling of U0110 (LLM-judge happy
path) on the Rego inline-error axis; together they close the
§2 (ii) policy-modal validation contract for UI.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect


_BAD_REGO = "this is not rego {\n  unclosed"
# Package name MUST be primer.tool_approval — the validator queries
# data.primer.tool_approval, so any other package returns empty
# regopy output and surfaces as a 422 even when the syntax is OK.
_VALID_REGO = (
    "package primer.tool_approval\n"
    "\n"
    "default required := false\n"
)


def _cleanup(base_url: str, policy_ids: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for pid in policy_ids:
            try:
                c.delete(f"/v1/tool_approval_policies/{pid}")
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0114 — Policy modal inline Rego-422 validation + retry journey
# ===========================================================================


def test_u0114_policy_modal_rego_compile_error_renders_inline(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0114 — Multi-state UI journey: open the New-policy modal,
    submit invalid Rego, verify the inline error renders under the
    Rego field (not as a toast), then retry with valid Rego and
    confirm success.

    Pinned invariants:
      * 422 errors from /v1/tool_approval_policies POST route
        inline via fieldErrors[loc.join(".")] — not a global
        toast (chrome.jsx's pushToast is NOT invoked on 422).
      * Inline error testid pattern:
        approval-policy-err-{loc.replace(".","-")}.
      * Modal stays OPEN on 422 — operator can fix + retry.
      * Successful retry routes through the same modal; on 201
        the modal closes, the success toast fires, and the
        policies table refetches and surfaces the new row.

    Cleanup deletes the policy if it lands; no orphaned policy
    if the modal stays open.
    """
    policy_id = f"u0114-pol-{unique_suffix}"
    toolset_id = f"u0114-ts-{unique_suffix}"
    tool_name = f"u0114-tool-{unique_suffix}"

    try:
        # ----- 1. Navigate /approvals → Policies tab ----------------
        page.goto(
            f"{console_url}#/approvals",
            wait_until="domcontentloaded",
        )
        policies_tab = page.locator(
            "[data-testid='approvals-tab-policies']",
        )
        expect(policies_tab).to_be_visible(timeout=15_000)
        policies_tab.click()

        # ----- 2. New policy modal opens ----------------------------
        new_btn = page.get_by_role("button", name="New policy", exact=True)
        expect(new_btn).to_be_visible(timeout=10_000)
        new_btn.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)

        # ----- 3. Fill required identity fields ---------------------
        modal.locator("[data-testid='approval-policy-id']").fill(policy_id)
        # toolset override input is the second mono input in the
        # modal (first is id; this lets us use a unique user-defined
        # toolset so the test doesn't collide on the (toolset_id,
        # tool_name) uniqueness constraint).
        modal.locator("input.input.mono").nth(1).fill(toolset_id)
        modal.locator(
            "[data-testid='approval-policy-tool']",
        ).fill(tool_name)

        # ----- 4. Switch to Policy (Rego) type ---------------------
        modal.locator("[data-testid='approval-policy-type-policy']").click()

        # Rego textarea is now visible.
        rego_textarea = modal.locator(
            "[data-testid='approval-policy-rego']",
        )
        expect(rego_textarea).to_be_visible(timeout=5_000)

        # ----- 5. Type intentionally-broken Rego --------------------
        # The textarea has a default Rego template; clear it before
        # filling so the bad content fully replaces the seed.
        rego_textarea.fill(_BAD_REGO)

        # ----- 6. Submit → server rejects 422 ----------------------
        create_btn = modal.locator(
            "[data-testid='approval-policy-create'],"
        )
        # Avoid the comma artefact from the locator above — re-resolve.
        create_btn = modal.locator(
            "[data-testid='approval-policy-create']",
        )
        expect(create_btn).to_be_enabled(timeout=5_000)
        create_btn.click()

        # ----- 7. Inline error renders under the Rego field --------
        # The fieldErr() helper renders a div with testid
        # `approval-policy-err-{loc.replace(".","-")}`. For loc =
        # ("body","approval","policy") the testid is
        # `approval-policy-err-body-approval-policy`.
        inline_err = modal.locator(
            "[data-testid='approval-policy-err-body-approval-policy']",
        )
        expect(inline_err).to_be_visible(timeout=10_000)
        # The error text should reference Rego (the validator's
        # message format: "rego compile failed: ...").
        expect(inline_err).to_contain_text("rego", ignore_case=True)

        # ----- 8. No global error toast for 422 --------------------
        # 422 paths route inline only; a kind=error toast would
        # indicate the inline-vs-toast gating in approvals.jsx
        # broke. The 200ms wait below is just to let any toast
        # animations settle; if none fires, the locator stays
        # empty.
        error_toasts = page.locator(".toast.toast-error")
        expect(error_toasts).to_have_count(0, timeout=2_000)

        # ----- 9. Modal stays OPEN (operator can retry) ------------
        expect(modal).to_be_visible(timeout=2_000)

        # ----- 10. Fix the Rego + retry ---------------------------
        rego_textarea.fill(_VALID_REGO)
        create_btn.click()

        # ----- 11. Success path -----------------------------------
        # Modal closes (onSuccess in the create mutation).
        expect(modal).not_to_be_visible(timeout=10_000)
        # Success toast appears.
        success_toast = page.locator(
            ".toast", has_text="Policy created",
        )
        expect(success_toast).to_be_visible(timeout=10_000)
        # Policies table refetches and contains the new row.
        policy_row = page.locator(
            f"[data-testid='approvals-policy-row-{policy_id}']",
        )
        expect(policy_row).to_be_visible(timeout=10_000)
    finally:
        _cleanup(base_url, [policy_id])
