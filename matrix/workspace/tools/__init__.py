"""Concrete :class:`matrix.workspace.tool.WorkspaceTool` implementations.

Seven tools, each in its own module:

* :class:`Ls` -- ``ls``: list directory contents.
* :class:`Read` -- ``read``: read a file with offset/limit pagination.
* :class:`Write` -- ``write``: create / replace a file (read-before-write rule).
* :class:`Edit` -- ``edit``: string-replace edit producing a unified diff.
* :class:`Glob` -- ``glob``: find files by glob, sorted by mtime.
* :class:`Grep` -- ``grep``: regex search across files.
* :class:`Exec` -- ``exec``: run a shell command (foreground only in v1).

The seven tools together comprise the workspace's tool surface; the
agent runtime composes them onto an agent's other tools at session
start. Per the spec, they are NOT registered in the global tools
collection.
"""

from matrix.workspace.tools.edit import Edit, EditArgs
from matrix.workspace.tools.exec_ import Exec, ExecArgs
from matrix.workspace.tools.glob import Glob, GlobArgs
from matrix.workspace.tools.grep import Grep, GrepArgs
from matrix.workspace.tools.ls import Ls, LsArgs
from matrix.workspace.tools.read import Read, ReadArgs
from matrix.workspace.tools.write import Write, WriteArgs


__all__ = [
    "Edit",
    "EditArgs",
    "Exec",
    "ExecArgs",
    "Glob",
    "GlobArgs",
    "Grep",
    "GrepArgs",
    "Ls",
    "LsArgs",
    "Read",
    "ReadArgs",
    "Write",
    "WriteArgs",
]
