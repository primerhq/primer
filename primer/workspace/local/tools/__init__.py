"""Concrete :class:`primer.workspace.tool.WorkspaceTool` implementations.

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

from primer.workspace.local.tools.edit import Edit, EditArgs
from primer.workspace.local.tools.exec_ import Exec, ExecArgs
from primer.workspace.local.tools.glob import Glob, GlobArgs
from primer.workspace.local.tools.grep import Grep, GrepArgs
from primer.workspace.local.tools.ls import Ls, LsArgs
from primer.workspace.local.tools.read import Read, ReadArgs
from primer.workspace.local.tools.write import Write, WriteArgs


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
