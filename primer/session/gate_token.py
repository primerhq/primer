"""What a decision says about WHICH gate it answers (console review C-033, ticket 01a11f52-9d98).

The REST respond routes (``primer.api.gate_fence``) and the channel inbox share three things, kept here so neither layer imports the other:

* :class:`StaleGateError`, raised for a decision that names a gate which is no longer the pending one (the REST face is a 409 ``approval_stale``);
* :func:`gate_token_matches`, how a token is judged against the pending gate's id. A full id (32 hex characters) must be equal; a platform that can
  carry only a prefix (Discord's 100-character custom id) sends the first :data:`SHORT_GATE_TOKEN_LEN`, which matches by prefix;
* :func:`count_gate_token`, the ``gate_respond_total{kind,gate_token}`` counter and the one INFO line for a decision that named none (a client that
  predates the token), so the flip to refusing those can be scheduled once the count is zero.
"""

from __future__ import annotations

import logging

from primer.model.except_ import PrimerError


logger = logging.getLogger(__name__)


SHORT_GATE_TOKEN_LEN = 12
"""How much of a gate id a platform that cannot carry all of it sends (48 bits: fencing a stale click, not authenticating one)."""


class StaleGateError(PrimerError):
    """A decision named a gate that is no longer the pending one; nothing was decided."""

    def __init__(self, kind: str = "approval") -> None:
        self.kind = kind
        super().__init__(f"this {'question' if kind == 'ask_user' else 'approval'} was replaced by a newer one")


def short_gate_token(gate_id: str | None) -> str | None:
    """The prefix of a gate id that a platform with a tight limit carries; ``None`` for a gate with no id."""
    return gate_id[:SHORT_GATE_TOKEN_LEN] if gate_id else None


def gate_token_matches(current_gate_id: str | None, token: str) -> bool:
    """Whether ``token`` names the gate whose id is ``current_gate_id``.

    A gate with no id (a park from before gates had ids) is named by no token. A full id must be equal; a shorter token (at least
    :data:`SHORT_GATE_TOKEN_LEN` characters) must be a prefix.
    """
    if not current_gate_id:
        return False
    return current_gate_id == token or (len(token) >= SHORT_GATE_TOKEN_LEN and current_gate_id.startswith(token))


def count_gate_token(*, kind: str, session_id: str, token: str | None, stale: bool = False) -> None:
    """Count one decision by what it said about which gate it answers, and log one that said nothing.

    ``kind`` is ``approval`` or ``ask_user``; ``stale`` marks a decision that named a gate that is not the pending one. The log line names the
    session and never the call's arguments.
    """
    import primer.observability.metrics as metrics

    outcome = "stale" if stale else ("matched" if token is not None else "absent")
    metrics.gate_respond_total.labels(kind=kind, gate_token=outcome).inc()
    if outcome == "absent":
        logger.info("%s decision without a gate_id on session %s: accepted (a client that predates the token)", kind, session_id)


__all__ = ["SHORT_GATE_TOKEN_LEN", "StaleGateError", "count_gate_token", "gate_token_matches", "short_gate_token"]
