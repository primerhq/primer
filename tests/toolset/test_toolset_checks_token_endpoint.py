"""A client-credentials toolset's token endpoint is an OAuth endpoint like its redirect URI and its resource (plan 07, Task 3).

The client secret is sent to the token endpoint, so an update that moves the endpoint while the secret rides along masked would hand the stored secret to
a server of the caller's choosing. ``repoints_stored_secrets`` counts the move, and ``toolset_admin_reason`` reserves it to an admin; the origin rule of
``preserve_masked_secrets`` refuses it for everyone (``tests/model/test_masked_secret_origin_threading.py``).
"""

from __future__ import annotations

from primer.model.provider import Toolset
from primer.toolset.toolset_checks import repoints_stored_secrets, toolset_admin_reason

MASK = "**********"
STORED = "real-client-secret"


def _toolset(token_endpoint: str | None, secret: str, *, url: str = "https://home.example/mcp") -> Toolset:
    oauth: dict = {"grant": "client_credentials", "static_client": {"client_id": "c", "client_secret": secret}}
    if token_endpoint is not None:
        oauth["token_endpoint"] = token_endpoint
    return Toolset.model_validate({"id": "kb", "provider": "mcp", "config": {"transport": "http", "config": {"url": url, "oauth": oauth}}})


def test_a_token_endpoint_move_with_a_masked_secret_is_a_repoint():
    stored = _toolset("https://home.example/token", STORED)
    moved = _toolset("https://attacker.example/token", MASK)
    assert repoints_stored_secrets(moved, stored) is True
    assert toolset_admin_reason(moved, stored) is not None


def test_a_token_endpoint_path_change_with_a_masked_secret_is_a_repoint_for_the_admin_gate():
    """Same origin, so the secret may be kept, but only an admin may change where it is sent (the same rule as a path change of the URL)."""
    stored = _toolset("https://home.example/token", STORED)
    moved = _toolset("https://home.example/oauth2/token", MASK)
    assert repoints_stored_secrets(moved, stored) is True


def test_the_same_token_endpoint_with_a_masked_secret_is_not_a_repoint():
    stored = _toolset("https://home.example/token", STORED)
    same = _toolset("https://home.example/token", MASK)
    assert repoints_stored_secrets(same, stored) is False
    assert toolset_admin_reason(same, stored) is None


def test_a_token_endpoint_that_appears_with_a_masked_secret_is_a_repoint():
    stored = _toolset(None, STORED)
    moved = _toolset("https://attacker.example/token", MASK)
    assert repoints_stored_secrets(moved, stored) is True


def test_re_entering_the_secret_lifts_the_repoint():
    """A secret the caller typed goes nowhere it was not sent by that caller: the user tier may move the endpoint when it re-enters the secret."""
    stored = _toolset("https://home.example/token", STORED)
    moved = _toolset("https://other.example/token", "typed-by-the-caller")
    assert repoints_stored_secrets(moved, stored) is False
    assert toolset_admin_reason(moved, stored) is None


def test_a_toolset_with_a_token_endpoint_and_no_change_is_not_a_repoint():
    stored = _toolset("https://home.example/token", STORED)
    assert repoints_stored_secrets(stored.model_copy(deep=True), stored) is False
