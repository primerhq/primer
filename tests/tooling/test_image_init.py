"""Both images run their process under an init (tini), so a killed child that nothing waits for is reaped.

Before, each image ran Python as PID 1 with no init (the primer image through ``primer-entrypoint.sh``, which ``exec``s
the command; the runtime image straight into ``python -m primer_runtime.server``). A process whose parent exits is
reparented to PID 1, which never ``wait()``s for it, so every process a command left behind that later died stayed a
zombie for the life of the container, and ``killpg(pgid, 0)`` kept reporting the group present
(``tests/runtime/test_process_group.py`` and ``primer/common/process_group.py`` work around that by reading
``/proc/<pid>/stat``; the zombies themselves were never reaped).

These are static checks of the two Dockerfiles: building an image is a heavy slot on the shared host and is the lead's
deploy step, so nothing here starts a container. What they pin:

* tini is installed from the distribution (``apt-get install ... tini``) in both images;
* the ENTRYPOINT is the exec form and starts with ``/usr/bin/tini -s --``: exec form so tini, and not a shell, is
  PID 1 and receives the signals; ``-s`` registers tini as a child subreaper when it is NOT PID 1 (Docker's own
  ``--init``, or a Kubernetes pod with a shared process namespace, puts another init above it), so it reaps either way;
* what follows ``--`` is the process the image ran before: the primer entrypoint script, and the runtime server.
  Workspace templates extend the runtime image and must not override its ENTRYPOINT (see ``runtime/Dockerfile``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

TINI = ["/usr/bin/tini", "-s", "--"]


def _logical_lines(path: Path) -> list[str]:
    """The Dockerfile's instructions, backslash continuations joined, comments and blank lines dropped."""
    lines: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if stripped.startswith("#"):
            continue                                   # a comment inside a continued instruction
        pending += (" " if pending else "") + stripped.rstrip("\\").strip()
        if not stripped.endswith("\\"):
            lines.append(pending)
            pending = ""
    return lines


def _entrypoints(path: Path) -> list[list[str]]:
    found: list[list[str]] = []
    for line in _logical_lines(path):
        match = re.match(r"ENTRYPOINT\s+(\[.*\])\s*$", line)
        if match:
            found.append(json.loads(match.group(1)))
    return found


def _apt_packages(path: Path) -> set[str]:
    packages: set[str] = set()
    for line in _logical_lines(path):
        for command in line.split("&&"):
            tokens = command.split()
            if tokens[:1] == ["RUN"]:
                tokens = tokens[1:]
            if tokens[:2] == ["apt-get", "install"]:
                packages.update(t for t in tokens[2:] if not t.startswith("-"))
    return packages


IMAGES = [
    pytest.param("Dockerfile", ["/usr/local/bin/primer-entrypoint.sh"], id="primer"),
    pytest.param("runtime/Dockerfile", ["python", "-m", "primer_runtime.server"], id="runtime"),
]


@pytest.mark.parametrize(("dockerfile", "process"), IMAGES)
def test_the_image_installs_tini_from_the_distribution(dockerfile: str, process: list[str]) -> None:
    assert "tini" in _apt_packages(ROOT / dockerfile), f"{dockerfile} does not apt-get install tini"


@pytest.mark.parametrize(("dockerfile", "process"), IMAGES)
def test_the_entrypoint_is_exec_form_and_starts_under_tini_as_a_subreaper(dockerfile: str, process: list[str]) -> None:
    entrypoints = _entrypoints(ROOT / dockerfile)

    assert len(entrypoints) == 1, f"{dockerfile} must have exactly one exec-form ENTRYPOINT, found {entrypoints}"
    assert entrypoints[0][: len(TINI)] == TINI, f"{dockerfile} ENTRYPOINT does not start with {TINI}: {entrypoints[0]}"


@pytest.mark.parametrize(("dockerfile", "process"), IMAGES)
def test_what_tini_runs_is_what_the_image_ran_before(dockerfile: str, process: list[str]) -> None:
    (entrypoint,) = _entrypoints(ROOT / dockerfile)

    assert entrypoint[len(TINI):] == process


def test_the_primer_default_command_is_still_a_cmd_so_deployments_can_override_it() -> None:
    """A deployment overrides ``args`` (Kubernetes) or ``command`` (compose), which replace CMD and keep the ENTRYPOINT, so
    they still run under tini. Moving the command into the ENTRYPOINT would make that override impossible."""
    lines = _logical_lines(ROOT / "Dockerfile")

    assert any(re.match(r"CMD\s+\[", line) for line in lines)


def test_the_helper_reads_the_real_dockerfiles() -> None:
    """The checks above would pass vacuously on an empty parse; this is the control that they see the files."""
    assert _entrypoints(ROOT / "Dockerfile") and _entrypoints(ROOT / "runtime/Dockerfile")
    assert {"git"} <= _apt_packages(ROOT / "runtime/Dockerfile")
    assert {"curl", "ca-certificates", "git"} <= _apt_packages(ROOT / "Dockerfile")
