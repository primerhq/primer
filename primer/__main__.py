"""Entry point: ``python -m primer ...`` runs the Typer CLI.

Mirrors the ``primer`` console script (``primer.cli:app``) so the
package can be launched with the module runner too -- used by the
distributed test harness, which spawns workers via
``sys.executable -m primer worker`` so it always hits the same
interpreter/venv as the test process.
"""

from __future__ import annotations

from primer.cli import app

if __name__ == "__main__":  # pragma: no cover
    app()
