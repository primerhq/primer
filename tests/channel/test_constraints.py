"""Pure single/multi association-constraint validator."""

from __future__ import annotations

import pytest

from primer.channel.constraints import (
    AssociationCounts, check_chat_association_allowed,
    check_workspace_association_allowed,
)
from primer.model.except_ import ConflictError


def _counts(*, ws=0, chat=0) -> AssociationCounts:
    return AssociationCounts(workspace_assocs=ws, chat_assocs=chat)


def test_single_chat_ok_when_empty():
    check_chat_association_allowed(supports_threads=False, counts=_counts())


def test_single_chat_conflicts_with_existing_workspace():
    with pytest.raises(ConflictError):
        check_chat_association_allowed(
            supports_threads=False, counts=_counts(ws=1))


def test_single_chat_conflicts_with_existing_chat():
    with pytest.raises(ConflictError):
        check_chat_association_allowed(
            supports_threads=False, counts=_counts(chat=1))


def test_single_workspace_conflicts_with_existing_chat():
    with pytest.raises(ConflictError):
        check_workspace_association_allowed(
            supports_threads=False, counts=_counts(chat=1))


def test_single_workspace_conflicts_with_existing_workspace():
    with pytest.raises(ConflictError):
        check_workspace_association_allowed(
            supports_threads=False, counts=_counts(ws=1))


def test_multi_chat_ok_when_none():
    check_chat_association_allowed(supports_threads=True, counts=_counts())


def test_multi_chat_ok_alongside_workspace_assocs():
    check_chat_association_allowed(
        supports_threads=True, counts=_counts(ws=3))


def test_multi_second_chat_conflicts():
    with pytest.raises(ConflictError):
        check_chat_association_allowed(
            supports_threads=True, counts=_counts(chat=1))


def test_multi_workspace_always_ok():
    check_workspace_association_allowed(
        supports_threads=True, counts=_counts(ws=5, chat=1))
