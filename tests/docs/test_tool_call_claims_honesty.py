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
        "Default OFF",
        "graph binding executor is unsupported",
        "GATED decisions are unwired",
        "post-release crash",
    ):
        assert marker in description


def test_docs_name_every_surface_that_keeps_the_flag_off() -> None:
    claim_doc = _read("docs/dev/architecture/claim-machine.md")
    for marker in (
        "graph binding executor is unsupported",
        "GATED decisions are unwired",
        "cancel fan-out",
        "recovery reconciliation",
        "nested subagent",
        "post-release crash",
    ):
        assert marker in claim_doc

    graphs_doc = _read("docs/dev/subsystems/graphs.md")
    sessions_doc = _read("docs/dev/subsystems/sessions.md")
    assert "unsupported, and broken" in graphs_doc
    assert "default-off flag despite its built standalone executor" in sessions_doc
