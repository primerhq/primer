from pathlib import Path
import textwrap

from scripts.tests.coverage_matrix import (
    scan_smk_ids,
    scan_markers,
    classify,
    render,
)


def test_scan_and_classify(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "01.md").write_text(
        "## SMK-FOO-01: A\n### SMK-FOO-02: B\n## SMK-FOO-03: C\n", encoding="utf-8"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text(
        textwrap.dedent('''
        from tests._support.smk import smk
        @smk("SMK-FOO-01")
        def test_a(): ...
        @smk("SMK-FOO-02", status="partial")
        def test_b(): ...
    '''),
        encoding="utf-8",
    )
    ids = scan_smk_ids(docs)
    assert ids == ["SMK-FOO-01", "SMK-FOO-02", "SMK-FOO-03"]
    markers = scan_markers(tests)
    rows = classify(ids, markers)
    assert rows["SMK-FOO-01"] == "full"
    assert rows["SMK-FOO-02"] == "partial"
    assert rows["SMK-FOO-03"] == "none"


def test_full_wins_over_partial(tmp_path: Path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text(
        'from tests._support.smk import smk\n@smk("SMK-Z-01", status="partial")\ndef t(): ...\n',
        encoding="utf-8",
    )
    (tests / "test_b.py").write_text(
        'from tests._support.smk import smk\n@smk("SMK-Z-01")\ndef t(): ...\n',
        encoding="utf-8",
    )
    assert scan_markers(tests)["SMK-Z-01"] == "full"


def test_render_counts(tmp_path: Path):
    out = render({"SMK-A-01": "full", "SMK-A-02": "none"})
    assert "FULL 1 | PARTIAL 0 | NONE 1 | total 2" in out
    assert "| SMK-A-02 | NONE |" in out


def test_multi_id_marker(tmp_path: Path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text(
        'from tests._support.smk import smk\n@smk("SMK-X-01", "SMK-X-02")\ndef t(): ...\n',
        encoding="utf-8",
    )
    markers = scan_markers(tests)
    assert markers["SMK-X-01"] == "full"
    assert markers["SMK-X-02"] == "full"
