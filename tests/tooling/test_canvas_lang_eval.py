"""scripts/canvas_lang_eval.py: the Canvas Lang evaluation's pure core.

The live run (a temporary agent that builds each of the 20 scripted canvases) is lead-run before release; these tests pin the
pure parts: the case file's shape (exactly 20 unique ids across the three bands), the per-case score (a failed apply followed by
a successful one is a repair; the first apply's validity; the tokens sum from the llm_call records), and the Markdown table the
report is written from.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "canvas_lang_eval.py"


def _load():
    spec = importlib.util.spec_from_file_location("canvas_lang_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules first: the script's @dataclass fields resolve through sys.modules[cls.__module__].
    sys.modules["canvas_lang_eval"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("canvas_lang_eval", None)
    return module


script = _load()


def _cases_file(tmp_path: Path, *cases: dict) -> Path:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(list(cases)), encoding="utf-8")
    return path


def _tool_result(call_id: str, *, failed: bool) -> dict:
    if failed:
        output = json.dumps({"type": "validation-error", "message": "1 problem(s); nothing was applied"})
    else:
        output = json.dumps({"rev": 1, "changed": {"added": ["a"], "updated": [], "removed": []}, "warnings": []})
    return {"kind": "tool_result", "payload": {"call_id": call_id, "output": output, "error": failed}}


def _apply(call_id: str, *, failed: bool) -> list[dict]:
    return [
        {"kind": "tool_call", "payload": {"id": call_id, "name": "canvas__canvas_apply", "arguments": {"source": "root = Text('x')"}}},
        _tool_result(call_id, failed=failed),
    ]


def _eval_case() -> "script.EvalCase":
    return script.EvalCase(id="c01", band="simple", prompt="Build it.", expect={"types_at_least": {"Text": 1}})


class TestLoadCases:
    def test_a_valid_file_loads_twenty_in_band_order(self, tmp_path) -> None:
        cases = [
            {"id": f"c{i:02d}", "band": "simple" if i <= 7 else "medium" if i <= 14 else "complex", "prompt": f"Build {i}."}
            for i in range(1, 21)
        ]
        loaded = script.load_cases(_cases_file(tmp_path, *cases))
        assert len(loaded) == 20
        assert [c.id for c in loaded][:3] == ["c01", "c02", "c03"]
        assert loaded[6].band == "simple" and loaded[7].band == "medium" and loaded[19].band == "complex"

    def test_a_file_with_the_wrong_count_is_refused(self, tmp_path) -> None:
        cases = [{"id": f"c{i:02d}", "band": "simple", "prompt": "Build it."} for i in range(1, 20)]
        with pytest.raises(ValueError):
            script.load_cases(_cases_file(tmp_path, *cases))

    def test_duplicate_ids_are_refused(self, tmp_path) -> None:
        cases = [{"id": "c01", "band": "simple", "prompt": "A."}] * 20
        with pytest.raises(ValueError):
            script.load_cases(_cases_file(tmp_path, *cases))

    def test_an_unknown_band_is_refused(self, tmp_path) -> None:
        cases = [{"id": f"c{i:02d}", "band": "trivial", "prompt": "Build it."} for i in range(1, 21)]
        with pytest.raises(ValueError):
            script.load_cases(_cases_file(tmp_path, *cases))


class TestScoreCase:
    def test_a_failed_then_successful_apply_is_a_repair(self) -> None:
        records = _apply("call_1", failed=True) + _apply("call_2", failed=False)
        score = script.score_case(_eval_case(), records, {"elements": {"a": {"type": "Text"}}, "element_count": 1})
        assert score.first_apply_valid is False
        assert score.repaired is True
        assert score.apply_calls == 2 and score.failed_apply_calls == 1

    def test_a_clean_first_apply_is_valid_and_not_repaired(self) -> None:
        records = _apply("call_1", failed=False)
        score = script.score_case(_eval_case(), records, {"elements": {"a": {"type": "Text"}}, "element_count": 1})
        assert score.first_apply_valid is True
        assert score.repaired is False
        assert score.failed_apply_calls == 0

    def test_checks_pass_only_when_the_types_are_met(self) -> None:
        records = _apply("call_1", failed=False)
        ok = script.score_case(_eval_case(), records, {"elements": {"a": {"type": "Text"}}, "element_count": 1})
        assert ok.checks_passed is True
        missing = script.score_case(_eval_case(), records, {"elements": {}, "element_count": 0})
        assert missing.checks_passed is False

    def test_tokens_sum_from_the_llm_call_records(self) -> None:
        records = _apply("call_1", failed=False) + [
            {"kind": "llm_call", "payload": {"input_tokens": 100, "output_tokens": 20}},
            {"kind": "llm_call", "payload": {"input_tokens": 50, "output_tokens": 10}},
        ]
        score = script.score_case(_eval_case(), records, {"elements": {"a": {"type": "Text"}}, "element_count": 1})
        assert score.input_tokens == 150 and score.output_tokens == 30


class TestSummarizeAndRender:
    def _scores(self) -> list:
        return [
            script.CaseScore(case_id="c01", band="simple", first_apply_valid=True, apply_calls=1, failed_apply_calls=0,
                             repaired=False, checks_passed=True, input_tokens=10, output_tokens=5),
            script.CaseScore(case_id="c02", band="simple", first_apply_valid=False, apply_calls=2, failed_apply_calls=1,
                             repaired=True, checks_passed=True, input_tokens=20, output_tokens=10),
        ]

    def test_summarize_rates_and_bands(self) -> None:
        summary = script.summarize(self._scores())
        assert summary["cases"] == 2
        assert summary["validity_rate"] == 0.5
        assert summary["repair_rate"] == 0.5
        assert summary["pass_rate"] == 1.0
        assert summary["per_band"]["simple"]["cases"] == 2

    def test_render_markdown_has_the_rates_and_a_row_per_case(self) -> None:
        scores = self._scores()
        md = script.render_markdown(script.summarize(scores), scores)
        assert "Canvas Lang evaluation" in md
        assert "c01" in md and "c02" in md
        assert "Repair rate" in md and "| simple |" in md
