"""Apply the Phase 0 decision rule of the prompt-size accounting work to ``llm_call`` records.

Phase 0 records, per model call, the provider's reported ``input_tokens`` beside our estimate of the prompt that was sent
(``estimated_input_tokens``). Whether a better signal than the character heuristic is worth building depends on how that ratio
behaves on real traffic. This script reads the ``llm_call`` records of session ``messages.jsonl`` files and prints the
verdict of the rule with the figures it rests on, so the decision is mechanical.

    uv run python scripts/analyse_estimate_ratio.py DIR [DIR ...] [--exclude-provider ID ...] [--min-days 7] [--json]

Where the data is. A session's record log is ``<workspace root>/<state_path>/sessions/<session id>/messages.jsonl`` and
``state_path`` is ``.state`` unless the template says otherwise. For the dogfood instance that is
``~/.primer/workspaces/<workspace>/.state/sessions/*/messages.jsonl``: pass ``~/.primer/workspaces``. A docker or k8s
workspace keeps its state INSIDE the sandbox, so copy it out first, for example
``kubectl cp <namespace>/<pod>:<workspace root>/.state/sessions ./k3s-dump/<workspace>`` for each workspace pod, then pass
``./k3s-dump``. Directories are searched recursively for ``messages.jsonl``; a root named twice (or a root inside another)
is read once. The script prints the distinct provider ids it saw: name the Ollama ones with ``--exclude-provider``, because
the record carries the provider's id and not its kind and Ollama's ``prompt_eval_count`` leaves out the KV-cached prefix.

The rule (native-token-counting design v3.4, section 8):

* Data: at least ``--min-days`` days of the calls the verdict rests on (the near-window calls of each material group, not of
  every record: older records without ``context_length``, aggregated profiles, excluded providers and guarded calls do not
  count towards the span), grouped by ``(provider_id, model)``, at least 200 calls per material group with
  ``estimated_input_tokens >= 0.33 x trigger`` (``trigger = 0.90 x (context_length - min(8192, max(1, context_length // 2)))``,
  from the record's own ``context_length``). Excluded: aggregated profiles (``provider_id`` null), providers named with
  ``--exclude-provider``, and calls made under a prompt guard (``guard`` present: the replay after an overflow).
  ``r = input_tokens / estimated_input_tokens``.
* (1) Every material group (5% or more of the near-window calls) has ``p10(r) >= 0.85`` and ``p90(r) <= 1.10``: do NOT build
  Phase 1.
* (2) Else, spread within every MATERIAL group small (``p90 / p10 <= 1.25``; a group below the material share cannot justify
  building, as in rule 1): build only Phase 1a (rung C x EMA kappa).
* (3) Else build Phase 1b (the anchor) only if, after kappa correction, ``p90 |r / kappa - 1| > 0.15`` for a material group AND
  there is a visible consequence: at least 1 overflow replay per 200 near-window turns, or at least 10% of the compactions
  the TRIGGER fired (``tokens_before >= trigger_tokens`` in the marker; manual and overflow-forced compactions are not
  counted) fired with real occupancy (the usage of the call before the marker) below ``0.75 x trigger``. **Approximation:**
  kappa here is the group's MEDIAN ratio, a static figure, where Phase 1a's kappa is a per-session EMA; a static median
  overstates the error left after correction, so this leans towards 1b and a verdict of 1b is the one to double check.
* (4) Per group, an anchor or kappa is allowed only if, within a turn (and a node and a stretch with no compaction
  marker), consecutive pure-append calls show non-decreasing ``input_tokens`` in at least 99% of the pairs and a median
  ``(delta usage / delta estimate)`` in ``[0.7, 1.4]``.

A TURN ends at a ``done`` whose ``stop_reason`` is not ``tool_use`` (the loop writes a ``done`` after EVERY model call, tool
rounds included), a ``cancelled`` or an ``error``, counted per ``(file, node_id, delegate_tool_call_id)``: graph nodes do not
interleave, and a delegated (subagent) run, which ``DelegationRecorder`` writes INLINE into the parent's log with
``payload.delegated`` and the delegating call's id, is a turn of its own, so its final ``done`` does not end the parent's turn
and a parent call is never paired with a child call. Turn segmentation is only as good as the adapters' stop reasons: the Chat Completions adapters now report a tool round
that the server finished with ``stop`` as ``tool_use`` (they used to record ``stop`` and end the turn early for that provider),
so logs written BEFORE that fix still split such turns.
**Limitation:** the recorder stamps no depth or run id, only the delegating call's RAW provider id, and providers that
synthesise ids (Gemini and Ollama: ``call_{idx}``) reuse them: a delegation nested inside a delegation, with the same raw id
at both levels, merges the child and the grandchild into one run (sequential reuse of an id is handled). Ticketed.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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
    turn: tuple[str, str | None, str | None, int]   # (file, node_id, delegating call id, turn index within that run)
    segment: int                               # compaction markers seen so far in the file: a prompt does not append across one
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
    premature: list[bool] = field(default_factory=list)    # per trigger-fired marker: was the prompt before it under 0.75 x trigger
    manual_or_forced_markers: int = 0
    without_context_length: int = 0
    files: int = 0


def _when(raw: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def find_files(roots: Iterable[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for root in roots:
        candidates = [root] if root.is_file() else sorted(root.rglob("messages.jsonl"))
        for path in candidates:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path


def _is_terminal(kind: str | None, payload: dict) -> bool:
    if kind in ("cancelled", "error"):
        return True
    return kind == "done" and payload.get("stop_reason") != "tool_use"


def read_corpus(roots: Iterable[Path]) -> Corpus:
    corpus = Corpus()
    for path in find_files(roots):
        corpus.files += 1
        turns: dict[tuple[str | None, str | None], int] = defaultdict(int)
        segment = 0
        last_call: Call | None = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("kind")
            payload = rec.get("payload") or {}
            node = rec.get("node_id")
            # A delegated run is its own run: its dones end ITS turns, and its calls pair only with each other.
            delegated = bool(payload.get("delegated"))
            run = (node, (payload.get("delegate_tool_call_id") or "") if delegated else None)
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
                    (str(path), run[0], run[1], turns[run]), segment, _when(rec.get("created_at")),
                )
                corpus.calls.append(call)
                if call.guard is None and ctx is not None and not delegated:
                    last_call = call          # the prompt a compaction marker followed: the parent's, not a subagent's, not a replay's
            elif kind == "compaction_marker":
                before, trig = payload.get("tokens_before"), payload.get("trigger_tokens")
                fired = isinstance(before, int) and isinstance(trig, int) and before >= trig
                if not fired:
                    corpus.manual_or_forced_markers += 1     # a manual or an overflow-forced compaction: not the trigger's
                elif last_call is not None and last_call.context_length is not None:
                    corpus.premature.append(
                        last_call.input_tokens < PREMATURE_OCCUPANCY * trigger_tokens(last_call.context_length)
                    )
                segment += 1
            elif _is_terminal(kind, payload):
                turns[run] += 1
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
    calls: list[Call]                          # the near-window calls the verdict rests on
    share: float = 0.0
    pairs: int = 0
    monotonic: float | None = None
    delta_ratio: float | None = None

    @property
    def ratios(self) -> list[float]:
        return [c.ratio for c in self.calls]

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
        return len(self.calls) >= MIN_CALLS

    @property
    def span_days(self) -> float:
        stamps = [c.when for c in self.calls if c.when is not None]
        return (max(stamps) - min(stamps)).total_seconds() / 86400 if len(stamps) > 1 else 0.0

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
        group = Group(key, calls, share=len(calls) / total if total else 0.0)
        _pure_append_pairs(group, [c for c in corpus.calls if (c.provider_id, c.model) == key and c.guard is None])
        groups.append(group)
    return groups, skipped


def _pure_append_pairs(group: Group, calls: list[Call]) -> None:
    """Rule 4's evidence: consecutive calls of one turn, node and compaction-free stretch (no guard), each pair's usage and
    estimate deltas."""
    by_run: dict[tuple, list[Call]] = defaultdict(list)
    for call in calls:
        by_run[(call.turn, call.segment)].append(call)
    pairs = monotonic = 0
    ratios = []
    for run in by_run.values():
        for a, b in zip(run, run[1:]):
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
    material = [g for g in groups if g.material]
    # The span is measured over the calls the verdict rests on: each material group's own near-window calls.
    days = min((g.span_days for g in material), default=0.0)
    replay_turns = {c.turn for c in corpus.calls if c.guard is not None}       # a guard is installed for a replay only
    near_turns = {c.turn for c in corpus.calls if c.near_window} | replay_turns
    replays_per_200 = 200 * len(replay_turns) / len(near_turns) if near_turns else 0.0
    premature = sum(corpus.premature)
    premature_share = premature / len(corpus.premature) if corpus.premature else 0.0
    out["facts"] = {
        "days_of_usable_data (shortest material group)": round(days, 2), "near_window_turns": len(near_turns),
        "replay_turns": len(replay_turns), "replays_per_200_near_window_turns": round(replays_per_200, 4),
        "trigger_fired_compactions": len(corpus.premature), "premature_compaction_share": round(premature_share, 3),
        "manual_or_forced_markers_not_counted": corpus.manual_or_forced_markers,
    }
    if not material:
        out["verdict"], out["reason"] = "INSUFFICIENT DATA", "no group carries 5% of the near-window calls"
    elif any(not g.enough for g in material):
        small = ", ".join(f"{g.key}: {len(g.calls)}" for g in material if not g.enough)
        out["verdict"], out["reason"] = "INSUFFICIENT DATA", f"a material group has fewer than {MIN_CALLS} near-window calls ({small})"
    elif days < min_days:
        short = ", ".join(f"{g.key}: {g.span_days:.1f} days" for g in material if g.span_days < min_days)
        out["verdict"], out["reason"] = "INSUFFICIENT DATA", f"the rule needs {min_days:g} days of usable calls per material group ({short})"
    elif all(g.p10 >= P10_FLOOR and g.p90 <= P90_CEIL for g in material):
        out["verdict"], out["reason"] = "DO NOT BUILD PHASE 1", "rule 1: every material group is within [0.85, 1.10] at p10 and p90"
    elif all(g.spread <= SPREAD_CEIL for g in material):
        out["verdict"], out["reason"] = "BUILD PHASE 1a ONLY", "rule 2: the error is not small but its spread within each material group is (p90/p10 <= 1.25)"
    else:
        varies = [g for g in material if g.kappa_error > KAPPA_ERROR]
        consequence = replays_per_200 >= REPLAYS_PER_200 or premature_share >= PREMATURE_SHARE
        if varies and consequence:
            out["verdict"], out["reason"] = "BUILD PHASE 1b", (
                "rule 3: the error varies within a material group after kappa correction "
                f"({', '.join(str(g.key) for g in varies)}; kappa is the group median, an overstatement of what an EMA "
                "leaves) and it has a visible consequence"
            )
        else:
            out["verdict"], out["reason"] = "BUILD PHASE 1a ONLY", (
                "rule 3 is not met: " + (
                    "no material group varies more than 15% after kappa correction" if not varies
                    else "the error varies but no visible consequence (replays, premature compactions) is recorded"
                )
            )
    return out


def render(corpus: Corpus, groups: list[Group], skipped: dict[str, int], decision: dict, providers: list[str]) -> str:
    lines = [f"files: {corpus.files}, llm_call records with usage and an estimate: {len(corpus.calls)}"]
    lines.append("skipped: " + ", ".join(f"{k}={v}" for k, v in skipped.items()))
    lines.append("provider ids seen: " + (", ".join(providers) or "none"))
    lines.append("")
    lines.append(
        f"{'provider / model':<48} {'n':>6} {'share':>6} {'days':>5} {'p10':>6} {'p50':>6} {'p90':>6} {'p90/p10':>8} "
        f"{'kappa err':>9}  rule 4"
    )
    for g in groups:
        allowed = g.anchor_allowed
        rule4 = "too few pairs" if allowed is None else ("ok" if allowed else "DEMOTE to estimate")
        if g.pairs and g.monotonic is not None and g.delta_ratio is not None:
            rule4 += f" (pairs {g.pairs}, non-decreasing {g.monotonic:.3f}, median delta ratio {g.delta_ratio:.2f})"
        name = f"{g.key[0]} / {g.key[1]}"
        flag = "" if g.material else "  (not material)"
        lines.append(
            f"{name:<48} {len(g.calls):>6} {g.share:>6.1%} {g.span_days:>5.1f} {g.p10:>6.2f} {g.p50:>6.2f} {g.p90:>6.2f} "
            f"{g.spread:>8.2f} {g.kappa_error:>9.2f}  {rule4}{flag}"
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
    providers = sorted({c.provider_id for c in corpus.calls if c.provider_id is not None})
    if providers and not args.exclude_provider:
        print(
            "warning: no --exclude-provider given; if any of the provider ids above is an Ollama server its "
            "prompt_eval_count leaves out the KV-cached prefix and it must be excluded", file=sys.stderr,
        )
    if args.json:
        print(json.dumps({
            "skipped": skipped, "providers": providers, **decision,
            "groups": [
                {
                    "provider_id": g.key[0], "model": g.key[1], "n": len(g.calls), "share": g.share, "span_days": g.span_days,
                    "p10": g.p10, "p50": g.p50, "p90": g.p90, "kappa_error": g.kappa_error, "pairs": g.pairs,
                    "non_decreasing": g.monotonic, "delta_ratio": g.delta_ratio, "anchor_allowed": g.anchor_allowed,
                } for g in groups
            ],
        }, indent=2))
    else:
        print(render(corpus, groups, skipped, decision, providers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
