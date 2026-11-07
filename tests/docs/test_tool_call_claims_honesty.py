"""Static pins for the default-off tool-call claim capability contract."""

from __future__ import annotations

from pathlib import Path

from primer.int.claim import ClaimKind
from primer.model.scheduler import WorkerConfig

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_claim_machine_documents_all_five_shipped_kinds_and_adapters() -> None:
    expected = ["SESSION", "HARNESS", "TRIGGER", "TOOL_CALL", "MEMORY"]
    adapter_names = [
        "SessionClaimAdapter",
        "HarnessClaimAdapter",
        "TriggerClaimAdapter",
        "ToolCallClaimAdapter",
        "MemoryClaimAdapter",
    ]
    assert [kind.name for kind in ClaimKind] == expected

    claim_doc = _read("docs/dev/architecture/claim-machine.md")
    worker_doc = _read("docs/dev/architecture/worker-system.md")
    factory = _read("primer/claim/factory.py")
    assert "The five `ClaimKind` values" in claim_doc
    assert "The five shipped adapters:" in claim_doc
    assert "The five shipped adapters are" in worker_doc
    for kind, adapter in zip(expected, adapter_names, strict=True):
        assert f"`{kind}`" in claim_doc
        assert f"ClaimKind.{kind}: {adapter}(" in factory
        assert f"`{adapter}`" in worker_doc


def test_tool_call_handler_is_registered_and_documented_as_built() -> None:
    pool = _read("primer/worker/pool.py")
    claim_doc = _read("docs/dev/architecture/claim-machine.md")

    assert "ClaimKind.TOOL_CALL: self._run_engine_tool_call" in pool
    assert "`run_engine_tool_call` is registered unconditionally" in claim_doc
    assert "queued --> running: run_engine_tool_call" in claim_doc


def test_flag_default_and_schema_description_keep_the_capability_guardrails() -> None:
    field = WorkerConfig.model_fields["tool_calls_as_claims_enabled"]
    schema = WorkerConfig.model_json_schema()["properties"]["tool_calls_as_claims_enabled"]
    description = field.description or ""

    assert field.default is False
    assert schema["default"] is False
    assert schema["description"] == description
    for marker in (
        "Standalone non-gated agent batches are implemented",
        "only when a batch has at least one claimable call",
        "born-terminal row for each notifying sibling",
        "all-notifying batch stays in-process and creates no ToolCallTask rows",
        "Default OFF",
        "graph binding executor is unsupported",
        "GATED decisions are unwired",
        "Exact boot and leader-periodic reconciliation repairs marked terminal-batch wake loss",
        "pre-marker rows retain the 3600-second park-timeout compatibility boundary",
    ):
        assert marker in description

    human_field = WorkerConfig.model_fields["task_gate_human_decisions_enabled"]
    human_schema = WorkerConfig.model_json_schema()["properties"]["task_gate_human_decisions_enabled"]
    assert human_field.default is False
    assert human_schema["default"] is False
    assert "not available before P3" in (human_field.description or "")


def test_s0_docs_and_code_disable_human_task_gate_surfaces() -> None:
    claim_doc = _read("docs/dev/architecture/claim-machine.md")
    worker_doc = _read("docs/dev/architecture/worker-system.md")
    router = _read("primer/api/routers/tool_approval.py")
    executor = _read("primer/worker/engine_handlers.py")
    for content in (claim_doc, worker_doc):
        assert "S0" in content
        assert "responder ordering P3" in content
    assert "if _task_gate_human_decisions_enabled(request):" in router
    assert "not _task_gate_human_execution_enabled(pool)" in executor


def test_all_notifying_batches_are_documented_as_staying_out_of_the_claims_path() -> None:
    for relative in (
        "docs/dev/architecture/claim-machine.md",
        "docs/dev/architecture/worker-system.md",
    ):
        content = _read(relative)
        assert "at least one claimable call" in content
        assert "born-terminal row for each notifying sibling" in content
        assert "all-notifying batch stays in-process" in content
        assert "no `ToolCallTask` rows" in content


def test_docs_name_every_surface_that_keeps_the_flag_off() -> None:
    claim_doc = _read("docs/dev/architecture/claim-machine.md")
    for marker in (
        "graph binding executor is unsupported",
        "GATED decisions are unwired",
        "cancel fan-out",
        "nested subagent",
    ):
        assert marker in claim_doc
    assert "Exact terminal-batch wake reconciliation is now implemented" in claim_doc
    assert "pre-marker rows retain the 3600-second timeout boundary" in claim_doc

    graphs_doc = _read("docs/dev/subsystems/graphs.md")
    sessions_doc = _read("docs/dev/subsystems/sessions.md")
    assert "unsupported, and broken" in graphs_doc
    assert "default-off flag despite its built standalone executor" in sessions_doc
