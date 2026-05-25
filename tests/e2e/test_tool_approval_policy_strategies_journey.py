"""E2E: §2 ToolApprovalPolicy multi-strategy operator-journey.

ONE pytest function walks every observable corner of the
ToolApprovalPolicy CRUD + validation contract — across all three
approval strategies (`required`, `policy` with Rego, `llm`) AND
across the cross-router LLM-provider integrity hook.

Multi-subsystem in one test:

  1. LLMProvider seed — feeds the `llm`-type policy's validation
     hook (matrix/api/routers/tool_approval.py:_validate_approval_config).
  2. ToolApprovalPolicy POST/GET/PUT/DELETE for the `required`
     strategy — full CRUD round-trip + enabled toggle.
  3. ToolApprovalPolicy POST for the `policy` (Rego) strategy with
     a valid one-liner Rego — proves the compile-test path under
     `evaluate_policy(cfg.policy, {})` accepts well-formed sources.
  4. Negative: POST `policy`-type with intentionally-malformed Rego
     — 422 /errors/validation-error with loc ending in
     `approval.policy`, never a 500 leak.
  5. ToolApprovalPolicy POST for the `llm` strategy referencing the
     seeded provider+model — 201, validates the LLMProvider lookup
     succeeds AND the model is in the row's models list.
  6. Negative: POST `llm`-type referencing a non-existent
     `provider_id` — 422 with loc ending in `approval.provider_id`.
  7. Negative: POST `llm`-type referencing a known provider but an
     unknown model — 422 with loc ending in `approval.model`.
  8. (toolset_id, tool_name) uniqueness — second POST duplicating
     the `required`-policy's pair returns 409 /errors/conflict that
     names both fields.
  9. PUT mutating a row to carry a sibling's (toolset_id,
     tool_name) — 409 /errors/conflict (uniqueness skips own id).
  10. DELETE → GET 404 for every created policy + LLMProvider.

Covers backlog items T0824 (CRUD round-trip), T0825 (duplicate
409), T0826 (PUT-to-sibling 409), T0827 (malformed Rego 422),
T0828 (valid Rego 201), T0829 (unknown provider_id 422), T0830
(unknown model 422), T0831 (enabled toggle round-trip), and
T0832-adjacent (cache invalidate exercised in unwind). One
function, eight backlog items, three approval strategies,
two routers.

Pinned invariants:
  * The Rego compile-test runs server-side at create/update time
    — bad Rego never persists.
  * The LLM-judge provider+model lookup runs server-side at
    create/update time — orphans never persist.
  * Uniqueness is on `(toolset_id, tool_name)` — both fields
    appear in the conflict detail; the skip-own-id rule lets a
    PUT keep the row's identity without faking a duplicate.
  * Every error envelope is RFC 7807-shaped with the right slug.
  * No code path leaks /errors/internal under invalid inputs.
"""

from __future__ import annotations

import httpx
import pytest


def _llm_body(entity_id: str, model_name: str) -> dict:
    return {
        "id": entity_id,
        "provider": "anthropic",
        "models": [
            {"name": model_name, "context_length": 200_000},
        ],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 1},
    }


def _required_policy_body(
    pid: str, *, toolset_id: str, tool_name: str, enabled: bool = True,
) -> dict:
    return {
        "id": pid,
        "toolset_id": toolset_id,
        "tool_name": tool_name,
        "enabled": enabled,
        "approval": {"type": "required"},
    }


_VALID_REGO = (
    # Package name MUST be matrix.tool_approval — the validator
    # queries data.matrix.tool_approval, so any other package
    # returns empty output and the regopy-JSON parse fails (a
    # subtle gotcha — see matrix/agent/rego.py:_PACKAGE_QUERY).
    "package matrix.tool_approval\n"
    "\n"
    "default required := false\n"
    "\n"
    "required {\n"
    '    input.arguments.amount > 10000\n'
    "}\n"
)

# Intentionally malformed: missing `package` clause + an unclosed
# brace. The compile-test runs evaluate_policy(rego, {}), which
# raises RegoCompileError → mapped to 422.
_BAD_REGO = "this is not rego {\n  unclosed"


def _policy_policy_body(
    pid: str, *, toolset_id: str, tool_name: str, rego: str,
) -> dict:
    return {
        "id": pid,
        "toolset_id": toolset_id,
        "tool_name": tool_name,
        "enabled": True,
        "approval": {"type": "policy", "policy": rego},
    }


