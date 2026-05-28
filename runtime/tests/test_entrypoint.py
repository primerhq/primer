"""Tests for the ``python -m matrix_runtime`` entrypoint (Task 6).

These tests verify that the entry point wiring is correct without actually
running the server or requiring Docker.
"""

from __future__ import annotations

import importlib
import inspect


def test_main_module_importable() -> None:
    """matrix_runtime.__main__ must be importable."""
    mod = importlib.import_module("matrix_runtime.__main__")
    assert mod is not None


def test_main_module_calls_server_main() -> None:
    """matrix_runtime.__main__ must import and expose ``main`` from server."""
    mod = importlib.import_module("matrix_runtime.__main__")
    assert hasattr(mod, "main"), "__main__ must expose 'main'"
    # Verify it's the same callable as server.main
    from matrix_runtime.server import main as server_main
    assert mod.main is server_main


def test_server_main_is_callable() -> None:
    """server.main() must be a plain callable (no required args)."""
    from matrix_runtime.server import main
    assert callable(main)
    sig = inspect.signature(main)
    # main() takes no required parameters
    for param in sig.parameters.values():
        assert param.default is not inspect.Parameter.empty, (
            f"server.main() must have no required parameters; found: {param.name}"
        )


def test_protocol_standalone() -> None:
    """matrix_runtime.protocol must not import from matrix package."""
    import matrix_runtime.protocol as proto
    # Verify key symbols exist (the inlined definitions)
    assert hasattr(proto, "OpName")
    assert hasattr(proto, "ErrorCode")
    assert hasattr(proto, "Request")
    assert hasattr(proto, "Response")
    assert hasattr(proto, "Event")
    assert hasattr(proto, "serialize")
    assert hasattr(proto, "deserialize")
    # Verify it is NOT re-exporting from matrix (i.e. the source is local)
    import inspect as _inspect
    src = _inspect.getfile(proto.OpName)
    assert "matrix_runtime" in src, (
        f"OpName should be defined in matrix_runtime, got: {src}"
    )
