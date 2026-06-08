import pytest

from primer.agent.tool_manager import _workspace_tool_descriptor
from primer.workspace.sandbox.tools.ls import SandboxLs
from primer.workspace.sandbox.tools.read import SandboxRead
from primer.workspace.sandbox.tools.write import SandboxWrite
from primer.workspace.sandbox.tools.edit import SandboxEdit
from primer.workspace.sandbox.tools.glob import SandboxGlob
from primer.workspace.sandbox.tools.grep import SandboxGrep
from primer.workspace.sandbox.tools.exec_ import SandboxExec
from tests.toolset._desc_conformance import assert_tool_conforms

_CLASSES = [SandboxLs, SandboxRead, SandboxWrite, SandboxEdit, SandboxGlob, SandboxGrep, SandboxExec]


def test_sandbox_tool_classes_have_examples():
    for cls in _CLASSES:
        assert cls.examples, f"{cls.id}: no examples ClassVar"


def test_workspace_descriptors_conform():
    for cls in _CLASSES:
        inst = cls.__new__(cls)  # parameters() is pure; skip __init__
        tool = _workspace_tool_descriptor(inst, scoped_id=f"workspace__{cls.id}")
        assert_tool_conforms(tool)
