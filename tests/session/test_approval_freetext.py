"""Free-text yes/no on approval gates (S1 P3 Task 22).

Ported verbatim from chat's classifier, including its quirks: these
decide whether a tool runs, so changing the parse silently would change
what gets approved.

Pinned decision 13: negation VETOES affirmation. "yes, but no" is a
rejection, because failing closed is the only safe direction for a gate.
"""

import pytest

from primer.session.yields import classify_approval_text


class TestAffirmative:
    @pytest.mark.parametrize(
        "text",
        ["yes", "y", "approve", "approved", "ok", "okay", "sure", "go"],
    )
    def test_each_affirmative_token(self, text):
        assert classify_approval_text(text) is True

    def test_case_is_folded(self):
        assert classify_approval_text("YES") is True
        assert classify_approval_text("Approve") is True

    def test_affirmative_inside_a_sentence(self):
        assert classify_approval_text("ok go ahead") is True


class TestNegative:
    @pytest.mark.parametrize(
        "text",
        ["no", "n", "nope", "nah", "deny", "denied", "reject",
         "rejected", "cancel", "stop", "dont", "don't"],
    )
    def test_each_negative_token(self, text):
        assert classify_approval_text(text) is False

    def test_negation_vetoes_affirmation(self):
        """Fails closed: ambiguity is never an approval."""
        assert classify_approval_text("yes but no") is False
        assert classify_approval_text("no yes") is False


class TestUndecided:
    @pytest.mark.parametrize(
        "text",
        ["what does this do", "", "   ", "maybe later", "explain first"],
    )
    def test_unrelated_prose_is_not_a_decision(self, text):
        """None means the caller keeps its existing behaviour rather
        than guessing at intent."""
        assert classify_approval_text(text) is None


class TestPortedQuirks:
    """Faithful to the chat original, warts included.

    Both of these are worth fixing, but not silently inside a port: they
    change which replies approve a tool call.
    """

    def test_punctuation_is_not_stripped(self):
        """"yes." does not approve, because tokens are whitespace-split
        and never depunctuated. The chat surface behaves identically."""
        assert classify_approval_text("yes.") is None
        assert classify_approval_text("yes!") is None

    def test_multiword_negatives_never_match(self):
        """"do not" is in the ported token set but cannot match a
        whitespace-split token, so it is dead as written."""
        assert classify_approval_text("do not") is None
