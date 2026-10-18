"""Messages from one session to another: the guards, the rate ledger and the send (pivot P3, design section 6.1).

``workspaces__message_session`` lets the agent in a workspace session hand a message to another session of its workspace (or, from the default
workspace, of any workspace). This module is the service behind it: the tool handlers are thin wrappers over :func:`send_peer_message`. A message
travels the delivery path every machine entrypoint shares (:func:`primer.session.steer_delivery.deliver_steer`): an idle or ended session starts a
turn with it, a session with a turn open reads it at its next turn boundary, and a gate the session waits on is never answered by it.

What a sender may do is decided here, before anything is written. The guards run in this order, and the first one that fails refuses the send with a
:class:`PeerMessageRefused` that carries the tool's error ``type`` and writes nothing (no ledger row, no queued message, no record, no event):

1. the target is not the sender (``bad-request``);
2. the sender is a session (``unsupported-surface``);
3. the target is within reach: it is in the sender's workspace, or the sender is in the default workspace. Out of reach is ``not-found`` in the same
   words as a session that does not exist, so a session id seen in a shared file or a log does not confirm that the session exists;
4. the sender ranks at least as high as the target's creator (``forbidden``): the target runs at its creator's rank, so a message drives a run at
   that rank. The rule is :func:`primer.authz.principal_rank`, the one a trigger's steer uses;
5. the chain of peer messages is shorter than ``MAX_PEER_HOPS`` (``hop-limit``);
6. the sender has sent fewer than ``PEER_RATE_LIMIT`` messages in the last ``PEER_RATE_WINDOW`` (``rate-limited``);
7. with ``mode="if_idle"``, the target would start a turn now (``conflict``);
8. the target can take a message at all: an ended session that cannot be reopened refuses (``conflict``).
"""

from __future__ import annotations

from primer.authz import principal_rank, rank_label
from primer.model.principal import PrincipalRef


class PeerMessageRefused(Exception):
    """A peer message was refused.

    ``error_type`` is the ``type`` the tool answers with (``forbidden``, ``not-found``, ``hop-limit``...), ``message`` the text the sending agent
    reads (``str(exc)`` is the same text). A refusal writes nothing.
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


def sender_may_message(sender: PrincipalRef | None, target_creator: PrincipalRef | None) -> bool:
    """Whether a run started by ``sender`` may put a message into a session its creator ``target_creator`` started.

    The target session runs at its creator's rank, so the message drives a run at that rank: the sender must rank at least as high
    (:func:`primer.authz.principal_rank`; a target with no recorded creator runs as an unattributed user and ranks as one). A sender with no
    principal at all is refused: an unknown run does not get to drive anyone's (fail closed).
    """
    if sender is None:
        return False
    return principal_rank(sender) >= principal_rank(target_creator)


__all__ = ["PeerMessageRefused", "rank_label", "sender_may_message"]
