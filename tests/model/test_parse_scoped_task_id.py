"""``parse_scoped_task_id``: the one parser of the ``<node>:tool:<turn_seg>:<seq>`` scoped tool-call id.

Graph node ids are free-form (``:`` and ``.`` included), so the id is split from the RIGHT and the node keeps
everything left of ``:tool:``. The turn segment and the seq are canonical ASCII integers matched with
``re.fullmatch``: anything ``int()`` would also accept (a sign, a space, an underscore, a leading zero, a non-ASCII
digit, a trailing newline) is malformed and raises, never guessed at (mutation N66: ``int()`` instead of the regexes).
"""

from __future__ import annotations

import pytest

from primer.model.tool_call_task import (
    MalformedScopedIdError,
    ScopedId,
    parse_scoped_task_id,
    tool_call_task_id,
)
from primer.tap.delta import scoped_tool_call_id

_NODE_IDS = ["x", "a:b", "a.b", "worker[0]", 'n"1', "n\\2", "a:tool:1:2", "a/b"]


@pytest.mark.parametrize("node_id", _NODE_IDS)
@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize(("turn_no", "seq"), [(0, 1), (3, 2), (12, 40)])
def test_round_trip_with_the_one_mint_site(node_id: str, qualified: bool, turn_no: int, seq: int) -> None:
    scoped = scoped_tool_call_id(node_id, turn_no, seq)
    task_id = tool_call_task_id("s1", scoped) if qualified else scoped

    parsed = parse_scoped_task_id(task_id, "s1")

    assert parsed == ScopedId(
        node=node_id, turn_no=turn_no, epoch=0, seq=seq, scoped=scoped, turn_seg=str(turn_no),
    )


def test_the_agent_surface_none_node_is_x() -> None:
    assert parse_scoped_task_id(scoped_tool_call_id(None, 4, 1), "s1").node == "x"


@pytest.mark.parametrize(
    ("scoped", "turn_no", "epoch", "turn_seg"),
    [("x:tool:3.2:1", 3, 2, "3.2"), ("a:b:tool:0.1:7", 0, 1, "0.1"), ("x:tool:10.12:3", 10, 12, "10.12")],
)
def test_an_epoch_is_read_from_the_turn_segment(scoped: str, turn_no: int, epoch: int, turn_seg: str) -> None:
    for task_id in (scoped, f"s1/{scoped}"):
        parsed = parse_scoped_task_id(task_id, "s1")
        assert (parsed.turn_no, parsed.epoch, parsed.turn_seg, parsed.scoped) == (turn_no, epoch, turn_seg, scoped)


def test_the_prefix_of_another_session_stays_in_the_node() -> None:
    """Only the exact ``<this session>/`` prefix is the qualification: a node id that contains a slash, or an id
    qualified for another session, is parsed as it is, and is not an error."""
    parsed = parse_scoped_task_id("s2/x:tool:3:1", "s1")
    assert (parsed.node, parsed.scoped) == ("s2/x", "s2/x:tool:3:1")
    assert parse_scoped_task_id("s1/s2/x:tool:3:1", "s1").node == "s2/x"


@pytest.mark.parametrize(
    "scoped",
    [
        "x:tool:3.0:1",     # an epoch is written only when greater than zero
        "x:tool:+3:1",      # a sign
        "x:tool: 3:1",      # whitespace
        "x:tool:1_0:1",     # an underscore separator
        "x:tool:03:1",      # a leading zero
        "x:tool:3:01",      # a leading zero in the seq
        "x:tool:3:1\n",     # a trailing newline (``$`` would accept it)
        "x:tool:٣:1",  # an Arabic-Indic digit three
        "x:tool:3:١",  # an Arabic-Indic digit one in the seq
        # A non-ASCII digit AFTER the first digit: the leading ``[1-9]`` cannot reject it, only ``[0-9]`` (not
        # ``\d``) for the rest does (mutation: ``\d`` reads these as turn 13, seq 11 and epoch 13).
        "x:tool:1٣:1",
        "x:tool:3:1١",
        "x:tool:3.1٣:1",
        "x:tool:3.:1",      # an empty epoch
        "x:tool:3.-1:1",    # a negative epoch
        "x:tool:3.1.1:1",   # two epochs
        "x:call:3:1",       # not a tool id
        "x:tool:3",         # a missing segment
        "x:tool:3:0",       # the seq is 1-based
        ":tool:3:1",        # an empty node
        "x:tool::1",        # an empty turn
        "x:tool:3:",        # an empty seq
        "",
    ],
)
def test_a_malformed_id_raises_naming_the_id(scoped: str) -> None:
    for task_id in (scoped, f"s1/{scoped}"):
        with pytest.raises(MalformedScopedIdError) as caught:
            parse_scoped_task_id(task_id, "s1")
        assert repr(task_id) in str(caught.value)


def test_the_error_is_a_value_error() -> None:
    assert issubclass(MalformedScopedIdError, ValueError)


def test_the_session_id_is_required() -> None:
    """Parsed without the session, a qualified id would yield node ``s1/x`` and no error: the argument has no default."""
    with pytest.raises(TypeError):
        parse_scoped_task_id("s1/x:tool:3:1")  # type: ignore[call-arg]
