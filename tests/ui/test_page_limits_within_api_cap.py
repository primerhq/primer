"""Every console list request must ask for a page the API will serve.

``OffsetPage.length`` is ``le=200`` (app spec section 4), so a console
component asking for more gets a 422 and the page renders with that data
missing. This is easy to do and invisible in review -- the JSX looks fine,
the failure only shows up as a failed fetch at runtime -- so it is pinned
here rather than left to a browser test to discover.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"

# Routes that declare their own, larger cap instead of taking OffsetPage:
# sessions/{id}/messages paginates through its own router, and
# collections/{id}/indexed_documents declares le=500 explicitly.
_EXEMPT = ("/messages", "/turn_log", "/events", "/indexed_documents")


def _api_cap() -> int:
    from primer.model.storage import OffsetPage

    return OffsetPage.model_fields["length"].metadata[-1].le


def test_no_console_request_exceeds_the_api_page_cap() -> None:
    cap = _api_cap()
    offenders = []
    for path in sorted(UI.rglob("*.js*")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.lstrip()
            # Comments describe history ("previously limit=1000"); only a
            # live request can 422.
            if stripped.startswith(("//", "*", "/*")):
                continue
            for m in re.finditer(r"limit=(\d+)", line):
                if int(m.group(1)) <= cap:
                    continue
                if any(e in line for e in _EXEMPT):
                    continue
                offenders.append(
                    f"{path.relative_to(ROOT)}: limit={m.group(1)} > {cap}"
                )
    assert not offenders, (
        "console list requests above the API page cap will 422:\n  "
        + "\n  ".join(offenders)
    )
