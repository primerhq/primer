"""Entry point for ``python -m primer_runtime``.

Equivalent to ``python -m primer_runtime.server``.  The Dockerfile
ENTRYPOINT uses the server module directly; this module exists so that
``python -m primer_runtime`` also works.
"""

from primer_runtime.server import main

if __name__ == "__main__":
    main()
