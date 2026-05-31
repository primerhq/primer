"""Per-node-kind form fields wired."""
from pathlib import Path
SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "graphs.jsx"
def _src() -> str: return SRC.read_text(encoding="utf-8")

def test_agent_form_has_input_template_and_schemas() -> None:
    src = _src()
    assert "input_template" in src
    assert "input_schema" in src
    assert "response_format" in src

def test_end_form_has_output_template_and_schema() -> None:
    src = _src()
    assert "output_template" in src
    assert "output_schema" in src

def test_begin_form_has_input_schema() -> None:
    src = _src()
    assert "input_schema" in src and 'kind === "begin"' in src

def test_description_field_on_all_node_kinds() -> None:
    src = _src()
    # The description field is rendered in the side panel for begin/end/agent/graph.
    assert src.count('"description"') >= 1

def test_max_iterations_field_present() -> None:
    src = _src()
    assert "max_iterations" in src
