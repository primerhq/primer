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

    def delegated(
        self, calls: list[dict], delegate_id: str, *, day: float, run_id: str | None = None, ends_turn: bool = True,
    ) -> Session:
        """A subagent run recorded INLINE in this log, as ``DelegationRecorder`` writes it: node_id null and every record
        (the done as well) stamped ``delegated`` with the delegating call's id, and with the run's id when given (a log written
        since the recorder stamps one). ``ends_turn=False`` leaves the last call a tool round (the run continues later)."""
        for k, kwargs in enumerate(calls):
            last = ends_turn and k == len(calls) - 1
            for rec in (
                _llm_call(len(self.records) + 1, day, **kwargs),
                _done(len(self.records) + 2, day, "stop" if last else "tool_use"),
            ):
                rec.payload["delegated"] = True
                rec.payload["delegate_tool_call_id"] = delegate_id
                if run_id is not None:
                    rec.payload["delegate_run_id"] = run_id
                self.records.append(rec)
        return self

    def terminal(self, kind: str, *, day: float) -> Session:
        """A turn that ended without a ``done``: a Stop or Cancel (``cancelled``) or a failure (``error``)."""
        self.records.append(SessionMessageRecord(
            seq=len(self.records) + 1, kind=SessionMessageKind(kind), payload={"message": "x"}, created_at=_stamp(day),
        ))
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
    """Calls with an estimate one token under 0.33 x trigger are not near-window (a gate of 0.32 would admit them): hundreds of
    wildly wrong ones change nothing; one token over, they decide."""
    s = Session()
    for i in range(300):
        s.turn([{"ratio": 1.0}], day=8.0 * i / 299)
    for i in range(300):
        s.turn([{"ratio": 3.0, "est": int(0.33 * TRIGGER) - 1}], day=8.0 * i / 299)
    assert _run(s.write(tmp_path))["verdict"] == DNB
    t = Session()
    for i in range(300):
        t.turn([{"ratio": 1.0}], day=8.0 * i / 299)
    for i in range(300):
        t.turn([{"ratio": 3.0, "est": int(0.33 * TRIGGER) + 1}], day=8.0 * i / 299)
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


def test_occupancy_one_token_under_three_quarters_of_the_trigger_is_premature(tmp_path):
    """ctx 16384: trigger 7372, three quarters 5529. 5528 is premature (a threshold of 0.65 to 0.74 would not call it so)."""
    ctx = 16_384
    trigger = rule.trigger_tokens(ctx)
    s = _singles(_wide())
    for _ in range(10):
        s.turn([{"ratio": 1.0, "est": 5528, "used": 5528, "ctx": ctx}], day=5.0)
        s.marker(day=5.0, before=trigger, trigger=trigger)
    out = _run(s.write(tmp_path))
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


# ---- delegated runs (a subagent's calls are written inline in the parent's log) ----------------------------------------------

