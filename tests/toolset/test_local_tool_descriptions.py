import pytest

from primer.agent.tool_manager import _workspace_tool_descriptor
from primer.workspace.local.tools.ls import Ls
from primer.workspace.local.tools.read import Read
from primer.workspace.local.tools.write import Write
from primer.workspace.local.tools.edit import Edit
from primer.workspace.local.tools.glob import Glob
from primer.workspace.local.tools.grep import Grep
from primer.workspace.local.tools.exec_ import Exec
from primer.workspace.sandbox.tools.ls import SandboxLs
from primer.workspace.sandbox.tools.read import SandboxRead
from primer.workspace.sandbox.tools.write import SandboxWrite
from primer.workspace.sandbox.tools.edit import SandboxEdit
from primer.workspace.sandbox.tools.glob import SandboxGlob
from primer.workspace.sandbox.tools.grep import SandboxGrep
from primer.workspace.sandbox.tools.exec_ import SandboxExec
from tests.toolset._desc_conformance import assert_tool_conforms

# (local_class, sandbox_twin) pairs - same id, must share identical description + examples
_PAIRS = [
    (Ls, SandboxLs), (Read, SandboxRead), (Write, SandboxWrite), (Edit, SandboxEdit),
    (Glob, SandboxGlob), (Grep, SandboxGrep), (Exec, SandboxExec),
]


def test_local_tools_have_examples():
    for local, _ in _PAIRS:
        assert local.examples, f"{local.id}: no examples ClassVar"


def test_local_descriptors_conform():
    for local, _ in _PAIRS:
        inst = local.__new__(local)  # parameters() is pure
        tool = _workspace_tool_descriptor(inst, scoped_id=f"workspace__{local.id}")
        assert_tool_conforms(tool)


def test_local_and_sandbox_twins_match():
    # Drift guard: an agent must see identical guidance for the same tool id
    # regardless of workspace backend.
    for local, sandbox in _PAIRS:
        assert local.id == sandbox.id
        assert local.description == sandbox.description, f"{local.id}: description drift"
        assert local.examples == sandbox.examples, f"{local.id}: examples drift"
