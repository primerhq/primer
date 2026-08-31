"""Auto-compaction trigger parity between the chat and session paths.

S1 P2 Task 15, an AUDIT that gates P7: chat's _maybe_compact_history
cannot be deleted until it is clear whether the session path fires on
the same triggers.

FINDING: they do NOT. The ratio matches, the reserved-output allowance
does not, and the two count tokens differently:

  chat     should_compact(...)          reserved 2000, llm.count_tokens
  session  CompactionStrategy           reserved 8192, _estimate_tokens

On a 128k model that is a trigger at ~113.4k versus ~107.8k, so a
session compacts earlier than the same conversation would have as a
chat.

The divergence is left in place deliberately. The session strategy's
8192 carries a documented clamp (min(8192, context // 2)) that stops a
small-context model collapsing to a zero budget and compacting on every
turn, which the flat 2000 does not. Chat is being deleted, so its
constant does not survive the programme; retuning the surviving path to
match a dying one would change when every session compacts.

These tests pin the values so the difference stays deliberate and a
later edit to either side fails loudly.
"""

from primer.agent.compaction import CompactionStrategy
from primer.agent.compaction_mixin import (
    DEFAULT_RESERVED_OUTPUT_TOKENS,
    DEFAULT_TRIGGER_RATIO,
)


def test_trigger_ratio_agrees_across_both_paths():
    """The one constant both paths genuinely share."""
    assert DEFAULT_TRIGGER_RATIO == 0.90
    assert CompactionStrategy.DEFAULT_TRIGGER_RATIO == DEFAULT_TRIGGER_RATIO


def test_reserved_output_deliberately_differs():
    """Pinned, not fixed. See the module docstring for why."""
    assert DEFAULT_RESERVED_OUTPUT_TOKENS == 2000
    assert CompactionStrategy.DEFAULT_RESERVED_OUTPUT == 8192
    assert CompactionStrategy.DEFAULT_RESERVED_OUTPUT != DEFAULT_RESERVED_OUTPUT_TOKENS


def test_session_budget_clamps_so_small_models_do_not_thrash():
    """The reason the session path reserves more.

    A flat 8192 reserve against an 8192-context model would leave a zero
    budget, so the trigger would be 0 and compaction would fire on every
    turn, repeatedly summarising a tiny history. The clamp keeps at
    least half the context usable.
    """
    from primer.model_profile.resolver import ResolvedModel

    strategy = CompactionStrategy()
    small = ResolvedModel(
        profile_id="p", provider_id="prov", model_name="m",
        context_length=8192, config={},
    )
    budget = strategy._effective_budget(small)
    assert budget >= 8192 // 2
    assert int(strategy.trigger_ratio * budget) > 0


def test_large_context_is_unaffected_by_the_clamp():
    from primer.model_profile.resolver import ResolvedModel

    strategy = CompactionStrategy()
    big = ResolvedModel(
        profile_id="p", provider_id="prov", model_name="m",
        context_length=128000, config={},
    )
    assert strategy._effective_budget(big) == 128000 - 8192


def test_session_path_does_not_import_the_chat_threshold_helper():
    """base.py imported should_compact but never called it.

    The dead import made the two paths look shared when they are not,
    which is precisely the confusion this audit exists to remove.
    """
    from pathlib import Path

    src = Path("primer/agent/base.py").read_text()
    assert src.count("_mixin_should_compact") <= 1, (
        "base.py references the chat threshold helper more than once; "
        "if the session path now calls it, this audit needs revisiting"
    )
