"""The two copies of the process-group helper keep their SHARED functions identical.

The workspace runtime image is built from ``runtime/`` alone and cannot import ``primer``, so it carries its own copy of
``primer/common/process_group.py`` (``runtime/primer_runtime/process_group.py``). They differ on purpose where the stop
differs (the runtime sends SIGTERM and a grace period before the SIGKILL, the local copy signals the leader directly as
well, only the local copy has a Windows branch), but three functions are the same code and must stay so: the scan of
``/proc`` for a live member of the group, the group-has-a-live-member probe around it, and the pipe close. A fix made to
one copy and not the other (the ``None`` when no stat file could be read was once only in the local copy) changes what
the stop waits for in one place and not the other, and nothing else would fail.

The comparison is of the syntax trees with the docstrings removed: comments, spacing and docstring wording may differ.
"""

from __future__ import annotations

import ast
import inspect

import pytest

import primer.common.process_group as local_copy
import primer_runtime.process_group as runtime_copy

SHARED = ["_group_has_a_live_member", "_live_member_of", "_close_the_pipes"]


def _function(module, name: str) -> ast.AST:
    tree = ast.parse(inspect.getsource(module))
    (node,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name]
    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant) \
            and isinstance(node.body[0].value.value, str):
        node.body = node.body[1:] or [ast.Pass()]                      # drop the docstring
    return node


@pytest.mark.parametrize("name", SHARED)
def test_a_shared_helper_is_the_same_code_in_both_copies(name: str) -> None:
    assert ast.dump(_function(local_copy, name)) == ast.dump(_function(runtime_copy, name)), (
        f"{name} differs between primer/common/process_group.py and runtime/primer_runtime/process_group.py: a fix made "
        "to one copy has to be made to the other (the runtime image cannot import primer, so it carries its own)"
    )
