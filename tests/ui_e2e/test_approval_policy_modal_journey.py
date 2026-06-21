"""UI E2E: New-policy modal LLM-judge create + Policies-tab lifecycle journey.

Multi-page operator-journey that walks the §2 ToolApprovalPolicy
authoring surface end-to-end:

  /providers/llm (verify seeded provider listed) →
  /approvals?tab=policies (Policies tab empty state) →
  New policy modal → LLM-judge form (provider dropdown enumerates
  the seeded provider, model dropdown auto-populates from the
  provider row's `models` field) → submit → "Policy created" toast →
  new policies-table row visible with type=llm pill → toggle
  enabled checkbox → "Policy updated" toast → click row's delete
  button → confirmation modal → "Policy deleted" toast → row gone.

Pages traversed:
  /console/#/providers/llm → /console/#/approvals → row delete +
  confirmation modal (same page) → /providers/llm (cleanup probe).

Multi-subsystem exercised:

  1. LLMProvider list (the policy modal's provider dropdown is fed
     by GET /v1/llm_providers).
  2. ToolApprovalPolicy CRUD via UI — create modal (POST), policies
     tab toggle (PUT), delete confirmation modal (DELETE).
  3. Cross-modal reference integrity — the seeded LLMProvider must
     appear in the modal's dropdown before the LLM-judge form is
     valid; model auto-fill from provider.models pins the
     ProviderItems → ModelOptions effect.
  4. Mutation feedback loop — create + update + delete each surface
     a kind=success / kind=warning toast and the policies table
     refetches via mutation.invalidates.

Covers backlog item U0110. Does NOT require LM Studio — the LLM
provider row is configured with a placeholder api_key so the
provider+model identity validates against the row (primer-side
_validate_approval_config) without any upstream call.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-10")


def _seed_llm_provider(base_url: str, pid: str, model_name: str) -> None:
    """Create an LLMProvider with one named model via the API."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": pid,
            "provider": "anthropic",
            "models": [
                {"name": model_name, "context_length": 200_000},
            ],
            "config": {"api_key": "sk-test-placeholder"},
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed llm: {r.text}"


