"""The console's provider-type -> extra map may name only live extras.

S9 section 4 lists "console hints" as a swept artifact. The map is plain
JS, so this reads it as text rather than importing it.
"""

from __future__ import annotations

import re
from pathlib import Path

from primer.common.optional import EXTRA_MODULES

REPO = Path(__file__).resolve().parents[2]
CAPS_JS = REPO / "ui" / "foundation" / "capabilities.js"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"

_SYNC_COMMENT_ANCHOR = "--all-extras so the test env has every optional backend"


def _mapped_extras() -> set[str]:
    text = CAPS_JS.read_text(encoding="utf-8")
    block = text.split("EXTRA_FOR_PROVIDER_TYPE = {", 1)[1].split("};", 1)[0]
    return set(re.findall(r":\s*'([a-z]+)'", block)) | set(
        re.findall(r':\s*"([a-z]+)"', block)
    )


def _sync_comment_extras() -> set[str]:
    """The extras the `test` lane's sync comment claims it installs."""
    tail = CI_YML.read_text(encoding="utf-8").split(_SYNC_COMMENT_ANCHOR, 1)[1]
    listed = tail.split(";", 1)[0].split("(", 1)[1].split(")", 1)[0]
    return set(re.findall(r"[a-z_]+", listed))


def test_console_map_names_only_live_extras() -> None:
    unknown = _mapped_extras() - set(EXTRA_MODULES)
    assert not unknown, f"console maps provider types to dead extras: {unknown}"


def test_sync_comment_names_only_live_extras() -> None:
    """The lane comment is documentation the CI reader trusts; keep it true."""
    unknown = _sync_comment_extras() - set(EXTRA_MODULES)
    assert not unknown, f"ci.yml sync comment names dead extras: {unknown}"


def test_core_install_lane_covers_the_capability_guards() -> None:
    """The core-only lane must still run the guard suite it was built for."""
    lane = CI_YML.read_text(encoding="utf-8").split("core-install:", 1)[1]
    for path in (
        "tests/api/test_capabilities.py",
        "tests/common/test_optional.py",
        "tests/workspace/test_factory_guards.py",
    ):
        assert path in lane, f"core-install lane dropped {path}"
