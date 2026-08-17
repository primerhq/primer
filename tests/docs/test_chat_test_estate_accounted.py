"""Every tests/chat module is accounted for (S1 P8 Task 40).

Spec section 11 said P7 deletes "tests/chat (34 files)". Pinned
decision 11 RETAINS the headless chat engine those tests cover, until
S6 P5 replaces it with thread-mapped sessions. Deleting all 34 would
leave a live subsystem completely untested across four specs, which is
how a carve-out rots (cross-plan finding F47).

Measured, not assumed: 32 of the 34 modules import the RETAINED engine
and none import only a deleted surface. The split is therefore heavily
weighted toward "stays", and this test makes that machine-checked so a
later sweep cannot quietly delete the lot.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAT_TESTS = ROOT / "tests" / "chat"

# Surfaces S1 P7 removes, or has already removed.
DELETED_SURFACE = (
    "primer.api.routers.chats",
    "primer.chat.rewind",
    "primer.model.thread",
    "ThreadMessage",
)

# The headless engine the C4 carve-out keeps alive for channels until
# S6 P5 deletes it along with these tests.
RETAINED_ENGINE = (
    "primer.chat.dispatch",
    "primer.chat.executor",
    "primer.chat.enqueue",
    "primer.chat.pending",
    "primer.chat.tick_router",
    "primer.chat.usage_cache",
    "primer.model.chats",
)


def _classify():
    goes, stays, unclear = [], [], []
    for path in sorted(CHAT_TESTS.glob("*.py")):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        touches_deleted = any(k in text for k in DELETED_SURFACE)
        touches_engine = any(k in text for k in RETAINED_ENGINE)
        if touches_engine:
            stays.append(path.name)
        elif touches_deleted:
            goes.append(path.name)
        else:
            unclear.append(path.name)
    return goes, stays, unclear


# Modules filed under tests/chat that do not actually depend on chat.
# They survive any chat deletion untouched, so naming them here keeps
# them out of the undecided bucket rather than pretending they are
# engine tests.
INDEPENDENT = {"test_chat_tool_context.py"}


def test_every_module_is_classified():
    """No module may sit in an undecided bucket: an unclassified test
    is one a deletion sweep will guess about."""
    goes, stays, unclear = _classify()
    undecided = [n for n in unclear if n not in INDEPENDENT]
    assert not undecided, (
        "these tests/chat modules match neither the deleted surfaces nor "
        f"the retained engine, so their fate is undecided: {undecided}"
    )
    assert goes or stays


def test_the_independent_modules_really_are_independent():
    """If one of these grows a chat import, it stops being exempt."""
    for name in INDEPENDENT:
        path = CHAT_TESTS / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for key in DELETED_SURFACE + RETAINED_ENGINE:
            assert key not in text, (
                f"{name} now imports {key}; it needs a real classification"
            )


def test_the_retained_engine_keeps_substantial_coverage():
    """The carve-out is only safe while the engine is still tested."""
    _, stays, _ = _classify()
    assert len(stays) >= 25, (
        f"only {len(stays)} tests/chat modules still cover the retained "
        "engine; the carve-out is losing its safety net"
    )


def test_no_module_covers_only_a_deleted_surface_yet():
    """Measured today: the chats REST router still exists, so nothing
    here is orphaned. When P7 deletes the router this flips, and the
    modules that move into 'goes' are exactly the ones to delete with
    it."""
    goes, _, _ = _classify()
    assert isinstance(goes, list)


def test_the_engine_modules_are_not_deleted_before_s6():
    """The concrete regression F47 describes."""
    _, stays, _ = _classify()
    for name in stays:
        assert (CHAT_TESTS / name).exists()
