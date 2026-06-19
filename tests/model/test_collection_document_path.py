import pytest
from pydantic import ValidationError
from primer.model.collection import Document


def _doc(**kw):
    base = dict(id="document-abc", collection_id="col1", name="SLO", path="concepts/slo.md")
    base.update(kw)
    return Document(**base)


def test_path_required_and_stored():
    d = _doc()
    assert d.path == "concepts/slo.md"
    assert d.title is None


def test_title_optional():
    assert _doc(title="Service Level Objectives").title == "Service Level Objectives"


@pytest.mark.parametrize("bad", ["", "/leading", "a/../b", "a//b", "trailing/"])
def test_path_rejected(bad):
    with pytest.raises(ValidationError):
        _doc(path=bad)


def test_path_normalised_no_dot_segments():
    assert _doc(path="a/b/c.md").path == "a/b/c.md"
