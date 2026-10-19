"""No code names the default workspace's id (pivot P2 task 4).

The default workspace is named by ``SystemState.default_workspace_id`` and read with
``primer.bootstrap.prime.resolve_default_workspace_id``. The legacy id (``primer``) and the fresh-install id (``prime``)
are constants only so that the modules which CREATE, ADOPT or RESOLVE the default agree on them; every other module asks
the resolver. A module that imported a constant instead would keep working on one kind of install and break on the other.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: The only source files that may name the ids: the constants, the resolver and adopter, the ensure pass that creates the
#: row, and the versioned step that adopts an existing install's row.
ALLOWED = {
    "primer/bootstrap/defaults.py",
    "primer/bootstrap/prime.py",
    "primer/bootstrap/seed.py",
    "primer/storage/migrations/m010_default_workspace.py",
}
NAMES = ("LEGACY_DEFAULT_WORKSPACE_ID", "PRIME_WORKSPACE_ID", "RESERVED_DEFAULT_WORKSPACE")


def _sources():
    for path in sorted((ROOT / "primer").rglob("*.py")):
        yield str(path.relative_to(ROOT)), path.read_text(encoding="utf-8")


def test_only_the_prime_modules_name_the_default_ids():
    offenders = sorted(rel for rel, text in _sources() if rel not in ALLOWED and any(name in text for name in NAMES))
    assert offenders == [], (
        "these modules name the default workspace's id; ask primer.bootstrap.prime.resolve_default_workspace_id instead: "
        f"{offenders}"
    )


def test_the_retired_constant_is_gone():
    """``RESERVED_DEFAULT_WORKSPACE`` is deleted, not kept as an alias: an alias would let a new caller hard-code the id again."""
    from primer.bootstrap import defaults

    assert not hasattr(defaults, "RESERVED_DEFAULT_WORKSPACE")
    assert "RESERVED_DEFAULT_WORKSPACE" not in defaults.__all__


def test_no_module_compares_a_workspace_id_to_the_legacy_literal():
    """The constants are not the only way to hard-code it: ``workspace_id == "primer"`` is the same mistake."""
    pattern = re.compile(r"""(?:workspace_id|wid|ws_id)\s*(?:==|!=|in\s*\()\s*[\"']primer[\"']""")
    offenders = sorted(rel for rel, text in _sources() if pattern.search(text))
    assert offenders == [], f"these modules compare a workspace id to the literal 'primer': {offenders}"
