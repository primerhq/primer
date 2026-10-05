"""The Phase 0 decision rule as a script (``scripts/analyse_estimate_ratio.py``), on records built the way persistence writes them.

Fixtures come from ``translate_stream_event``: an ``llm_call`` record for every model call and a ``done`` record AFTER EVERY
CALL (``tool_use`` for a tool round, ``stop`` for the last one of a turn), as the loop and ``persistence.py`` produce them. An
earlier version of these tests wrote one ``done`` per turn, which a real instance never does, so turn segmentation and rule 4
passed on fixtures and could not work on data. The boundary tests pin the rule's constants, percentiles and quantifiers with
asymmetric distributions, so a mis-edit that turns a BUILD into DO NOT BUILD fails here.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from primer.model.chat import Done, ExtendedEvent, _LlmCall
from primer.model.workspace_session import SessionMessageKind, SessionMessageRecord
from primer.session.persistence import _CoalesceState, translate_stream_event
from scripts import analyse_estimate_ratio as rule

CTX = 100_000
TRIGGER = rule.trigger_tokens(CTX)        # 0.9 x (100000 - 8192) = 82627
NEAR = int(0.5 * TRIGGER)                 # an estimate well past the 0.33 gate
START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _stamp(day: float) -> datetime:
    return START + timedelta(days=day)


def _llm_call(seq: int, day: float, *, ratio=1.0, est=NEAR, used=None, provider="prov-a", model="m1", guard="none",
              ctx=CTX, node=None) -> SessionMessageRecord:
    """The record ``translate_stream_event`` writes for one model call."""
    event = ExtendedEvent(extended=_LlmCall(
        profile_id="prof", provider_id=provider, model=model,
        input_tokens=used if used is not None else int(est * ratio), output_tokens=5, estimated_input_tokens=est,
        context_length=ctx, guard=guard, duration_ms=10, status="ok",
    ))
    rec = translate_stream_event(event, _CoalesceState(), node)
    assert isinstance(rec, SessionMessageRecord)
    return rec.model_copy(update={"seq": seq, "created_at": _stamp(day)})


def _done(seq: int, day: float, stop_reason: str, node=None) -> SessionMessageRecord:
    rec = translate_stream_event(Done(stop_reason=stop_reason, raw_reason=stop_reason), _CoalesceState(), node)
    assert isinstance(rec, SessionMessageRecord)
    return rec.model_copy(update={"seq": seq, "created_at": _stamp(day)})


def _marker(seq: int, day: float, *, before: int, trigger: int) -> SessionMessageRecord:
    return SessionMessageRecord(
        seq=seq, kind=SessionMessageKind.COMPACTION_MARKER, created_at=_stamp(day),
        payload={"summary": "s", "tokens_before": before, "tokens_after": 1, "trigger_tokens": trigger, "outcome": "summarised"},
    )


class Session:
    """Builds one ``messages.jsonl`` call by call, the way a turn writes it: a ``done`` after EVERY call."""

    def __init__(self) -> None:
        self.records: list[SessionMessageRecord] = []

    def turn(self, calls: list[dict], *, day: float, node=None) -> Session:
        for k, kwargs in enumerate(calls):
            self.records.append(_llm_call(len(self.records) + 1, day, node=node, **kwargs))
            last = k == len(calls) - 1
            self.records.append(_done(len(self.records) + 1, day, "stop" if last else "tool_use", node=node))
        return self

    def marker(self, *, day: float, before: int, trigger: int = TRIGGER) -> Session:
        self.records.append(_marker(len(self.records) + 1, day, before=before, trigger=trigger))
        return self

    def write(self, root: Path, name: str = "s1") -> Path:
        path = root / name / "messages.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(r.model_dump_json() for r in self.records) + "\n", encoding="utf-8")
        return root


def _singles(ratios, *, days=8.0, **kw) -> Session:
    """One call per turn over ``days`` days."""
    s, n = Session(), len(ratios)
    for i, r in enumerate(ratios):
        s.turn([{"ratio": r, **kw}], day=days * i / max(1, n - 1))
    return s


def _dist(*spec: tuple[int, float]) -> list[float]:
    return [value for count, value in spec for _ in range(count)]


def _run(root: Path, *, exclude=(), min_days=7.0) -> dict:
    corpus = rule.read_corpus([root])
    groups, skipped = rule.build_groups(corpus, set(exclude))
    return rule.decide(corpus, groups, min_days) | {"groups": groups, "skipped": skipped, "corpus": corpus}


def _verdict(tmp_path, ratios, **kw) -> str:
    return _run(_singles(ratios, **kw).write(tmp_path))["verdict"]


DNB, ONLY_1A, BUILD_1B, NO_DATA = "DO NOT BUILD PHASE 1", "BUILD PHASE 1a ONLY", "BUILD PHASE 1b", "INSUFFICIENT DATA"


def test_the_fixtures_are_what_persistence_writes():
    s = Session().turn([{}, {}, {}], day=0)
    assert [r.kind.value for r in s.records] == ["llm_call", "done"] * 3
    assert [r.payload["stop_reason"] for r in s.records if r.kind.value == "done"] == ["tool_use", "tool_use", "stop"]
    call = s.records[0].payload
    assert call["context_length"] == CTX and "guard" not in call and call["estimated_input_tokens"] == NEAR


def test_the_trigger_is_the_compaction_strategys():
    assert rule.trigger_tokens(131_072) == int(0.9 * (131_072 - 8_192))
    assert rule.trigger_tokens(8_192) == int(0.9 * (8_192 - 4_096)), "the reserve is clamped to half the window"
    assert rule.trigger_tokens(4_000) == int(0.9 * (4_000 - 2_000))


# ---- the data requirement -------------------------------------------------------------------------------------------

def test_an_accurate_estimator_means_do_not_build_phase_1(tmp_path):
    assert _verdict(tmp_path, [0.95 + 0.10 * (i % 11) / 10 for i in range(300)]) == DNB


def test_too_few_calls_is_insufficient_data(tmp_path):
    assert _verdict(tmp_path, [1.0] * 50) == NO_DATA


def test_too_few_days_of_usable_calls_is_insufficient_data(tmp_path):
    assert _verdict(tmp_path, [1.0] * 300, days=2.0) == NO_DATA


def test_weeks_of_records_the_script_cannot_use_do_not_make_up_the_span(tmp_path):
    """Probe B of the review: 30 days of records without a context_length, then 300 usable accurate calls in two hours."""
    s = Session()
    for i in range(300):
        s.records.append(_llm_call(len(s.records) + 1, 30.0 * i / 299, ctx=None))
        s.records.append(_done(len(s.records) + 1, 30.0 * i / 299, "stop"))
    for i in range(300):
        s.turn([{"ratio": 1.0}], day=30.0 + (2 / 24) * i / 299)
    out = _run(s.write(tmp_path))
    assert out["verdict"] == NO_DATA and out["skipped"]["no_context_length"] == 300
    assert out["facts"]["days_of_usable_data (shortest material group)"] < 1


def test_an_excluded_providers_days_do_not_make_up_the_span_either(tmp_path):
    """Probe C: Ollama spans 8 days and is excluded; the group that counts spans an hour."""
    s = Session()
    for i in range(300):
        s.turn([{"ratio": 1.0, "provider": "ollama-1"}], day=8.0 * i / 299)
    for i in range(300):
        s.turn([{"ratio": 1.0}], day=8.0 + (1 / 24) * i / 299)
    assert _run(s.write(tmp_path), exclude={"ollama-1"})["verdict"] == NO_DATA


def test_seven_days_of_usable_calls_in_every_material_group_is_enough(tmp_path):
    """The control for the two tests above: the same shape with a week of usable calls."""
    s = Session()
    for i in range(300):
        s.turn([{"ratio": 1.0}], day=7.5 * i / 299)
    assert _run(s.write(tmp_path))["verdict"] == DNB


# ---- rule 1: the thresholds, the percentiles, the direction of the ratio ----------------------------------------------

def test_p10_just_below_the_floor_is_not_do_not_build(tmp_path):
    """40 of 300 at 0.84 (p10 is 0.84, p25 is 1.0): kills a floor of 0.80 and a p25 in place of p10."""
    assert _verdict(tmp_path, _dist((40, 0.84), (260, 1.0))) == ONLY_1A


def test_p10_just_above_the_floor_with_a_deep_thin_tail_is_do_not_build(tmp_path):
    """3 of 300 at 0.50 and 87 at 0.86: p10 is 0.86, p0 is 0.50 (kills a p0 and a floor of 0.90)."""
    assert _verdict(tmp_path, _dist((3, 0.50), (87, 0.86), (210, 1.0))) == DNB


def test_p90_just_above_the_ceiling_is_not_do_not_build(tmp_path):
    """40 of 300 at 1.16 (p90 is 1.16, p75 is 1.0): kills a ceiling of 1.20 and a p75 in place of p90."""
    assert _verdict(tmp_path, _dist((260, 1.0), (40, 1.16))) == ONLY_1A


def test_p90_just_below_the_ceiling_with_a_high_thin_tail_is_do_not_build(tmp_path):
    """3 of 300 at 1.8 and 87 at 1.08: p90 is 1.08, p100 is 1.8 (kills a p100 and a ceiling of 1.04)."""
    assert _verdict(tmp_path, _dist((210, 1.0), (87, 1.08), (3, 1.8))) == DNB


def test_the_ratio_is_usage_over_estimate_not_the_reverse(tmp_path):
    """A uniform 0.9 (our estimate 11% above the provider's) is within the band; inverted it would be 1.11, outside it."""
    assert _verdict(tmp_path, [0.9] * 300) == DNB
    assert _verdict(tmp_path / "b", [1.2] * 300) == ONLY_1A


def test_a_group_below_the_material_share_cannot_block_do_not_build(tmp_path):
    """Two groups: an accurate one, and an inaccurate one at 4% of the near-window calls (under the 5% bar)."""
    s = Session()
    for i in range(3800):
        s.turn([{"ratio": 1.0, "provider": "big"}], day=8.0 * i / 3799)
    for i in range(160):                         # 160 / 3960 = 4.0%
        s.turn([{"ratio": 1.6, "provider": "small", "model": "m2"}], day=8.0 * i / 159)
    out = _run(s.write(tmp_path))
    assert {g.key[0]: g.material for g in out["groups"]} == {"big": True, "small": False}
    assert out["verdict"] == DNB


def test_one_material_inaccurate_group_blocks_do_not_build_whatever_the_others_do(tmp_path):
    """The same two groups with the small one at 6% (material, and 240 calls so it has enough): every material group must pass."""
    s = Session()
    for i in range(3800):
        s.turn([{"ratio": 1.0, "provider": "big"}], day=8.0 * i / 3799)
    for i in range(240):                         # 240 / 4040 = 5.9%
        s.turn([{"ratio": 1.6, "provider": "small", "model": "m2"}], day=8.0 * i / 239)
    out = _run(s.write(tmp_path))
    assert {g.key[0]: g.material for g in out["groups"]} == {"big": True, "small": True}
    assert out["verdict"] == ONLY_1A


def test_the_near_window_gate_is_a_third_of_the_trigger(tmp_path):
    """Calls with an estimate just under 0.33 x trigger are not near-window: a thousand wildly wrong ones change nothing."""
    s = Session()
    for i in range(300):
        s.turn([{"ratio": 1.0}], day=8.0 * i / 299)
    for i in range(300):
        s.turn([{"ratio": 3.0, "est": int(0.32 * TRIGGER)}], day=8.0 * i / 299)
    assert _run(s.write(tmp_path))["verdict"] == DNB
    t = Session()
    for i in range(300):
        t.turn([{"ratio": 1.0}], day=8.0 * i / 299)
    for i in range(300):
        t.turn([{"ratio": 3.0, "est": int(0.34 * TRIGGER)}], day=8.0 * i / 299)
    assert _run(t.write(tmp_path / "b"))["verdict"] != DNB


# ---- rules 2 and 3 ----------------------------------------------------------------------------------------------------

def test_a_steady_undercount_means_phase_1a_only(tmp_path):
    out = _run(_singles([1.30 + 0.20 * (i % 21) / 20 for i in range(300)]).write(tmp_path))
    assert out["verdict"] == ONLY_1A and out["reason"].startswith("rule 2")


def _wide(n=300):
    return [0.8 if i % 2 else 1.8 for i in range(n)]      # kappa about 1.3, error 0.38 after correction, spread 2.25


def _with_replays(s: Session, count: int, day: float = 4.0) -> Session:
    for i in range(count):
        s.turn([{"ratio": 1.0, "guard": "reduced"}], day=day + i)
    return s


def test_a_wide_spread_with_replays_means_phase_1b(tmp_path):
    # 3 replay turns beside 300 near-window turns: about 2 per 200
    out = _run(_with_replays(_singles(_wide()), 3).write(tmp_path))
    assert out["verdict"] == BUILD_1B, out["reason"]
    assert out["facts"]["replay_turns"] == 3 and out["facts"]["replays_per_200_near_window_turns"] >= 1


def test_a_wide_spread_without_a_visible_consequence_is_still_phase_1a(tmp_path):
    out = _run(_singles(_wide()).write(tmp_path))
    assert out["verdict"] == ONLY_1A and "no visible consequence" in out["reason"]


def test_kappa_is_the_group_median_not_the_mean(tmp_path):
    """45% at 1.0 and 55% at 1.3: the median is 1.3 (error 0.23 after correction, so it varies), the mean 1.165 (error 0.14,
    which would not)."""
    ratios = _dist((135, 1.0), (165, 1.3))
    out = _run(_with_replays(_singles(ratios), 3).write(tmp_path))
    assert out["verdict"] == BUILD_1B, out["reason"]


def test_a_replay_turn_is_counted_once_however_many_calls_it_made(tmp_path):
    s = _singles(_wide())
    s.turn([{"ratio": 1.0, "guard": "reduced"}] * 4, day=4.0)      # one turn, four guarded calls
    assert _run(s.write(tmp_path))["facts"]["replay_turns"] == 1


def test_only_compactions_the_trigger_fired_count_towards_premature(tmp_path):
    """Ten manual or overflow-forced markers (tokens_before under the trigger) at low occupancy are not a consequence;
    ten trigger-fired ones are."""
    forced = _singles(_wide())
    for _ in range(10):
        forced.turn([{"ratio": 1.0, "est": int(0.3 * TRIGGER)}], day=5.0)
        forced.marker(day=5.0, before=int(0.3 * TRIGGER))
    out = _run(forced.write(tmp_path))
    assert out["facts"]["trigger_fired_compactions"] == 0 and out["facts"]["manual_or_forced_markers_not_counted"] == 10
    assert out["verdict"] == ONLY_1A
    fired = _singles(_wide())
    for _ in range(10):
        fired.turn([{"ratio": 1.0, "est": int(0.3 * TRIGGER)}], day=5.0)
        fired.marker(day=5.0, before=TRIGGER + 10)
    out = _run(fired.write(tmp_path / "b"))
    assert out["facts"]["premature_compaction_share"] == 1.0 and out["verdict"] == BUILD_1B


def test_occupancy_exactly_at_three_quarters_of_the_trigger_is_not_premature(tmp_path):
    """16384 gives a trigger of 7372, three quarters of which is the whole number 5529: ``<`` and not ``<=``."""
    ctx = 16_384
    trigger = rule.trigger_tokens(ctx)
    assert trigger == 7372 and 0.75 * trigger == 5529
    s = _singles(_wide())
    for _ in range(10):
        s.turn([{"ratio": 1.0, "est": 5529, "used": 5529, "ctx": ctx}], day=5.0)
        s.marker(day=5.0, before=trigger, trigger=trigger)
    assert _run(s.write(tmp_path))["facts"]["premature_compaction_share"] == 0.0


# ---- the exclusions and the inputs --------------------------------------------------------------------------------------

def test_guarded_calls_aggregated_profiles_and_excluded_providers_are_left_out(tmp_path):
    chunks = {
        "a": _singles([1.0] * 300),
        "b": _singles([3.0] * 300, provider=None, model=None),
        "c": _singles([3.0] * 300, provider="ollama-1"),
        "d": _singles([3.0] * 300, guard="reduced"),
    }
    for name, s in chunks.items():
        s.write(tmp_path, name)
    corpus = rule.read_corpus([tmp_path])
    groups, skipped = rule.build_groups(corpus, {"ollama-1"})
    assert [g.key for g in groups] == [("prov-a", "m1")]
    assert (skipped["aggregated"], skipped["excluded_provider"], skipped["guarded"]) == (300, 300, 300)
    assert rule.decide(corpus, groups, 7.0)["verdict"] == DNB


def test_a_record_without_a_context_length_is_counted_and_skipped(tmp_path):
    corpus = rule.read_corpus([_singles([1.0] * 5, ctx=None).write(tmp_path)])
    assert corpus.without_context_length == 5
    groups, skipped = rule.build_groups(corpus, set())
    assert groups == [] and skipped["no_context_length"] == 5


def test_overlapping_roots_are_read_once(tmp_path):
    _singles([1.0] * 150).write(tmp_path / "sessions", "s1")
    once = rule.read_corpus([tmp_path])
    twice = rule.read_corpus([tmp_path, tmp_path / "sessions", tmp_path / "sessions" / "s1" / "messages.jsonl"])
    assert len(once.calls) == len(twice.calls) == 150 and twice.files == 1


# ---- rule 4, on turns of several calls with a done after each ---------------------------------------------------------------

def _runs(tmp_path, *, calls_per_turn=3, turns=60, step=2_000, used=lambda k, est: est, node=None, split=False) -> rule.Group:
    s = Session()
    for turn in range(turns):
        s.turn([{"est": NEAR + step * k, "used": used(k, NEAR + step * k)} for k in range(calls_per_turn)],
               day=8.0 * turn / (turns - 1), node=node)
    groups, _ = rule.build_groups(rule.read_corpus([s.write(tmp_path)]), set())
    (group,) = groups
    return group


def test_pairs_are_counted_within_a_turn_that_has_a_done_after_every_call(tmp_path):
    """Probe A of the review: with a done after each call a turn split on every done found 0 pairs."""
    group = _runs(tmp_path)
    assert group.pairs == 120 and group.anchor_allowed is True


def test_a_provider_whose_usage_falls_while_the_prompt_grows_is_demoted(tmp_path):
    """A cache-hit under-report (LM Studio, Ollama) shows as usage that does not grow with the prompt."""
    group = _runs(tmp_path, used=lambda k, est: est - 6_000 * k)
    assert group.pairs == 120 and group.monotonic < 0.99 and group.anchor_allowed is False


def test_two_percent_of_pairs_going_backwards_demotes_the_provider(tmp_path):
    """200 pairs, 4 of them decreasing: 0.98 non-decreasing is under the 0.99 floor (kills a floor of 0.5)."""
    s = Session()
    for turn in range(100):
        # pairs within a turn: (k0, k1), (k1, k2); one turn in 25 has its second pair going backwards
        backwards = turn % 25 == 0
        calls = [
            {"est": NEAR, "used": NEAR}, {"est": NEAR + 2_000, "used": NEAR + 2_000},
            {"est": NEAR + 4_000, "used": NEAR + 2_000 - 500 if backwards else NEAR + 4_000},
        ]
        s.turn(calls, day=8.0 * turn / 99)
    (group,) = rule.build_groups(rule.read_corpus([s.write(tmp_path)]), set())[0]
    assert group.pairs == 200 and group.monotonic == 0.98 and group.anchor_allowed is False


def test_a_median_delta_ratio_outside_the_band_demotes_the_provider(tmp_path):
    """Usage that grows 0.65 or 1.5 times as fast as our estimate does is outside [0.7, 1.4]; 1.0 is inside."""
    low = _runs(tmp_path / "lo", used=lambda k, est: NEAR + int(0.65 * 2_000 * k))
    high = _runs(tmp_path / "hi", used=lambda k, est: NEAR + int(1.5 * 2_000 * k))
    inside = _runs(tmp_path / "in", used=lambda k, est: NEAR + int(1.0 * 2_000 * k))
    assert (low.anchor_allowed, high.anchor_allowed, inside.anchor_allowed) == (False, False, True)
    assert 0.6 < low.delta_ratio < 0.7 and 1.4 < high.delta_ratio < 1.6


def test_too_few_pairs_say_nothing(tmp_path):
    assert _runs(tmp_path, turns=5).anchor_allowed is None


def test_graph_nodes_do_not_split_each_others_turns_or_pair_across_nodes(tmp_path):
    """Two nodes interleave their calls in one file: a node's done must not end the other node's turn, and a pair is two
    calls of ONE node."""
    s = Session()
    for turn in range(40):
        day = 8.0 * turn / 39
        # node A: three calls; node B: one call in the middle, with its own done (stop)
        s.records.append(_llm_call(len(s.records) + 1, day, est=NEAR, node="A"))
        s.records.append(_done(len(s.records) + 1, day, "tool_use", node="A"))
        s.records.append(_llm_call(len(s.records) + 1, day, est=NEAR, used=10, node="B"))
        s.records.append(_done(len(s.records) + 1, day, "stop", node="B"))
        s.records.append(_llm_call(len(s.records) + 1, day, est=NEAR + 2_000, node="A"))
        s.records.append(_done(len(s.records) + 1, day, "stop", node="A"))
    (group,) = rule.build_groups(rule.read_corpus([s.write(tmp_path)]), set())[0]
    assert group.pairs == 40, "one pair per node-A turn; node B's calls are alone in their turns"


def test_a_compaction_marker_between_two_calls_is_not_a_pure_append(tmp_path):
    s = Session()
    for turn in range(40):
        day = 8.0 * turn / 39
        s.records.append(_llm_call(len(s.records) + 1, day, est=NEAR))
        s.records.append(_done(len(s.records) + 1, day, "tool_use"))
        s.marker(day=day, before=TRIGGER + 1)
        s.records.append(_llm_call(len(s.records) + 1, day, est=NEAR // 2 + NEAR, used=NEAR // 3))
        s.records.append(_done(len(s.records) + 1, day, "stop"))
    (group,) = rule.build_groups(rule.read_corpus([s.write(tmp_path)]), set())[0]
    assert group.pairs == 0


# ---- the command line -------------------------------------------------------------------------------------------------------

def test_the_cli_prints_a_verdict_json_and_warns_when_no_provider_is_excluded(tmp_path, capsys):
    _singles([1.0] * 300).write(tmp_path)
    assert rule.main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert f"VERDICT: {DNB}" in captured.out and "provider ids seen: prov-a" in captured.out
    assert "no --exclude-provider given" in captured.err
    assert rule.main([str(tmp_path), "--exclude-provider", "nothing", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["verdict"] == DNB and "no --exclude-provider" not in captured.err
