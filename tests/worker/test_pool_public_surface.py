"""Characterization test locking the public import surface of
:mod:`primer.worker.pool`.

The pool module is being carved into cohesive helper modules (drivers,
io_shim, engine_handlers, session_resume_coordinator, ...). The names below
are imported at runtime by sibling modules, ``primer.agent.invoke``, and the
test suite, and one is a monkeypatch seam (``run_one_session_turn`` is patched
as ``primer.worker.pool.run_one_session_turn``). They MUST stay importable as
``primer.worker.pool.<name>`` through every extraction step.

Importing the module itself must also succeed — the helper modules import
names back from ``primer.worker.pool`` (bottom-import cycle), so a broken
extraction would surface here as an ImportError.
"""

from __future__ import annotations


def test_public_symbols_importable():
    from primer.worker.pool import (  # noqa: F401
        WorkerPool,
        _TurnDriver,
        _GraphTurnDriver,
        _WorkspaceIOShim,
        _toolset_ids_from_scoped,
        run_one_session_turn,
    )


def test_module_imports_without_cycle():
    import primer.worker.pool as pool_mod

    # A broken bottom-import cycle would raise ImportError at collection
    # time (this module imports names from pool above), so reaching here
    # already proves the module initialised. Assert the class binding to
    # keep the intent explicit.
    assert hasattr(pool_mod, "WorkerPool")
