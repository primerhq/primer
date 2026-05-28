"""Entry point for ``python -m matrix_runtime``.

Equivalent to ``python -m matrix_runtime.server``.  The Dockerfile
ENTRYPOINT uses the server module directly; this module exists so that
``python -m matrix_runtime`` also works.
"""

from matrix_runtime.server import main

if __name__ == "__main__":
    main()