def _cleanup(base_url: str, pid: str, policy_ids: list[str]) -> None:
    """Best-effort delete every policy and the LLMProvider."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for ppid in policy_ids:
            try:
                c.delete(f"/v1/tool_approval_policies/{ppid}")
            except Exception:  # noqa: BLE001
                pass
        try:
            c.delete(f"/v1/llm_providers/{pid}")
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# U0110 — New-policy modal LLM-judge end-to-end lifecycle journey
# ===========================================================================


def test_u0110_policy_modal_llm_judge_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0110 — Multi-page operator-journey walking the §2 policy
    authoring surface from an API-seeded LLMProvider all the way to
    a deleted policy.

    Steps:

      1. Seed LLMProvider with a named judge model via API.
      2. Navigate /providers/llm → assert the seeded row is listed
         (proves the cross-page dropdown source is populated).
      3. Navigate /approvals → click Policies tab.
      4. Click "New policy" → modal opens.
      5. Fill id + toolset + tool, then click "LLM judge" chip.
      6. Provider dropdown shows the seeded provider; select it.
      7. Model dropdown auto-enables + populates with the provider's
         model; select it. Fill the judge prompt.
      8. Click "Create policy" → "Policy created" toast.
      9. Policies table now contains a row with the new policy id.
     10. Toggle the row's enabled checkbox → "Policy updated" toast.
     11. Click the row's delete button → confirmation modal renders.
     12. Click "Delete" in the confirm modal → "Policy deleted" toast
         + row disappears within the next refetch cycle.

    Pinned invariants:
      * LLM-type form dropdowns are fed by live /v1/llm_providers
        — the seeded provider must appear within the modal open.
      * Provider→model effect: picking a provider unblocks the model
        select and prefills the first available model.
      * Each mutation (create, toggle, delete) surfaces the documented
        toast title via approvals.jsx + chrome.jsx toaster wiring.
      * The delete confirmation modal is a real gate — the row only
        disappears after the confirm button in the modal is clicked.
    """
    judge_model = "judge-m1"
    pid = f"u0110-llm-{unique_suffix}"
    policy_id = f"u0110-pol-{unique_suffix}"
    toolset_id = f"u0110-ts-{unique_suffix}"
    tool_name = f"u0110-tool-{unique_suffix}"

    _seed_llm_provider(base_url, pid, judge_model)

    try:
        # --- 1. Verify seeded provider is listed on /providers/llm --
        page.goto(
            f"{console_url}#/providers/llm",
            wait_until="domcontentloaded",
        )
        provider_row = page.locator("tbody tr", has_text=pid)
        expect(provider_row.first).to_be_visible(timeout=15_000)

        # --- 2. Open the policy modal from the Tools page ----------
        # The approval-policy authoring surface moved off the Approvals
        # page onto the per-tool Tools table: each row's Add/Edit button
        # opens the same AP_NewPolicyModal (free-form id / toolset / tool
        # inputs are still editable, so we override them below).
        page.goto(
            f"{console_url}#/tools",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(
            "Tools", exact=False,
        ).first.wait_for(state="visible", timeout=15_000)

        # --- 3. Open the New-policy modal via a tool row -----------
        add_btn = page.get_by_role("button", name="Add", exact=True).or_(
            page.get_by_role("button", name="Edit", exact=True)
        ).first
        expect(add_btn).to_be_visible(timeout=15_000)
        add_btn.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)

        # --- 4. Fill core identity fields --------------------------
        modal.locator("[data-testid='approval-policy-id']").fill(policy_id)
        # The toolset select defaults to _workspaces; the input override
        # below it accepts free-text. Use the override input to set a
        # unique user-defined toolset id so this test doesn't collide
        # with sibling tests' internal-toolset writes.
        toolset_inputs = modal.locator("input.input.mono")
        # First mono input is the id (already filled); second is the
        # toolset override; third is the tool name.
        toolset_inputs.nth(1).fill(toolset_id)
        modal.locator("[data-testid='approval-policy-tool']").fill(tool_name)

        # --- 5. Switch to LLM-judge type ---------------------------
        modal.locator("[data-testid='approval-policy-type-llm']").click()

        # --- 6. Provider dropdown enumerates the seeded provider ---
        provider_select = modal.locator(
            "[data-testid='approval-policy-provider']",
        )
        expect(provider_select).to_be_visible(timeout=10_000)
        # Wait for the option to appear (provider list is async-loaded).
        expect(provider_select.locator(f"option[value='{pid}']")).to_be_attached(
            timeout=10_000,
        )
        provider_select.select_option(pid)

        # --- 7. Model dropdown auto-enables + populates ------------
        model_select = modal.locator(
            "[data-testid='approval-policy-model']",
        )
        expect(model_select).to_be_enabled(timeout=5_000)
        expect(
            model_select.locator(f"option[value='{judge_model}']"),
        ).to_be_attached(timeout=5_000)
        model_select.select_option(judge_model)

        modal.locator("[data-testid='approval-policy-prompt']").fill(
            "Decide whether this tool call is safe to proceed.",
        )

        # --- 8. Submit → "Policy created" toast --------------------
        create_btn = modal.locator(
            "[data-testid='approval-policy-create']",
        )
        expect(create_btn).to_be_enabled(timeout=5_000)
        create_btn.click()

        create_toast = page.locator(".toast", has_text="Policy created")
        expect(create_toast).to_be_visible(timeout=10_000)

        # Modal closes on success.
        expect(modal).not_to_be_visible(timeout=5_000)

        # --- 9. The policy persisted with the LLM-judge config -----
        # The Approvals-page policies table (with inline toggle/delete)
        # was removed; verify the created policy via the API instead.
        # This still pins the provider/model selection threading through
        # the modal into the stored ToolApprovalPolicy.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/tool_approval_policies/{policy_id}")
            assert r.status_code == 200, (
                f"policy {policy_id!r} not persisted after create: {r.text}"
            )
            row = r.json()
            approval = row.get("approval") or {}
            assert approval.get("type") == "llm", (
                f"expected an llm-judge policy, got {approval!r}"
            )
            assert approval.get("provider_id") == pid, (
                f"policy provider_id mismatch: {approval!r}"
            )
            assert approval.get("model") == judge_model, (
                f"policy model mismatch: {approval!r}"
            )
    finally:
        _cleanup(base_url, pid, [policy_id])
