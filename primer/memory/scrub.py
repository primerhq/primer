"""The deterministic scrubber for memory deltas (spec 6.4).

A delta whose text has ANY hit is dropped; nothing is repaired. The classes: ``email``, ``phone``, ``url_credentials``, ``token``, ``ip``,
``known_name`` (an install user's username or email local part), ``workspace_id``, ``session_id`` and ``credential`` (what the credential scrubber
of LLM and speech errors would mask, the provider's configured keys included).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

from primer.llm._failure import _CONTROL_TABLE, scrubbed_event_text
from primer.model.storage import OffsetPage
from primer.model.user import User

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("phone", re.compile(r"\+\d[\d\s().-]{7,}\d|(?<![\w.])\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\w|\.\d)")),
    ("url_credentials", re.compile(r"\b[a-zA-Z][\w+.-]*://[^/\s:@]+:[^/\s@]+@")),
    ("token", re.compile(
        r"\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b"
        r"|\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"
    )),
    # Four dotted octets; an IPv6 has a `::` or at least four groups (so a clock time "12:30:45" is not one).
    ("ip", re.compile(rf"(?<![\w.]){_OCTET}(?:\.{_OCTET}){{3}}(?!\w|\.\d)|(?<![\w:])(?:[0-9A-Fa-f]{{1,4}}:){{3,7}}[0-9A-Fa-f]{{1,4}}(?![\w:])|(?<![\w:])[0-9A-Fa-f:]*::[0-9A-Fa-f:]*(?![\w:])")),
    # An id carries a digit, so the words "workspace-template" and "ws-style" are not ids.
    ("workspace_id", re.compile(r"\b(?:ws|workspace)-(?=[\w-]*\d)[\w-]{6,}")),
    ("session_id", re.compile(r"\b(?:sess|session)-(?=[\w-]*\d)[\w-]{6,}")),
)

#: The credential check compares in slices this long: the scrubber cuts and caps what it is given.
_SLICE = 3000
#: A name shorter than this is not matched (a two-letter username would blank ordinary words).
_MIN_NAME = 3
#: ``scrubbed_event_text`` reads ``provider.config`` for the keys to mask; with no provider there are none.
_NO_PROVIDER = SimpleNamespace(config=None)


def personal_data_hits(text: str, *, known_names: Iterable[str], provider: Any | None = None) -> list[str]:
    """The classes of personal data found in ``text``; empty means clean."""
    text = text.translate(_CONTROL_TABLE)
    hits = [name for name, pattern in _PATTERNS if pattern.search(text)]
    names = {n.strip() for n in known_names if len(n.strip()) >= _MIN_NAME}
    if any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text, re.IGNORECASE) for n in names):
        hits.append("known_name")
    who = provider if provider is not None else _NO_PROVIDER
    if any(scrubbed_event_text(text[i : i + _SLICE], who) != text[i : i + _SLICE] for i in range(0, len(text), _SLICE)):
        hits.append("credential")
    return hits


async def known_names(sp: Any) -> list[str]:
    """Every user's ``username`` and the local part of their ``email``."""
    users = sp.get_storage(User)
    names: list[str] = []
    offset = 0
    while True:
        page = await users.list(OffsetPage(offset=offset, length=200))
        for user in page.items:
            names.append(user.username)
            if user.email and "@" in user.email:
                names.append(user.email.split("@", 1)[0])
        offset += len(page.items)
        if not page.items or offset >= page.total:
            return names
