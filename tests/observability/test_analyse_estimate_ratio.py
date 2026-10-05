"""The Phase 0 decision rule as a script (``scripts/analyse_estimate_ratio.py``), on synthetic ``llm_call`` records.

Each test writes the ``messages.jsonl`` a dogfood instance would, with a known ratio of the provider's figure to our
estimate, and asserts the branch of the rule that the figures select (and that the exclusions are applied).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import analyse_estimate_ratio as rule

CTX = 100_000
TRIGGER = rule.trigger_tokens(CTX)        # 0.9 x (100000 - 8192) = 82627
NEAR = int(0.5 * TRIGGER)                 # an estimate well past the 0.33 gate
START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _call(seq: int, day: float, *, ratio: float, provider="prov-a", model="m1", guard=None, ctx=CTX, est=NEAR, used=None):
    payload = {
        "profile_id": "prof", "provider_id": provider, "model": model,
        "input_tokens": used if used is not None else int(est * ratio), "output_tokens": 5,
        "estimated_input_tokens": est, "duration_ms": 10, "status": "ok",
    }
    if ctx is not None:
        payload["context_length"] = ctx
    if guard:
        payload["guard"] = guard
    when = (START + timedelta(days=day)).isoformat()
    return {"seq": seq, "kind": "llm_call", "payload": payload, "created_at": when}


def _terminal(seq: int, day: float):
    return {"seq": seq, "kind": "done", "payload": {"stop_reason": "stop"}, "created_at": (START + timedelta(days=day)).isoformat()}


def _write(tmp_path: Path, records: list[dict], name="s1") -> Path:
    path = tmp_path / name / "messages.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return tmp_path


def _turns(ratios, *, per_turn=1, days=8.0, **kw) -> list[dict]:
    """One llm_call per turn (``per_turn`` calls) over ``days`` days, then a done record."""
    out, seq, n = [], 1, len(ratios)
    for i, ratio in enumerate(ratios):
        day = days * i / max(1, n - 1)
        for _ in range(per_turn):
            out.append(_call(seq, day, ratio=ratio, **kw)); seq += 1
        out.append(_terminal(seq, day)); seq += 1
    return out


def _verdict(tmp_path, *args) -> dict:
    corpus = rule.read_corpus([tmp_path])
    groups, skipped = rule.build_groups(corpus, set())
    return rule.decide(corpus, groups, 7.0) | {"groups": groups, "skipped": skipped, "corpus": corpus}


def test_the_trigger_is_the_compaction_strategys():
    assert rule.trigger_tokens(131_072) == int(0.9 * (131_072 - 8_192))
    assert rule.trigger_tokens(8_192) == int(0.9 * (8_192 - 4_096)), "the reserve is clamped to half the window"
    assert rule.trigger_tokens(4_000) == int(0.9 * (4_000 - 2_000))


def test_an_accurate_estimator_means_do_not_build_phase_1(tmp_path):
    ratios = [0.95 + 0.10 * (i % 11) / 10 for i in range(300)]
    out = _verdict(_write(tmp_path, _turns(ratios)))
    assert out["verdict"] == "DO NOT BUILD PHASE 1", out["reason"]


def test_too_few_days_or_too_few_calls_is_insufficient_data(tmp_path):
    ratios = [1.0] * 300
    assert _verdict(_write(tmp_path, _turns(ratios, days=2.0)))["verdict"] == "INSUFFICIENT DATA"
    assert _verdict(_write(tmp_path / "b", _turns([1.0] * 50)))["verdict"] == "INSUFFICIENT DATA"


def test_a_steady_undercount_means_phase_1a_only(tmp_path):
    """The provider counts 1.3 to 1.5 times our estimate everywhere: wrong, but with a small spread (p90/p10 about 1.15)."""
    ratios = [1.30 + 0.20 * (i % 21) / 20 for i in range(300)]
    out = _verdict(_write(tmp_path, _turns(ratios)))
    assert out["verdict"] == "BUILD PHASE 1a ONLY" and out["reason"].startswith("rule 2")


def _wide(n=300):
    return [0.8 if i % 2 else 1.8 for i in range(n)]      # kappa 1.3, error about 0.38 after correction, spread 2.25


def test_a_wide_spread_with_replays_means_phase_1b(tmp_path):
    records = _turns(_wide())
    # one replay turn in every 100 near-window turns: 3 replays in 300 turns is 2 per 200
    seq = 10_000
    for i in range(3):
        records.append(_call(seq, 4.0 + i, ratio=1.0, guard="reduced")); seq += 1
        records.append(_terminal(seq, 4.0 + i)); seq += 1
    out = _verdict(_write(tmp_path, records))
    assert out["verdict"] == "BUILD PHASE 1b", out["reason"]
    assert out["facts"]["replay_turns"] == 3 and out["facts"]["replays_per_200_near_window_turns"] >= 1


def test_a_wide_spread_without_a_visible_consequence_is_still_phase_1a(tmp_path):
    out = _verdict(_write(tmp_path, _turns(_wide())))
    assert out["verdict"] == "BUILD PHASE 1a ONLY" and "no visible consequence" in out["reason"]


def test_premature_compactions_are_a_visible_consequence(tmp_path):
    records = _turns(_wide())
    seq = 20_000
    for _ in range(10):          # ten compactions fired at a real occupancy of 0.3 x trigger
        records.append(_call(seq, 5.0, ratio=1.0, est=int(0.3 * TRIGGER), used=int(0.3 * TRIGGER))); seq += 1
        records.append({"seq": seq, "kind": "compaction_marker", "payload": {}, "created_at": START.isoformat()}); seq += 1
    out = _verdict(_write(tmp_path, records))
    assert out["facts"]["premature_compaction_share"] == 1.0
    assert out["verdict"] == "BUILD PHASE 1b"


def test_guarded_calls_aggregated_profiles_and_excluded_providers_are_left_out(tmp_path):
    guarded = [_call(i + 1, 3.0, ratio=3.0, guard="reduced") for i in range(300)]
    # one file each (a file's seqs restart at 1): accurate, aggregated, an excluded provider, and 300 guarded calls
    for name, chunk in (("a", _turns([1.0] * 300)), ("b", _turns([3.0] * 300, provider=None, model=None)),
                        ("c", _turns([3.0] * 300, provider="ollama-1")), ("d", guarded)):
        _write(tmp_path, chunk, name=name)
    corpus = rule.read_corpus([tmp_path])
    groups, skipped = rule.build_groups(corpus, {"ollama-1"})
    assert [g.key for g in groups] == [("prov-a", "m1")]
    assert (skipped["aggregated"], skipped["excluded_provider"], skipped["guarded"]) == (300, 300, 300)
    assert rule.decide(corpus, groups, 7.0)["verdict"] == "DO NOT BUILD PHASE 1"


def test_a_record_without_a_context_length_is_counted_and_skipped(tmp_path):
    records = _turns([1.0] * 5, ctx=None)
    corpus = rule.read_corpus([_write(tmp_path, records)])
    assert corpus.without_context_length == 5
    groups, skipped = rule.build_groups(corpus, set())
    assert groups == [] and skipped["no_context_length"] == 5


def test_a_provider_whose_usage_falls_while_the_prompt_grows_is_demoted(tmp_path):
    """Rule 4: a cache-hit under-report (LM Studio, Ollama) shows as input_tokens that does not grow with the prompt."""
    records, seq = [], 1
    for turn in range(60):
        for k in range(3):
            est = NEAR + 2_000 * k
            used = int(est * 1.0) if k == 0 else int(est * 1.0) - 6_000 * k       # later calls report LESS
            records.append(_call(seq, 8.0 * turn / 59, ratio=1.0, est=est, used=used, provider="lms")); seq += 1
        records.append(_terminal(seq, 8.0 * turn / 59)); seq += 1
    groups, _ = rule.build_groups(rule.read_corpus([_write(tmp_path, records)]), set())
    (group,) = groups
    assert group.pairs == 120 and group.monotonic < 0.99 and group.anchor_allowed is False


def test_a_well_behaved_provider_keeps_its_anchor(tmp_path):
    records, seq = [], 1
    for turn in range(60):
        for k in range(3):
            est = NEAR + 2_000 * k
            records.append(_call(seq, 8.0 * turn / 59, ratio=1.1, est=est)); seq += 1
        records.append(_terminal(seq, 8.0 * turn / 59)); seq += 1
    groups, _ = rule.build_groups(rule.read_corpus([_write(tmp_path, records)]), set())
    (group,) = groups
    assert group.anchor_allowed is True and 0.7 <= group.delta_ratio <= 1.4


def test_the_cli_prints_a_verdict_and_json(tmp_path, capsys):
    _write(tmp_path, _turns([1.0] * 300))
    assert rule.main([str(tmp_path)]) == 0
    assert "VERDICT: DO NOT BUILD PHASE 1" in capsys.readouterr().out
    assert rule.main([str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "DO NOT BUILD PHASE 1"
