"""01a070ea: every writer on the SQLite shared connection must route
through ``_write_guard()`` - a bypasser that executes+commits outside the
guard interacts badly with the guard's own rollback semantics: a guarded
write failing while a bypasser sits between its own execute and commit
either loses the bypasser's statement (post-fix, via the guard's
rollback) or silently mis-commits it through the same interleave
(pre-fix). ``CorrelationStore._atomic_upsert``/``_ensure_unique_index``
and ``SqliteStorageProvider``'s six ``system_state`` setters were the
known bypassers; this file pins the fix (behavior) and adds a structural
check so the NEXT bypasser can't slip in unnoticed.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest
import pytest_asyncio

import primer.channel.correlation as correlation_module
import primer.storage.sqlite as sqlite_module
from primer.channel.correlation import CorrelationStore
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


# ---------------------------------------------------------------------------
# Structural: no commit() on the shared connection lives outside a
# _write_guard() block.
# ---------------------------------------------------------------------------

# transaction/read_snapshot issue their own BEGIN..COMMIT to IMPLEMENT the
# guard mechanism itself; _write_guard is the guard; initialize() runs once
# at startup, synchronously, before the provider is shared with any other
# coroutine, so no concurrent writer can race its DDL. All four are exempt
# by design, not bypassers.
_EXEMPT_FUNCTIONS = {"_write_guard", "transaction", "read_snapshot", "initialize"}


def _is_write_guard_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
        and node.func.attr == "_write_guard"


def _is_commit_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
        and node.func.attr == "commit"


class _UnguardedCommitFinder(ast.NodeVisitor):
    """Walks a module's AST tracking enclosing function name + whether the
    current position is nested inside an ``async with ..._write_guard():``
    block, and records every commit() call found outside one."""

    def __init__(self) -> None:
        self.function_stack: list[str] = []
        self.guard_depth = 0
        self.violations: list[str] = []

    def _visit_function(self, node: ast.AST) -> None:
        self.function_stack.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        is_guard = any(_is_write_guard_call(item.context_expr) for item in node.items)
        if is_guard:
            self.guard_depth += 1
        self.generic_visit(node)
        if is_guard:
            self.guard_depth -= 1

    def visit_Call(self, node: ast.Call) -> None:
        if _is_commit_call(node) and self.guard_depth == 0:
            fn_name = self.function_stack[-1] if self.function_stack else "<module>"
            if fn_name not in _EXEMPT_FUNCTIONS:
                self.violations.append(f"{fn_name}(): {ast.unparse(node)}")
        self.generic_visit(node)


def _find_unguarded_commits(module) -> list[str]:
    src = textwrap.dedent(inspect.getsource(module))
    tree = ast.parse(src)
    finder = _UnguardedCommitFinder()
    finder.visit(tree)
    return finder.violations


def test_sqlite_provider_has_no_unguarded_commits() -> None:
    violations = _find_unguarded_commits(sqlite_module)
    assert violations == [], (
        "commit() on the shared SQLite connection found outside "
        f"_write_guard(): {violations}"
    )


def test_correlation_store_has_no_unguarded_commits() -> None:
    violations = _find_unguarded_commits(correlation_module)
    assert violations == [], (
        "commit() on the shared SQLite connection found outside "
        f"_write_guard(): {violations}"
    )


# ---------------------------------------------------------------------------
# Behavior: CorrelationStore._atomic_upsert standalone AND deferred inside
# an enclosing transaction().
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def provider(tmp_path: Path):
    cfg = SqliteConfig(path=tmp_path / "write_guard_coverage.sqlite")
    p = SqliteStorageProvider(cfg)
    await p.initialize()
    try:
        yield p
    finally:
        await p.aclose()


async def test_atomic_upsert_standalone_commits_immediately(
    provider: SqliteStorageProvider,
) -> None:
    store = CorrelationStore(provider)
    await store.upsert_session(
        channel_id="ch1", anchor="a1", workspace_id="w1",
        session_id="s1", tool_call_id="tc1",
    )
    found = await store.lookup("ch1", "a1")
    assert found is not None
    assert found.session_id == "s1"


async def test_atomic_upsert_inside_transaction_commits_with_the_txn(
    provider: SqliteStorageProvider,
) -> None:
    """Standalone-vs-deferred parity: an upsert issued while an enclosing
    transaction() is open must ride that transaction's own commit, not
    commit itself early - the single INSERT...ON CONFLICT...RETURNING
    statement's result is visible within the same connection/transaction
    even before COMMIT, so nothing about the upsert's own return value
    should differ from the standalone case."""
    store = CorrelationStore(provider)
    async with provider.transaction():
        result = await store.upsert_session(
            channel_id="ch2", anchor="a2", workspace_id="w1",
            session_id="s2", tool_call_id="tc2",
        )
    assert result.session_id == "s2"
    found = await store.lookup("ch2", "a2")
    assert found is not None
    assert found.session_id == "s2"


async def test_atomic_upsert_inside_a_rolled_back_transaction_is_undone(
    provider: SqliteStorageProvider,
) -> None:
    """The crown-jewel proof: pre-fix, _atomic_upsert called
    self._sp.connection.commit() UNCONDITIONALLY, so an upsert issued
    inside an enclosing transaction() would commit itself immediately and
    survive that transaction's later rollback - a silent mis-commit
    through the exact interleave 01a070ea describes. Post-fix, the
    upsert defers to _write_guard's should_commit=False inside an owned
    transaction, so a rollback of the enclosing unit must undo it too."""
    store = CorrelationStore(provider)
    with pytest.raises(RuntimeError):
        async with provider.transaction():
            await store.upsert_session(
                channel_id="ch3", anchor="a3", workspace_id="w1",
                session_id="s3", tool_call_id="tc3",
            )
            raise RuntimeError("roll back after the upsert")

    found = await store.lookup("ch3", "a3")
    assert found is None, (
        "the upsert must not survive the enclosing transaction's rollback"
    )
    # The connection must not be left wedged in a skip-commit state.
    await store.upsert_session(
        channel_id="ch3", anchor="a3", workspace_id="w1",
        session_id="s3-after", tool_call_id="tc3-after",
    )
    after = await store.lookup("ch3", "a3")
    assert after is not None and after.session_id == "s3-after"


async def test_system_state_setter_inside_a_rolled_back_transaction_is_undone(
    provider: SqliteStorageProvider,
) -> None:
    """Same interleave, the sqlite.py side: a system_state setter issued
    inside an enclosing transaction() must roll back with it, not survive
    via its own unconditional pre-fix commit()."""
    with pytest.raises(RuntimeError):
        async with provider.transaction():
            await provider.set_default_agent_id("ag-doomed")
            raise RuntimeError("roll back after the setter")

    state = await provider.get_system_state()
    assert state.default_agent_id is None

    await provider.set_default_agent_id("ag-after")
    state_after = await provider.get_system_state()
    assert state_after.default_agent_id == "ag-after"
