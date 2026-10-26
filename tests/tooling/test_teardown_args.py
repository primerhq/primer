"""Regression test for the e2e teardown compose args (C1: non-destructive default).

The default teardown must NOT drop the shared ``primer-pgdata`` volume (it would
wipe the dogfood data); the separately named destructive path (gated on
``PRIMER_E2E_DELETE_VOLUMES=1``) is the only mode that adds ``-v``. This test
inspects the GENERATED compose args (``scripts/e2e/teardown.sh --print-args``)
for both modes, without executing any compose command.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEARDOWN = ROOT / "scripts" / "e2e" / "teardown.sh"


def _print_args(destructive: bool, delete_volumes: bool) -> str:
    """Run ``teardown.sh --print-args`` and return the generated compose args."""
    env = os.environ.copy()
    if delete_volumes:
        env["PRIMER_E2E_DELETE_VOLUMES"] = "1"
    else:
        env.pop("PRIMER_E2E_DELETE_VOLUMES", None)
    args = ["bash", str(TEARDOWN), "--print-args"]
    if destructive:
        args.append("--destructive")
    result = subprocess.run(
        args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_default_mode_never_includes_v():
    """The default teardown's compose args never include -v (no volume drop)."""
    args = _print_args(destructive=False, delete_volumes=False)
    assert "-v" not in args.split(), f"default args must not include -v: {args!r}"


def test_destructive_mode_includes_v():
    """The destructive mode's compose args DO include -v (the opt-in volume drop)."""
    args = _print_args(destructive=True, delete_volumes=True)
    assert "-v" in args.split(), f"destructive args must include -v: {args!r}"


def test_destructive_flag_without_env_var_is_default():
    """--destructive WITHOUT PRIMER_E2E_DELETE_VOLUMES=1 falls back to the default (no -v)."""
    args = _print_args(destructive=True, delete_volumes=False)
    assert "-v" not in args.split(), (
        f"--destructive without the env var must be the default (no -v): {args!r}"
    )
