"""In-process throttling of sign-in attempts (architecture review A-07).

``POST /v1/auth/login`` used to be a free online guessing oracle: forty wrong passwords in a row for one account all answered 401
and the forty-first, correct one, answered 200. This is the counter the route now consults.

The rule, per ``(username, client address)``:

* the first ``free_attempts`` (5) attempts in a row are never delayed;
* from the ``free_attempts``-th on, the NEXT attempt must wait ``base_delay * 2 ** k`` seconds (2, 4, 8, ... capped at ``max_delay``,
  15 minutes), where ``k`` counts the attempts made since the free ones;
* an attempt made while a wait is in force is refused WITHOUT being examined and does not extend the wait, so hammering a locked
  key costs the attacker nothing extra and the legitimate user nothing more than the wait already set;
* one success forgets the key; a key idle for ``forget_after`` (1 hour) starts again from zero.

The count is taken when an attempt STARTS (:meth:`LoginThrottle.reserve`), not when it fails. A password check is slow and the
route awaits it, so counting afterwards would let a burst of parallel guesses all pass the check made before any of them finished.
:meth:`reserve` never awaits, so on one event loop it is atomic.

Every kind of failure is an attempt (unknown user, no local password, disabled, wrong password), and the key is the submitted
string, not the account: the refusal must not depend on the account existing, or a 429 would tell an attacker which usernames are
real.

Limits, stated on purpose:

* **One process.** The state lives in this object. A deployment with several API replicas (or uvicorn ``--workers`` > 1) gives each
  its own counters, so the effective limit is multiplied by the replica count until the state is shared. Nothing here is persisted
  and a restart forgets everything.
* **The key is only as good as the client address.** The route passes ``request.client.host`` and nothing else (``X-Forwarded-For``
  is never parsed here, a client can write it). Behind a reverse proxy uvicorn only rewrites it when the proxy is trusted
  (``FORWARDED_ALLOW_IPS``); otherwise every client shares the proxy's address and the key degrades to the username alone. That is
  still safe against guessing, but one attacker can then keep an account in backoff for everybody.
* **Per username, not per address.** One address trying one password against many usernames makes one attempt per key and is never
  throttled. This is a brake on guessing one account, not a defence against password spraying.
* **Bounded memory.** At most ``max_entries`` keys are tracked. When full, the oldest key that is not currently waiting is evicted,
  looking at the oldest 64 keys; only when all 64 are waiting is the oldest of them evicted, waiting or not. So flooding with
  invented usernames ages out idle keys first, and forgets a locked one only when the table is full AND the 64 oldest keys are all
  waiting. An attacker who locks 64 keys of their own and then fills the table (``max_entries`` requests, one counted attempt each) can
  steer eviction onto the oldest waiting key, which may be the one it wants released: that key loses its wait and its count and gets its
  free attempts back. The wait it loses is usually short (a key's wait grows only by attempts made after the previous wait elapsed), but
  this is a hole in "a locked key stays locked" and is NOT closed here (ticket 01a11a82-af8b).
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_FREE_ATTEMPTS = 5
DEFAULT_BASE_DELAY_SECONDS = 2.0
DEFAULT_MAX_DELAY_SECONDS = 900.0
DEFAULT_FORGET_AFTER_SECONDS = 3600.0
DEFAULT_MAX_ENTRIES = 10_000

# A username is at most 64 characters (LoginBody); the cap keeps a key small whatever a caller passes.
_USERNAME_KEY_LIMIT = 128
# Doubling past this is already far over any max_delay; the cap only keeps the exponent a small int.
_MAX_EXPONENT = 30
# How many of the oldest keys eviction looks at for one that is not waiting, before it gives up and evicts the oldest.
_EVICTION_SCAN = 64


@dataclass(slots=True)
class _Entry:
    attempts: int
    blocked_until: float
    touched: float
    # Whether a refusal in the CURRENT wait has already been announced (see LoginThrottle.first_refusal); a new wait resets it.
    announced: bool = False


class LoginThrottle:
    """Bounded, in-process counter of sign-in attempts. See the module docstring for the rule and its limits."""

    def __init__(
        self,
        *,
        free_attempts: int = DEFAULT_FREE_ATTEMPTS,
        base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
        max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
        forget_after: float = DEFAULT_FORGET_AFTER_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if free_attempts < 1 or max_entries < 1 or base_delay <= 0 or max_delay < base_delay or forget_after <= 0:
            raise ValueError("the throttle needs free_attempts >= 1, max_entries >= 1, 0 < base_delay <= max_delay and forget_after > 0")
        self._free_attempts = free_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._forget_after = forget_after
        self._max_entries = max_entries
        self._clock = clock
        # Oldest first: an entry moves to the end each time an attempt for it is let through.
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    def reserve(self, username: str, client: str) -> int:
        """Count an attempt that is about to be made, or refuse it.

        Returns ``0`` when the attempt may proceed (it has been counted; a failure needs no further call) or the whole seconds the
        caller must wait, at least 1, when it must be refused (nothing is counted, and the wait is not extended).
        """
        now = self._clock()
        key = (username[:_USERNAME_KEY_LIMIT], client)
        entry = self._entries.get(key)
        if entry is not None:
            if entry.blocked_until > now:
                return max(1, math.ceil(entry.blocked_until - now))
            if now - entry.touched >= self._forget_after:
                del self._entries[key]
                entry = None
        if entry is None:
            self._make_room(now)
            entry = _Entry(attempts=0, blocked_until=0.0, touched=now)
            self._entries[key] = entry
        entry.attempts += 1
        entry.touched = now
        if entry.attempts >= self._free_attempts:
            exponent = min(entry.attempts - self._free_attempts, _MAX_EXPONENT)
            entry.blocked_until = now + min(self._base_delay * 2**exponent, self._max_delay)
            entry.announced = False
        self._entries.move_to_end(key)
        return 0

    def first_refusal(self, username: str, client: str) -> bool:
        """Whether a refusal of ``(username, client)`` that :meth:`reserve` has just answered is the FIRST of the current wait.

        True once per wait, then False until a counted attempt sets the next one, so a caller that logs a refusal logs one line per wait
        and not one per request: a client hammering a locked key costs the log nothing more than the wait already set. Call it right
        after a :meth:`reserve` that returned a wait (it never awaits, so nothing can interleave); a key that is not waiting is not
        announced. It reads the clock again to tell "still waiting" from "expired", so a wait that expires between the two calls (two
        clock reads, no await between them, so only a clock tick apart) is not announced either; that costs one missing log line.
        """
        entry = self._entries.get((username[:_USERNAME_KEY_LIMIT], client))
        if entry is None or entry.announced or entry.blocked_until <= self._clock():
            return False
        entry.announced = True
        return True

    def succeeded(self, username: str, client: str) -> None:
        """Forget ``(username, client)``: the next failures start counting from zero."""
        self._entries.pop((username[:_USERNAME_KEY_LIMIT], client), None)

    def __len__(self) -> int:
        return len(self._entries)

    def _make_room(self, now: float) -> None:
        while len(self._entries) >= self._max_entries:
            victim = None
            for scanned, (key, entry) in enumerate(self._entries.items()):
                if entry.blocked_until <= now:
                    victim = key
                    break
                if scanned + 1 >= _EVICTION_SCAN:
                    break
            if victim is None:
                victim = next(iter(self._entries))
            del self._entries[victim]


__all__ = [
    "DEFAULT_BASE_DELAY_SECONDS",
    "DEFAULT_FORGET_AFTER_SECONDS",
    "DEFAULT_FREE_ATTEMPTS",
    "DEFAULT_MAX_DELAY_SECONDS",
    "DEFAULT_MAX_ENTRIES",
    "LoginThrottle",
]
