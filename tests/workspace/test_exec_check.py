"""`exec` can fail the tool call when the command fails.

Without `check`, a command that exits non-zero still produces a SUCCESSFUL tool
call. A graph node wrapping it therefore records ``status=completed`` and
``error=None``, so a run whose script raised is indistinguishable from one that
succeeded -- the only trace is an exit code at the head of the output text. That
misdirection is expensive: a scoring graph reported every node "completed" across
three separate failed runs while writing nothing.

Default stays False. Plenty of commands use a non-zero exit as information
(``grep`` finding nothing, ``diff`` reporting a difference), so failing by
default would break them; the flag is opt-in for steps whose failure should stop
the graph.
"""
import sys

import pytest

from primer.model.except_ import BadRequestError
from primer.workspace.local.tools.exec_ import ExecArgs, nonzero_exit_message

FAIL = f'"{sys.executable}" -c "import sys; sys.stderr.write(\'boom\\n\'); sys.exit(3)"'
OK = f'"{sys.executable}" -c "print(\'fine\')"'


def test_check_defaults_to_false():
    """The historical contract: a failing command is not a failing tool call."""
    assert ExecArgs(command="ls", description="d").check is False


def test_check_is_settable():
    assert ExecArgs(command="ls", description="d", check=True).check is True


def test_message_leads_with_the_exit_code_and_carries_the_tail():
    """The exit code alone sends the reader back to the raw output.

    Including the tail means the node's `error` field states the cause directly,
    which is the whole point -- the traceback existed all along, just not
    anywhere the run status was visible.
    """
    msg = nonzero_exit_message("python x.py", 1, "some stdout",
                               "Traceback (most recent call last):\nRuntimeError: boom")
    assert "exited 1" in msg
    assert "python x.py" in msg
    assert "RuntimeError: boom" in msg


def test_message_prefers_stderr_but_falls_back_to_stdout():
    assert "on stdout" in nonzero_exit_message("c", 2, "on stdout", "")
    assert "on stderr" in nonzero_exit_message("c", 2, "on stdout", "on stderr")


def test_message_is_bounded():
    """A runaway log must not become the node's error field verbatim."""
    msg = nonzero_exit_message("c", 1, "", "x" * 10_000)
    assert len(msg) < 1_200


@pytest.mark.asyncio
async def test_local_exec_raises_on_nonzero_when_checked(tmp_path):
    from primer.workspace.local.tools.exec_ import Exec

    tool = Exec(tmp_path)
    with pytest.raises(BadRequestError) as ei:
        await tool.execute(
            ExecArgs(command=FAIL, description="fails", check=True, workdir="."),
            _ctx(),
        )
    assert "exited 3" in str(ei.value)
    assert "boom" in str(ei.value), "the cause must reach the error message"


@pytest.mark.asyncio
async def test_local_exec_is_unchanged_without_check(tmp_path):
    """The default path must keep returning normally, exit code in the output."""
    from primer.workspace.local.tools.exec_ import Exec

    tool = Exec(tmp_path)
    result = await tool.execute(
        ExecArgs(command=FAIL, description="fails", workdir="."), _ctx()
    )
    assert result.metadata["exit_code"] == 3
    assert result.output.startswith("3\n")


@pytest.mark.asyncio
async def test_check_does_not_disturb_a_successful_command(tmp_path):
    from primer.workspace.local.tools.exec_ import Exec

    tool = Exec(tmp_path)
    result = await tool.execute(
        ExecArgs(command=OK, description="ok", check=True, workdir="."), _ctx()
    )
    assert result.metadata["exit_code"] == 0
    assert "fine" in result.output


def _ctx():
    """A context with only the fields `exec` actually reads.

    ToolCallContext requires a live AgentSession, which these tests have no use
    for; model_construct skips validation so the test stays about `check`.
    """
    import asyncio

    from primer.workspace.tool import ToolCallContext

    return ToolCallContext.model_construct(
        workspace_id="ws-1", session_id="s-1", agent_id="a-1", call_id="c-1",
        abort=asyncio.Event(), session=None,
    )
