"""artifact_id satisfies the binary-source requirement + JSON round-trips."""

from __future__ import annotations

import pytest

from primer.model.chat import DocumentPart, ImagePart


def test_artifact_id_satisfies_source():
    p = ImagePart(artifact_id="artifact-abc", mime_type="image/png")
    assert p.artifact_id == "artifact-abc"
    assert p.data is None


def test_no_source_still_rejected():
    with pytest.raises(ValueError):
        ImagePart(mime_type="image/png")


def test_json_round_trip():
    p = DocumentPart(artifact_id="artifact-9", mime_type="application/pdf",
                     filename="r.pdf")
    dumped = p.model_dump(mode="json")
    assert dumped["artifact_id"] == "artifact-9"
    back = DocumentPart.model_validate(dumped)
    assert back.artifact_id == "artifact-9"
    assert back.filename == "r.pdf"
