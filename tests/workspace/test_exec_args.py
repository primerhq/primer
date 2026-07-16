"""Unit tests for ``ExecArgs.access`` / ``ExecArgs.writes``.

These fields are optional and backward compatible: existing callers
that pass neither field must be unaffected. The sandbox exec tool
imports ``ExecArgs`` verbatim from the local tools package, so this
single model update is shared by both backends.
"""

from __future__ import annotations

from primer.workspace.local.tools.exec_ import ExecArgs
from primer.workspace.sandbox.tools.exec_ import SandboxExec  # imports same ExecArgs


def test_exec_args_defaults_are_backward_compatible():
    a = ExecArgs(command="ls", description="list")
    assert a.access == "write"
    assert a.writes is None


def test_exec_args_accepts_read_and_writes():
    a = ExecArgs(command="grep x", description="search",
                 access="read", writes=["out.txt", "log/*.txt"])
    assert a.access == "read"
    assert a.writes == ["out.txt", "log/*.txt"]


def test_exec_args_rejects_bad_access():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ExecArgs(command="ls", description="d", access="append")


def test_sandbox_exec_shares_the_same_args_model():
    assert SandboxExec(sandbox=None, workspace_root="/workspace").parameters() is ExecArgs
