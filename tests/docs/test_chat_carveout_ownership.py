"""The retained chat engine names its own deleter (S1 P7 Task 38).

Cross-plan findings F17, F32 and F47 all landed on the same gap: the
headless chat engine survives S1 because channels need it until S6
replaces channel conversations with thread-mapped sessions, and NOBODY
had been assigned its deletion. S1 cannot own it, because "S1 deletes
it" and "channels keep working" are mutually exclusive.

So the owner is written on the code. Every retained file carries the
stamp in its module docstring, and this test keeps it there: a
carve-out whose ownership lives only in a plan file is a carve-out that
becomes permanent by accident.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STAMP = "S6 P5 deletes this file (S1 P7 carve-out, crosscheck C4)."

# Every file the carve-out retains, grouped by why it survives.
CHANNEL_RELAY = (
    "primer/channel/chat_dispatcher.py",
    "primer/channel/chat_inbox.py",
    "primer/channel/chat_router.py",
)
TRIGGER_SUBSCRIBERS = (
    "primer/trigger/subscribers/start_chat.py",
    "primer/trigger/subscribers/chat_message.py",
)
HEADLESS_ENGINE = (
    "primer/model/chats.py",
    "primer/chat/dispatch.py",
    "primer/chat/executor.py",
    "primer/chat/enqueue.py",
    "primer/chat/pending.py",
    "primer/chat/tick_router.py",
    "primer/chat/usage_cache.py",
    "primer/claim/adapters/chats.py",
)
RETAINED = CHANNEL_RELAY + TRIGGER_SUBSCRIBERS + HEADLESS_ENGINE


def test_every_retained_file_names_its_deleter():
    unstamped = [
        rel for rel in RETAINED
        if (ROOT / rel).exists()
        and STAMP not in (ROOT / rel).read_text(encoding="utf-8")
    ]
    assert not unstamped, (
        "these retained chat files do not say who deletes them, so the "
        f"carve-out has lost its owner: {unstamped}"
    )


def test_the_retained_files_still_exist():
    """If one vanishes early, channels lost a dependency four specs
    before its replacement exists."""
    missing = [rel for rel in RETAINED if not (ROOT / rel).exists()]
    assert not missing, (
        f"retained chat files deleted before S6 P5: {missing}"
    )


def test_the_claim_lane_survives_with_the_engine():
    """The engine cannot run turns without its claim lane, so the two
    have to be deleted together rather than separately."""
    claim = (ROOT / "primer" / "int" / "claim.py").read_text(
        encoding="utf-8"
    )
    assert "CHAT" in claim