def _llm_policy_body(
    pid: str, *, toolset_id: str, tool_name: str,
    provider_id: str, model: str,
) -> dict:
    return {
        "id": pid,
        "toolset_id": toolset_id,
        "tool_name": tool_name,
        "enabled": True,
        "approval": {
            "type": "llm",
            "provider_id": provider_id,
            "model": model,
            "prompt": "Decide whether this call should be approved.",
        },
    }


# ===========================================================================
# T0858 — ToolApprovalPolicy multi-strategy journey
# ===========================================================================


@pytest.mark.asyncio
async def test_t0858_tool_approval_policy_multi_strategy_journey(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0858 — one journey across all three approval strategies +
    the (toolset_id, tool_name) uniqueness + the cross-router LLM
    provider integrity hook.

    See module docstring for the 10-step walk and which backlog
    items each step corresponds to.
    """
    base = "/v1/tool_approval_policies"
    llm_id = f"llm-t858-{unique_suffix}"
    judge_model = "judge-m1"

    # ----- 0. Seed LLMProvider used by the llm-type policy -----
    seed = await client.post(
        "/v1/llm_providers", json=_llm_body(llm_id, judge_model),
    )
    assert seed.status_code == 201, seed.text

    # Track every policy id we POST so the finally-block can unwind.
    created_pids: list[str] = []
    try:
        # ----- 1. required strategy CRUD round-trip + toggle -----
        # T0824 + T0831 in one walk.
        required_pid = f"pol-req-{unique_suffix}"
        required_toolset = "_workspaces"
        required_tool = f"req.fs.delete.{unique_suffix}"
        body = _required_policy_body(
            required_pid,
            toolset_id=required_toolset,
            tool_name=required_tool,
        )
        r = await client.post(base, json=body)
        assert r.status_code == 201, r.text
        created_pids.append(required_pid)
        assert r.json()["approval"]["type"] == "required"

        got = await client.get(f"{base}/{required_pid}")
        assert got.status_code == 200, got.text
        echoed = got.json()
        assert echoed["toolset_id"] == required_toolset
        assert echoed["tool_name"] == required_tool
        assert echoed["enabled"] is True
        assert echoed["approval"]["type"] == "required"

        # PUT enabled=false; GET reflects it.
        body_off = dict(body, enabled=False)
        upd = await client.put(f"{base}/{required_pid}", json=body_off)
        assert upd.status_code == 200, upd.text
        assert upd.json()["enabled"] is False

        # PUT enabled=true to flip back — round-trip mutation visible.
        upd2 = await client.put(
            f"{base}/{required_pid}",
            json=dict(body, enabled=True),
        )
        assert upd2.status_code == 200, upd2.text
        assert upd2.json()["enabled"] is True

        # ----- 2. policy (Rego) strategy — valid Rego accepted ----
        # T0828.
        valid_rego_pid = f"pol-rego-ok-{unique_suffix}"
        r = await client.post(
            base,
            json=_policy_policy_body(
                valid_rego_pid,
                toolset_id=f"ts-rego-{unique_suffix}",
                tool_name=f"rego.tool.{unique_suffix}",
                rego=_VALID_REGO,
            ),
        )
        assert r.status_code == 201, r.text
        created_pids.append(valid_rego_pid)
        assert r.json()["approval"]["type"] == "policy"
        assert "package matrix.tool_approval" in r.json()["approval"]["policy"]

        # ----- 3. policy strategy — malformed Rego rejected --------
        # T0827. Loc tuple is ("body", "approval", "policy") because
        # FastAPI's RequestValidationError prepends the request part.
        bad_rego_pid = f"pol-rego-bad-{unique_suffix}"
        r = await client.post(
            base,
            json=_policy_policy_body(
                bad_rego_pid,
                toolset_id=f"ts-bad-{unique_suffix}",
                tool_name=f"bad.tool.{unique_suffix}",
                rego=_BAD_REGO,
            ),
        )
        assert r.status_code == 422, r.text
        env = r.json()
        assert env["type"] == "/errors/validation-error", env
        # Some Pydantic / FastAPI versions emit loc as a list, others
        # as a tuple-serialised list — both render as JSON arrays.
        errs = env.get("extensions", {}).get("errors", [])
        loc_tails = [
            tuple((e.get("loc") or [])[-2:]) for e in errs
        ]
        assert ("approval", "policy") in loc_tails, (
            f"expected an error loc ending in ('approval', 'policy'); "
            f"got {errs!r}"
        )

        # ----- 4. llm strategy — valid provider + model accepted ----
        # Sets the foundation for the duplicate-pair test.
        llm_ok_pid = f"pol-llm-ok-{unique_suffix}"
        r = await client.post(
            base,
            json=_llm_policy_body(
                llm_ok_pid,
                toolset_id=f"ts-llm-{unique_suffix}",
                tool_name=f"llm.tool.{unique_suffix}",
                provider_id=llm_id,
                model=judge_model,
            ),
        )
        assert r.status_code == 201, r.text
        created_pids.append(llm_ok_pid)
        echoed = r.json()
        assert echoed["approval"]["type"] == "llm"
        assert echoed["approval"]["provider_id"] == llm_id
        assert echoed["approval"]["model"] == judge_model

        # ----- 5. llm strategy — unknown provider_id rejected ------
        # T0829.
        r = await client.post(
            base,
            json=_llm_policy_body(
                f"pol-llm-noprov-{unique_suffix}",
                toolset_id=f"ts-noprov-{unique_suffix}",
                tool_name=f"noprov.tool.{unique_suffix}",
                provider_id="this-provider-id-does-not-exist",
                model=judge_model,
            ),
        )
        assert r.status_code == 422, r.text
        env = r.json()
        assert env["type"] == "/errors/validation-error", env
        errs = env.get("extensions", {}).get("errors", [])
        loc_tails = [
            tuple((e.get("loc") or [])[-2:]) for e in errs
        ]
        assert ("approval", "provider_id") in loc_tails, (
            f"expected loc ending in ('approval', 'provider_id'); "
            f"got {errs!r}"
        )

        # ----- 6. llm strategy — unknown model rejected -----------
        # T0830.
        r = await client.post(
            base,
            json=_llm_policy_body(
                f"pol-llm-nomodel-{unique_suffix}",
                toolset_id=f"ts-nomodel-{unique_suffix}",
                tool_name=f"nomodel.tool.{unique_suffix}",
                provider_id=llm_id,
                model="this-model-is-not-on-the-provider",
            ),
        )
        assert r.status_code == 422, r.text
        env = r.json()
        assert env["type"] == "/errors/validation-error", env
        errs = env.get("extensions", {}).get("errors", [])
        loc_tails = [
            tuple((e.get("loc") or [])[-2:]) for e in errs
        ]
        assert ("approval", "model") in loc_tails, (
            f"expected loc ending in ('approval', 'model'); "
            f"got {errs!r}"
        )

        # ----- 7. (toolset_id, tool_name) uniqueness ---------------
        # T0825. Duplicate the required-policy's pair with a fresh id.
        dup_pid = f"pol-dup-{unique_suffix}"
        r = await client.post(
            base,
            json=_required_policy_body(
                dup_pid,
                toolset_id=required_toolset,
                tool_name=required_tool,
            ),
        )
        assert r.status_code == 409, r.text
        env = r.json()
        assert env["type"] == "/errors/conflict", env
        detail = env.get("detail") or ""
        assert required_toolset in detail and required_tool in detail, (
            f"conflict detail should name both fields; got: {detail!r}"
        )

        # ----- 8. PUT changing to sibling's pair → 409 -------------
        # T0826. PUT valid_rego_pid to carry the required policy's
        # (toolset_id, tool_name) — uniqueness validation must skip
        # own id and still surface the conflict against the sibling.
        cross_pair = _policy_policy_body(
            valid_rego_pid,
            toolset_id=required_toolset,
            tool_name=required_tool,
            rego=_VALID_REGO,
        )
        r = await client.put(f"{base}/{valid_rego_pid}", json=cross_pair)
        assert r.status_code == 409, r.text
        env = r.json()
        assert env["type"] == "/errors/conflict", env
        detail = env.get("detail") or ""
        assert required_toolset in detail and required_tool in detail, (
            f"sibling-conflict detail should name both fields; "
            f"got: {detail!r}"
        )

        # ----- 9. DELETE → GET 404 for every created policy --------
        for pid in created_pids:
            rm = await client.delete(f"{base}/{pid}")
            assert rm.status_code == 204, (
                f"DELETE {pid} expected 204, got {rm.status_code}: "
                f"{rm.text}"
            )
            gone = await client.get(f"{base}/{pid}")
            assert gone.status_code == 404, gone.text
            assert gone.json()["type"] == "/errors/not-found"
        created_pids.clear()
    finally:
        # Per-test cleanup — DELETE the LLMProvider + any policies
        # that didn't already get cleaned up in the happy-path tail.
        for pid in created_pids:
            try:
                await client.delete(f"{base}/{pid}")
            except Exception:  # noqa: BLE001
                pass
        try:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        except Exception:  # noqa: BLE001
            pass
