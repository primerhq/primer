"""Apply the Phase 0 decision rule of the prompt-size accounting work to ``llm_call`` records.

Phase 0 records, per model call, the provider's reported ``input_tokens`` beside our estimate of the prompt that was sent
(``estimated_input_tokens``). Whether a better signal than the character heuristic is worth building depends on how that ratio
behaves on real traffic. This script reads the ``llm_call`` records of session ``messages.jsonl`` files (a dogfood
instance's workspace directory, a k3s dump, any directory tree that holds them) and prints the verdict of the rule, with
the figures it rests on, so the decision is mechanical.

    uv run python scripts/analyse_estimate_ratio.py DIR [DIR ...] [--exclude-provider ID ...] [--min-days 7] [--json]

The rule (native-token-counting design v3.4, section 8):

* Data: at least ``--min-days`` days of records, grouped by ``(provider_id, model)``, at least 200 calls per group with
  ``estimated_input_tokens >= 0.33 x trigger`` (``trigger = 0.90 x (context_length - min(8192, max(1, context_length // 2)))``,
  from the record's own ``context_length``). Excluded: aggregated profiles (``provider_id`` null), providers named with
  ``--exclude-provider`` (Ollama: ``prompt_eval_count`` leaves out the KV-cached prefix), and calls made under a prompt guard
  (``guard`` present: the replay after an overflow). ``r = input_tokens / estimated_input_tokens``.
* (1) Every material group (5% or more of the near-window calls) has ``p10(r) >= 0.85`` and ``p90(r) <= 1.10``: do NOT build
  Phase 1.
* (2) Else, spread within every group small (``p90 / p10 <= 1.25``): build only Phase 1a (rung C x EMA kappa).
* (3) Else build Phase 1b (the anchor) only if, after kappa correction, ``p90 |r / kappa - 1| > 0.15`` for a material group AND
  there is a visible consequence: at least 1 overflow replay per 200 near-window turns, or at least 10% of compactions fired
  with real occupancy (the usage of the call before the marker) below ``0.75 x trigger``.
* (4) Per group, an anchor or kappa is allowed only if, within a turn, consecutive pure-append calls show non-decreasing
  ``input_tokens`` in at least 99% of the pairs and a median ``(delta usage / delta estimate)`` in ``[0.7, 1.4]``.

Records written before Phase 0b carry no ``context_length``; they are counted and skipped.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from collections.abc import Iterable, Iterator

GATE = 0.33
MIN_CALLS = 200
MATERIAL_SHARE = 0.05
P10_FLOOR, P90_CEIL = 0.85, 1.10
SPREAD_CEIL = 1.25
KAPPA_ERROR = 0.15
REPLAYS_PER_200 = 1.0
PREMATURE_SHARE = 0.10
PREMATURE_OCCUPANCY = 0.75
MONOTONIC_FLOOR = 0.99
DELTA_RATIO_BAND = (0.7, 1.4)
MIN_PAIRS = 30
TERMINAL_KINDS = ("done", "cancelled", "error")


def trigger_tokens(context_length: int) -> int:
    """The compaction trigger of ``CompactionStrategy`` at its defaults (``_effective_budget`` and ``DEFAULT_TRIGGER_RATIO``)."""
    reserved = min(8192, max(1, context_length // 2))
    return int(0.90 * max(0, context_length - reserved))


@dataclass
class Call:
    provider_id: str | None
    model: str | None
    input_tokens: int
    estimated: int
    context_length: int | None
    guard: str | None
    turn: tuple[str, int]
    when: datetime | None

    @property
    def ratio(self) -> float:
        return self.input_tokens / self.estimated

    @property
    def near_window(self) -> bool:
        return self.context_length is not None and self.estimated >= GATE * trigger_tokens(self.context_length)


@dataclass
class Corpus:
    calls: list[Call] = field(default_factory=list)
    turns: set[tuple[str, int]] = field(default_factory=set)
    markers: list[float] = field(default_factory=list)     # occupancy / trigger of the call before each marker
    without_context_length: int = 0
    files: int = 0


def _when(raw: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def find_files(roots: Iterable[Path]) -> Iterator[Path]:
    for root in roots:
        if root.is_file():
            yield root
        else:
            yield from sorted(root.rglob("messages.jsonl"))


def read_corpus(roots: Iterable[Path]) -> Corpus:
    corpus = Corpus()
    for path in find_files(roots):
        corpus.files += 1
        turn = 0
        last_call: Call | None = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            kind = rec.get("kind") if isinstance(rec, dict) else None
            payload = rec.get("payload") or {} if isinstance(rec, dict) else {}
            if kind == "llm_call":
                used, est = payload.get("input_tokens"), payload.get("estimated_input_tokens")
                if not (isinstance(used, int) and used > 0 and isinstance(est, int) and est > 0):
                    continue            # a call with no usage records no estimate either
                ctx = payload.get("context_length")
                if not isinstance(ctx, int):
                    corpus.without_context_length += 1
                    ctx = None
                call = Call(
                    payload.get("provider_id"), payload.get("model"), used, est, ctx, payload.get("guard"),
                    (str(path), turn), _when(rec.get("created_at")),
                )
                corpus.calls.append(call)
                corpus.turns.add(call.turn)
                last_call = call
            elif kind == "compaction_marker":
                if last_call is not None and last_call.context_length is not None:
                    corpus.markers.append(last_call.input_tokens / trigger_tokens(last_call.context_length))
            elif kind in TERMINAL_KINDS:
                turn += 1
    return corpus


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


@dataclass
class Group:
    key: tuple[str, str | None]
    ratios: list[float]
    share: float = 0.0
    pairs: int = 0
    monotonic: float | None = None
    delta_ratio: float | None = None

    @property
    def p10(self) -> float:
        return percentile(self.ratios, 0.10)

    @property
    def p50(self) -> float:
        return percentile(self.ratios, 0.50)

    @property
    def p90(self) -> float:
        return percentile(self.ratios, 0.90)

    @property
    def material(self) -> bool:
        return self.share >= MATERIAL_SHARE

    @property
    def enough(self) -> bool:
        return len(self.ratios) >= MIN_CALLS

    @property
    def spread(self) -> float:
        return self.p90 / self.p10 if self.p10 > 0 else float("inf")

    @property
    def kappa_error(self) -> float:
        kappa = self.p50
        return percentile([abs(r / kappa - 1) for r in self.ratios], 0.90) if kappa > 0 else float("inf")

    @property
    def anchor_allowed(self) -> bool | None:
        """Rule 4: ``None`` when there are too few pure-append pairs to say."""
        if self.pairs < MIN_PAIRS or self.monotonic is None or self.delta_ratio is None:
            return None
        return self.monotonic >= MONOTONIC_FLOOR and DELTA_RATIO_BAND[0] <= self.delta_ratio <= DELTA_RATIO_BAND[1]


def build_groups(corpus: Corpus, exclude_providers: set[str]) -> tuple[list[Group], dict[str, int]]:
    skipped = {"aggregated": 0, "excluded_provider": 0, "guarded": 0, "no_context_length": corpus.without_context_length}
    near: dict[tuple[str, str | None], list[Call]] = defaultdict(list)
    for call in corpus.calls:
        if call.provider_id is None:
            skipped["aggregated"] += 1
        elif call.provider_id in exclude_providers:
            skipped["excluded_provider"] += 1
        elif call.guard is not None:
            skipped["guarded"] += 1
        elif call.near_window:
            near[(call.provider_id, call.model)].append(call)
    total = sum(len(v) for v in near.values())
    groups = []
    for key, calls in sorted(near.items()):
        group = Group(key, [c.ratio for c in calls], share=len(calls) / total if total else 0.0)
        _pure_append_pairs(group, [c for c in corpus.calls if (c.provider_id, c.model) == key and c.guard is None])
        groups.append(group)
    return groups, skipped


def _pure_append_pairs(group: Group, calls: list[Call]) -> None:
    """Rule 4's evidence: consecutive calls of one turn (no guard), each pair's usage and estimate deltas."""
    by_turn: dict[tuple[str, int], list[Call]] = defaultdict(list)
    for call in calls:
        by_turn[call.turn].append(call)
    pairs = monotonic = 0
    ratios = []
    for turn_calls in by_turn.values():
        for a, b in zip(turn_calls, turn_calls[1:]):
            pairs += 1
            monotonic += b.input_tokens >= a.input_tokens
            d_est = b.estimated - a.estimated
            if d_est > 0:
                ratios.append((b.input_tokens - a.input_tokens) / d_est)
    group.pairs = pairs
    group.monotonic = monotonic / pairs if pairs else None
    group.delta_ratio = statistics.median(ratios) if ratios else None


def decide(corpus: Corpus, groups: list[Group], min_days: float) -> dict:
    out: dict = {"facts": {}, "verdict": None, "reason": None}
    stamps = [c.when for c in corpus.calls if c.when is not None]
    days = (max(stamps) - min(stamps)).total_seconds() / 86400 if len(stamps) > 1 else 0.0
    replay_turns = {c.turn for c in corpus.calls if c.guard is not None}       # a guard is installed for a replay only
    near_turns = {c.turn for c in corpus.calls if c.near_window} | replay_turns
    replays_per_200 = 200 * len(replay_turns) / len(near_turns) if near_turns else 0.0
    premature = sum(1 for occ in corpus.markers if occ < PREMATURE_OCCUPANCY)
    premature_share = premature / len(corpus.markers) if corpus.markers else 0.0
    out["facts"] = {
        "days_of_data": round(days, 2), "near_window_turns": len(near_turns),
        "replay_turns": len(replay_turns), "replays_per_200_near_window_turns": round(replays_per_200, 2),
        "compactions": len(corpus.markers), "premature_compaction_share": round(premature_share, 3),
    }
    material = [g for g in groups if g.material]
    if days < min_days:
        out["verdict"], out["reason"] = "INSUFFICIENT DATA", f"{days:.1f} days of records, the rule needs {min_days:g}"
    elif not material:
        out["verdict"], out["reason"] = "INSUFFICIENT DATA", "no group carries 5% of the near-window calls"
    elif any(not g.enough for g in material):
        small = ", ".join(f"{g.key}: {len(g.ratios)}" for g in material if not g.enough)
        out["verdict"], out["reason"] = "INSUFFICIENT DATA", f"a material group has fewer than {MIN_CALLS} near-window calls ({small})"
    elif all(g.p10 >= P10_FLOOR and g.p90 <= P90_CEIL for g in material):
        out["verdict"], out["reason"] = "DO NOT BUILD PHASE 1", "rule 1: every material group is within [0.85, 1.10] at p10 and p90"
    elif all(g.spread <= SPREAD_CEIL for g in groups if g.enough):
        out["verdict"], out["reason"] = "BUILD PHASE 1a ONLY", "rule 2: the error is not small but its spread within each group is (p90/p10 <= 1.25)"
    else:
        varies = [g for g in material if g.kappa_error > KAPPA_ERROR]
        consequence = replays_per_200 >= REPLAYS_PER_200 or premature_share >= PREMATURE_SHARE
        if varies and consequence:
            out["verdict"], out["reason"] = "BUILD PHASE 1b", (
                "rule 3: the error varies within a material group after kappa correction "
                f"({', '.join(str(g.key) for g in varies)}) and it has a visible consequence"
            )
        else:
            out["verdict"], out["reason"] = "BUILD PHASE 1a ONLY", (
                "rule 3 is not met: " + (
                    "no material group varies more than 15% after kappa correction" if not varies
                    else "the error varies but no visible consequence (replays, premature compactions) is recorded"
                )
            )
    return out


def render(corpus: Corpus, groups: list[Group], skipped: dict[str, int], decision: dict) -> str:
    lines = [f"files: {corpus.files}, llm_call records with usage and an estimate: {len(corpus.calls)}"]
    lines.append("skipped: " + ", ".join(f"{k}={v}" for k, v in skipped.items()))
    lines.append("")
    lines.append(f"{'provider / model':<48} {'n':>6} {'share':>6} {'p10':>6} {'p50':>6} {'p90':>6} {'p90/p10':>8} {'kappa err':>9}  rule 4")
    for g in groups:
        allowed = g.anchor_allowed
        rule4 = "too few pairs" if allowed is None else ("ok" if allowed else "DEMOTE to estimate")
        if g.pairs and g.monotonic is not None and g.delta_ratio is not None:
            rule4 += f" (pairs {g.pairs}, non-decreasing {g.monotonic:.3f}, median delta ratio {g.delta_ratio:.2f})"
        name = f"{g.key[0]} / {g.key[1]}"
        flag = "" if g.material else "  (not material)"
        lines.append(
            f"{name:<48} {len(g.ratios):>6} {g.share:>6.1%} {g.p10:>6.2f} {g.p50:>6.2f} {g.p90:>6.2f} {g.spread:>8.2f} "
            f"{g.kappa_error:>9.2f}  {rule4}{flag}"
        )
    lines.append("")
    for k, v in decision["facts"].items():
        lines.append(f"{k}: {v}")
    lines.append("")
    lines.append(f"VERDICT: {decision['verdict']} ({decision['reason']})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("roots", nargs="+", type=Path, help="directories (searched for messages.jsonl) or files")
    parser.add_argument("--exclude-provider", action="append", default=[], help="provider id to exclude (Ollama)")
    parser.add_argument("--min-days", type=float, default=7.0)
    parser.add_argument("--json", action="store_true", help="print the figures and the verdict as JSON")
    args = parser.parse_args(argv)
    corpus = read_corpus(args.roots)
    groups, skipped = build_groups(corpus, set(args.exclude_provider))
    decision = decide(corpus, groups, args.min_days)
    if args.json:
        print(json.dumps({
            "skipped": skipped, **decision,
            "groups": [
                {
                    "provider_id": g.key[0], "model": g.key[1], "n": len(g.ratios), "share": g.share, "p10": g.p10,
                    "p50": g.p50, "p90": g.p90, "kappa_error": g.kappa_error, "pairs": g.pairs,
                    "non_decreasing": g.monotonic, "delta_ratio": g.delta_ratio, "anchor_allowed": g.anchor_allowed,
                } for g in groups
            ],
        }, indent=2))
    else:
        print(render(corpus, groups, skipped, decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