def test_a_delegated_run_inside_a_parent_turn_neither_splits_it_nor_pairs_with_it(tmp_path):
    """Probe of the review: ``DelegationRecorder`` writes the subagent's llm_call and done records into the parent's file with
    node_id null. The child's final done (stop) used to end the parent's turn, and a parent call paired with a child call (a
    'decrease', since a subagent's prompt is smaller), so a monotonic provider looked broken."""
    s = Session()
    for turn in range(40):
        day = 8.0 * turn / 39
        s.records.append(_llm_call(len(s.records) + 1, day, est=NEAR))
        s.records.append(_done(len(s.records) + 1, day, "tool_use"))
        s.delegated([{"est": NEAR // 4, "used": NEAR // 4}, {"est": NEAR // 4 + 1_000, "used": NEAR // 4 + 1_000}],
                    "call_9", day=day)
        s.records.append(_llm_call(len(s.records) + 1, day, est=NEAR + 2_000))
        s.records.append(_done(len(s.records) + 1, day, "stop"))
    corpus = rule.read_corpus([s.write(tmp_path)])
    parent = [c for c in corpus.calls if c.turn[2] is None]
    assert len(parent) == 80 and {c.turn for c in parent[:2]} == {parent[0].turn}, "the parent's two calls are one turn"
    assert all(c.turn[2] == "call_9" for c in corpus.calls if c.turn[2] is not None)
    (group,) = rule.build_groups(corpus, set())[0]
    assert group.pairs == 80 and group.monotonic == 1.0 and group.anchor_allowed is True
    assert group.pairs == 40 + 40, "one parent pair and one child pair per turn, never a parent-child pair"


def test_two_delegated_runs_with_the_same_call_id_are_separate_turns(tmp_path):
    """Provider call ids restart every stream, so one id can name two delegations: each ends at its own final done."""
    s = Session()
    for turn in range(40):
        day = 8.0 * turn / 39
        s.delegated([{"est": NEAR, "used": NEAR}, {"est": NEAR + 1_000, "used": NEAR + 1_000}], "call_0", day=day)
        s.delegated([{"est": NEAR, "used": NEAR}, {"est": NEAR + 1_000, "used": NEAR + 1_000}], "call_0", day=day)
    (group,) = rule.build_groups(rule.read_corpus([s.write(tmp_path)]), set())[0]
    assert group.pairs == 80


def _child_with_a_grandchild(s: Session, day: float, *, child: str | None, grandchild: str | None) -> None:
    """The child makes a call, delegates (the grandchild makes two calls), then makes its final call: one raw id, ``call_0``, at
    both levels, the records interleaved as they are written."""
    s.delegated([{"est": NEAR, "used": NEAR}], "call_0", day=day, run_id=child, ends_turn=False)
    s.delegated([{"est": NEAR // 4, "used": NEAR // 4}, {"est": NEAR // 4 + 1_000, "used": NEAR // 4 + 1_000}],
                "call_0", day=day, run_id=grandchild)
    s.delegated([{"est": NEAR + 3_000, "used": NEAR + 3_000}], "call_0", day=day, run_id=child)


def test_a_child_and_a_grandchild_that_reuse_one_call_id_are_separate_runs_when_the_records_carry_run_ids(tmp_path):
    """The recorder stamped only the delegating call's raw id, so a nested delegation whose ids collide merged the child and the
    grandchild into one run: the grandchild's final done ended the child's turn and the child's call paired with the grandchild's
    (a 'decrease', since a subagent's prompt is smaller). Keyed by ``delegate_run_id`` each is its own run."""
    s = Session()
    for turn in range(40):
        _child_with_a_grandchild(s, 8.0 * turn / 39, child=f"R1-{turn}", grandchild=f"R2-{turn}")
    corpus = rule.read_corpus([s.write(tmp_path)])
    assert {c.turn[2] for c in corpus.calls} == {f"R{level}-{turn}" for level in (1, 2) for turn in range(40)}
    (group,) = rule.build_groups(corpus, set())[0]
    assert group.pairs == 80, "one pair per run: the child's two calls, the grandchild's two calls"
    assert group.monotonic == 1.0 and group.anchor_allowed is True


def test_records_without_a_run_id_are_keyed_by_the_call_id_as_before(tmp_path):
    """A log written before the recorder stamped run ids still reads: its runs are keyed by the delegating call's id (and a
    nested delegation that reuses it still merges, the documented limitation of such a log)."""
    s = Session()
    for turn in range(40):
        _child_with_a_grandchild(s, 8.0 * turn / 39, child=None, grandchild=None)
    corpus = rule.read_corpus([s.write(tmp_path)])
    assert {c.turn[2] for c in corpus.calls} == {"call_0"}


def test_a_delegated_or_guarded_call_is_never_the_prompt_a_compaction_marker_followed(tmp_path):
    """The call before a trigger-fired marker is the parent's last unguarded call: a small subagent prompt or a small replay
    prompt in between must not turn a compaction at 0.9 x trigger into a 'premature' one."""
    s = _singles(_wide())
    for _ in range(10):
        s.turn([{"ratio": 1.0, "est": int(0.9 * TRIGGER), "used": int(0.9 * TRIGGER)}], day=5.0)
        s.delegated([{"est": int(0.3 * TRIGGER), "used": int(0.3 * TRIGGER)}], "call_7", day=5.0)
        s.turn([{"ratio": 1.0, "est": int(0.3 * TRIGGER), "guard": "reduced"}], day=5.0)
        s.marker(day=5.0, before=TRIGGER + 1)
    assert _run(s.write(tmp_path))["facts"]["premature_compaction_share"] == 0.0


# ---- the data requirement, at its edges ----------------------------------------------------------------------------------

def test_199_calls_are_not_enough_and_200_are(tmp_path):
    assert _verdict(tmp_path / "a", [1.0] * 199) == NO_DATA
    assert _verdict(tmp_path / "b", [1.0] * 200) == DNB


def test_the_span_must_reach_the_minimum_days_exactly(tmp_path):
    assert _verdict(tmp_path / "a", [1.0] * 300, days=6.5) == NO_DATA
    assert _verdict(tmp_path / "b", [1.0] * 300, days=7.0) == DNB, "seven days to the second is enough"


def test_the_command_line_default_is_seven_days(tmp_path, capsys):
    """No --min-days: 6.5 days of data is not enough; 7.5 is."""
    _singles([1.0] * 300, days=6.5).write(tmp_path / "a")
    assert rule.main([str(tmp_path / "a"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == NO_DATA
    _singles([1.0] * 300, days=7.5).write(tmp_path / "b")
    assert rule.main([str(tmp_path / "b"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == DNB


def test_every_material_group_needs_its_own_week_not_just_the_longest(tmp_path):
    """Two material groups spanning 8 and 2 days: the shortest decides."""
    s = Session()
    for i in range(300):
        s.turn([{"ratio": 1.0, "provider": "long"}], day=8.0 * i / 299)
    for i in range(300):
        s.turn([{"ratio": 1.0, "provider": "short", "model": "m2"}], day=2.0 * i / 299)
    out = _run(s.write(tmp_path))
    assert out["verdict"] == NO_DATA and "short" in out["reason"] and "long" not in out["reason"]


# ---- rule 2 is about MATERIAL groups ------------------------------------------------------------------------------------------

def test_a_group_below_the_material_share_cannot_turn_rule_2_into_rule_3(tmp_path):
    """The material group is a steady undercount (spread small, so rule 2 applies); beside it a 200-call group at 4.8% with a
    wide spread. Rule 1 and rule 2 both look at material groups only, so the verdict is rule 2's, not rule 3's."""
    s = Session()
    for i in range(4000):
        s.turn([{"ratio": 1.30 + 0.20 * (i % 21) / 20, "provider": "big"}], day=8.0 * i / 3999)
    for i, r in enumerate(_wide(200)):
        s.turn([{"ratio": r, "provider": "small", "model": "m2"}], day=8.0 * i / 199)
    out = _run(s.write(tmp_path))
    small = [g for g in out["groups"] if g.key[0] == "small"][0]
    assert not small.material and small.enough and small.spread > 1.25
    assert out["verdict"] == ONLY_1A and out["reason"].startswith("rule 2"), out["reason"]


# ---- rule 4 with skewed deltas, turns that end in other ways, and markers at the edge ------------------------------------------

def test_rule_4_uses_the_median_delta_ratio_not_the_mean(tmp_path):
    """60% of pairs grow at 1.0 times our estimate, 40% at 3.0: the median (1.0) is in the band, the mean (1.8) is not."""
    s = Session()
    for turn in range(100):
        fast = turn % 5 < 2                                  # 40% of the turns
        step = 6_000 if fast else 2_000
        s.turn([{"est": NEAR, "used": NEAR}, {"est": NEAR + 2_000, "used": NEAR + step}], day=8.0 * turn / 99)
    (group,) = rule.build_groups(rule.read_corpus([s.write(tmp_path)]), set())[0]
    assert group.pairs == 100 and 0.99 < group.delta_ratio < 1.01 and group.anchor_allowed is True


def test_a_cancelled_or_an_error_ends_a_turn_like_a_final_done(tmp_path):
    def build(kind):
        s = Session()
        for turn in range(40):
            day = 8.0 * turn / 39
            s.records.append(_llm_call(len(s.records) + 1, day, est=NEAR))
            s.records.append(_done(len(s.records) + 1, day, "tool_use"))
            if kind:
                s.terminal(kind, day=day)               # the turn ends here, without a stop done
            s.records.append(_llm_call(len(s.records) + 1, day, est=NEAR + 2_000))
            s.records.append(_done(len(s.records) + 1, day, "stop"))
        return s
    pairs = {}
    for kind in (None, "cancelled", "error"):
        (group,) = rule.build_groups(rule.read_corpus([build(kind).write(tmp_path / str(kind))]), set())[0]
        pairs[kind] = group.pairs
    assert pairs == {None: 40, "cancelled": 0, "error": 0}


def test_a_marker_at_exactly_the_trigger_was_fired_by_the_trigger(tmp_path):
    s = _singles(_wide())
    s.turn([{"ratio": 1.0, "est": int(0.3 * TRIGGER)}], day=5.0)
    s.marker(day=5.0, before=TRIGGER)                      # tokens_before == trigger_tokens
    s.turn([{"ratio": 1.0, "est": int(0.3 * TRIGGER)}], day=5.0)
    s.marker(day=5.0, before=TRIGGER - 1)                  # one under: not the trigger's
    facts = _run(s.write(tmp_path))["facts"]
    assert facts["trigger_fired_compactions"] == 1 and facts["manual_or_forced_markers_not_counted"] == 1


# ---- the percentile --------------------------------------------------------------------------------------------------------------

def test_the_percentile_interpolates_between_ranks():
    assert rule.percentile([7.0], 0.9) == 7.0
    assert rule.percentile([10.0, 20.0], 0.5) == 15.0
    assert rule.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 0.5) == 3.0, "unsorted input"
    assert rule.percentile(list(map(float, range(101))), 0.10) == 10.0
    assert rule.percentile(list(map(float, range(101))), 0.90) == 90.0
    assert rule.percentile([0.0, 10.0], 0.25) == 2.5
    assert rule.percentile([1.0, 2.0, 3.0], 0.0) == 1.0 and rule.percentile([1.0, 2.0, 3.0], 1.0) == 3.0


# ---- rule 3's consequence, at its edges ----------------------------------------------------------------------------------------

def _fired_markers(s: Session, *, premature: int, calm: int, premature_at=0.3, calm_at=0.9) -> Session:
    """Trigger-fired compactions, each after one call whose usage was ``*_at`` x trigger. The call's ESTIMATE is far under
    the near-window gate, so it does not join the group whose ratios the verdict reads (only its usage is the occupancy)."""
    for share, count in ((premature_at, premature), (calm_at, calm)):
        for _ in range(count):
            s.turn([{"est": int(0.2 * TRIGGER), "used": int(share * TRIGGER)}], day=5.0)
            s.marker(day=5.0, before=TRIGGER + 1)
    return s


def test_exactly_a_tenth_of_the_compactions_premature_is_a_visible_consequence_and_a_twentieth_is_not(tmp_path):
    """20 trigger-fired compactions: 2 premature is exactly 10% (build 1b: kills ``>`` and a share of 0.5), 1 is 5% (1a: kills a
    share of 0.01)."""
    assert 2 / 20 == rule.PREMATURE_SHARE
    ten = _run(_fired_markers(_singles(_wide()), premature=2, calm=18).write(tmp_path / "a"))
    assert ten["facts"]["premature_compaction_share"] == 0.1 and ten["verdict"] == BUILD_1B, ten["reason"]
    five = _run(_fired_markers(_singles(_wide()), premature=1, calm=19).write(tmp_path / "b"))
    assert five["facts"]["premature_compaction_share"] == 0.05
    assert five["verdict"] == ONLY_1A and "no visible consequence" in five["reason"]


def test_manual_and_forced_compactions_are_not_in_the_denominator_of_the_premature_share(tmp_path):
    """The same 2 premature of 20 trigger-fired compactions, plus 8 manual or overflow-forced ones (tokens_before under the
    trigger) after calls at HIGH occupancy: the share stays exactly 0.1 (build 1b). Counting the forced ones in the denominator
    would make it 2/28 and flip the verdict to 1a; the earlier test put its forced markers at low occupancy, which pins
    only the numerator."""
    s = _fired_markers(_singles(_wide()), premature=2, calm=18)
    for _ in range(8):
        s.turn([{"est": int(0.2 * TRIGGER), "used": int(0.9 * TRIGGER)}], day=5.0)
        s.marker(day=5.0, before=int(0.5 * TRIGGER))
    out = _run(s.write(tmp_path))
    assert out["facts"]["trigger_fired_compactions"] == 20 and out["facts"]["manual_or_forced_markers_not_counted"] == 8
    assert out["facts"]["premature_compaction_share"] == 0.1
    assert out["verdict"] == BUILD_1B, out["reason"]


def test_a_compaction_after_a_call_at_six_tenths_of_the_trigger_is_premature_and_one_at_eight_tenths_is_not(tmp_path):
    """Occupancy is compared with 0.75 x trigger: 0.6 is under it (a share of 0.5 would call it fine), 0.8 is over it."""
    low = _run(_fired_markers(_singles(_wide()), premature=10, calm=0, premature_at=0.6).write(tmp_path / "a"))
    assert low["facts"]["premature_compaction_share"] == 1.0 and low["verdict"] == BUILD_1B
    high = _run(_fired_markers(_singles(_wide()), premature=10, calm=0, premature_at=0.8).write(tmp_path / "b"))
    assert high["facts"]["premature_compaction_share"] == 0.0 and high["verdict"] == ONLY_1A


def test_exactly_one_replay_per_200_near_window_turns_is_a_visible_consequence_and_just_under_is_not(tmp_path):
    """2 replay turns among 400 near-window turns (398 ordinary + the 2) is exactly 1.0 per 200: build 1b (kills ``>`` and a
    threshold of 1.9); among 401 it is 0.9975: 1a (kills a threshold of 0.5)."""
    exact = _run(_with_replays(_singles(_wide(398)), 2).write(tmp_path / "a"))
    assert exact["facts"]["replays_per_200_near_window_turns"] == 1.0 and exact["verdict"] == BUILD_1B, exact["reason"]
    under = _run(_with_replays(_singles(_wide(399)), 2).write(tmp_path / "b"))
    assert (under["facts"]["near_window_turns"], under["facts"]["replay_turns"]) == (401, 2)
    assert under["facts"]["replays_per_200_near_window_turns"] == round(200 * 2 / 401, 4) < 1.0
    assert under["verdict"] == ONLY_1A and "no visible consequence" in under["reason"]


def test_turns_that_are_not_near_window_do_not_dilute_the_replay_rate(tmp_path):
    """The denominator is near-window turns (and the replay turns themselves): a thousand small turns beside them change
    nothing, so the same 2 replays per 400 stay a visible consequence."""
    s = _with_replays(_singles(_wide(398)), 2)
    for i in range(1000):
        s.turn([{"ratio": 1.0, "est": int(0.2 * TRIGGER)}], day=8.0 * i / 999)
    out = _run(s.write(tmp_path))
    assert out["facts"]["near_window_turns"] == 400 and out["facts"]["replays_per_200_near_window_turns"] == 1.0
    assert out["verdict"] == BUILD_1B


def _uniform(lo: float, hi: float, n: int = 300) -> list[float]:
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def test_a_kappa_error_just_under_and_just_over_fifteen_percent_decides_rule_3(tmp_path):
    """Uniform ratios in [0.85, 1.15] leave a p90 error of 0.135 after correction by the median, [0.82, 1.18] leave 0.162 (both
    with a spread over 1.25, so rule 2 does not answer first); replays are present in both."""
    near = _run(_with_replays(_singles(_uniform(0.85, 1.15)), 3).write(tmp_path / "a"))
    assert near["groups"][0].kappa_error < 0.15 and near["verdict"] == ONLY_1A and "no material group varies" in near["reason"]
    over = _run(_with_replays(_singles(_uniform(0.82, 1.18)), 3).write(tmp_path / "b"))
    assert over["groups"][0].kappa_error > 0.15 and over["verdict"] == BUILD_1B, over["reason"]


# ---- the other exact edges ---------------------------------------------------------------------------------------------------------

def test_a_group_at_exactly_five_percent_of_the_near_window_calls_is_material(tmp_path):
    assert 210 / 4200 == rule.MATERIAL_SHARE
    def build(small: int, root) -> dict:
        s = Session()
        for i in range(4200 - small):
            s.turn([{"ratio": 1.0, "provider": "big"}], day=8.0 * i / 3989)
        for i in range(small):
            s.turn([{"ratio": 1.0, "provider": "small", "model": "m2"}], day=8.0 * i / (small - 1))
        return {g.key[0]: g for g in _run(s.write(root))["groups"]}
    at = build(210, tmp_path / "a")
    assert at["small"].share == 0.05 and at["small"].material
    below = build(209, tmp_path / "b")
    assert below["small"].share < 0.05 and not below["small"].material


def test_exactly_ninety_nine_percent_of_pairs_non_decreasing_keeps_the_anchor_and_98_5_does_not(tmp_path):
    """200 pairs: 2 going backwards is exactly 0.99 (allowed: kills ``>``), 3 is 0.985 (demoted)."""
    assert 198 / 200 == rule.MONOTONIC_FLOOR
    def build(backwards: int, root) -> rule.Group:
        s = Session()
        for turn in range(100):
            back = turn < backwards
            s.turn([
                {"est": NEAR, "used": NEAR}, {"est": NEAR + 2_000, "used": NEAR + 2_000},
                {"est": NEAR + 4_000, "used": NEAR + 1_500 if back else NEAR + 4_000},
            ], day=8.0 * turn / 99)
        (group,) = rule.build_groups(rule.read_corpus([s.write(root)]), set())[0]
        return group
    two, three = build(2, tmp_path / "a"), build(3, tmp_path / "b")
    assert (two.pairs, two.monotonic, two.anchor_allowed) == (200, 0.99, True)
    assert (three.pairs, three.monotonic, three.anchor_allowed) == (200, 0.985, False)


# ---- the recorder itself ---------------------------------------------------------------------------------------------------------

class _Writer:
    def __init__(self) -> None:
        self.records: list[SessionMessageRecord] = []

    async def append(self, rec: SessionMessageRecord) -> int:
        self.records.append(rec)
        return len(self.records)


class _Bus:
    async def publish(self, *args, **kwargs) -> None:
        return None


async def test_a_delegated_run_written_by_the_real_recorder_is_a_turn_of_its_own(tmp_path):
    """The same shape as the hand-stamped fixture, but the child's records come out of ``DelegationRecorder.on_event``, so a
    change to what the recorder stamps (the keys, which records it stamps) turns this red instead of leaving the fixture a
    comfortable fiction."""
    from primer.session.delegation import DelegationRecorder

    s = Session()
    for turn in range(40):
        day = 8.0 * turn / 39
        s.records.append(_llm_call(len(s.records) + 1, day, est=NEAR))
        s.records.append(_done(len(s.records) + 1, day, "tool_use"))
        writer = _Writer()
        recorder = DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="s1")
        child = [(NEAR // 4, NEAR // 4), (NEAR // 4 + 1_000, NEAR // 4 + 1_000)]
        for k, (est, used) in enumerate(child):
            call = ExtendedEvent(extended=_LlmCall(
                profile_id="prof", provider_id="prov-a", model="m1", input_tokens=used, output_tokens=5,
                estimated_input_tokens=est, context_length=CTX, duration_ms=10, status="ok",
            ))
            await recorder.on_event(call, delegate_tool_call_id="call_9")
            await recorder.on_event(
                Done(stop_reason="stop" if k == len(child) - 1 else "tool_use", raw_reason="x"), delegate_tool_call_id="call_9",
            )
        assert [r.kind.value for r in writer.records] == ["llm_call", "done"] * 2
        assert all(r.payload["delegated"] is True and r.payload["delegate_tool_call_id"] == "call_9" for r in writer.records)
        for rec in writer.records:
            s.records.append(rec.model_copy(update={"seq": len(s.records) + 1, "created_at": _stamp(day)}))
        s.records.append(_llm_call(len(s.records) + 1, day, est=NEAR + 2_000))
        s.records.append(_done(len(s.records) + 1, day, "stop"))
    (group,) = rule.build_groups(rule.read_corpus([s.write(tmp_path)]), set())[0]
    assert group.pairs == 80 and group.monotonic == 1.0 and group.anchor_allowed is True


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
