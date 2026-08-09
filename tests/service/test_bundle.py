"""validate_bundle: the publish-time gate (spec section 5)."""

import io
import tarfile

import pytest

from primer.service.bundle import BundleError, validate_bundle

GOOD_FN = (
    "@primer_tool(timeout_seconds=5)\n"
    "async def add(a: int, b: int) -> int:\n"
    "    return a + b\n"
)


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_minimal_static_bundle() -> None:
    vb = validate_bundle(_tar({"index.html": b"<h1>hi</h1>"}))
    assert vb.manifest.entry == "index.html"
    assert vb.files["index.html"] == b"<h1>hi</h1>"
    assert vb.functions == []


def test_functions_default_populated_and_registered() -> None:
    vb = validate_bundle(_tar({
        "index.html": b"x", "functions.py": GOOD_FN.encode()}))
    assert [f.name for f in vb.functions] == ["add"]
    assert vb.functions[0].timeout_seconds == 5.0
    assert vb.functions[0].source_file == "functions.py"
    assert vb.functions[0].schema_["properties"].keys() == {"a", "b"}


def test_manifest_unknown_key_rejected() -> None:
    with pytest.raises(BundleError):
        validate_bundle(_tar({
            "index.html": b"x", "service.yaml": b"bogus_key: 1\n"}))


def test_manifest_listed_functions_file_missing() -> None:
    with pytest.raises(BundleError) as ei:
        validate_bundle(_tar({
            "index.html": b"x",
            "service.yaml": b"functions:\n  - missing.py\n"}))
    assert "missing.py" in str(ei.value)


def test_syntax_error_carries_lineno() -> None:
    with pytest.raises(BundleError) as ei:
        validate_bundle(_tar({
            "index.html": b"x", "functions.py": b"def broken(:\n"}))
    assert ei.value.lineno is not None


def test_yielding_rejected() -> None:
    src = (
        "@primer_tool()\n"
        "async def ask(q: str) -> str:\n"
        "    return ask_user(q)\n\n"
        "@resumes(ask)\n"
        "def _(payload: dict, meta: dict) -> str:\n"
        "    return str(payload)\n"
    )
    with pytest.raises(BundleError):
        validate_bundle(_tar({"index.html": b"x", "functions.py": src.encode()}))


def test_path_escape_rejected() -> None:
    with pytest.raises(BundleError):
        validate_bundle(_tar({"../evil": b"x"}))


def test_too_many_files_rejected() -> None:
    files = {f"f{i}.txt": b"x" for i in range(201)}
    files["index.html"] = b"x"
    with pytest.raises(BundleError):
        validate_bundle(_tar(files))


def test_oversize_rejected() -> None:
    with pytest.raises(BundleError):
        validate_bundle(_tar({"index.html": b"0" * (10 * 1024 * 1024 + 1)}))


def test_symlink_rejected() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
        data = b"x"
        fi = tarfile.TarInfo("index.html")
        fi.size = len(data)
        tf.addfile(fi, io.BytesIO(data))
    with pytest.raises(BundleError):
        validate_bundle(buf.getvalue())


def test_duplicate_function_names_across_files_rejected() -> None:
    with pytest.raises(BundleError):
        validate_bundle(_tar({
            "index.html": b"x",
            "service.yaml": b"functions:\n  - a.py\n  - b.py\n",
            "a.py": GOOD_FN.encode(),
            "b.py": GOOD_FN.encode()}))


def test_not_a_tar_rejected() -> None:
    with pytest.raises(BundleError):
        validate_bundle(b"definitely not a tarball")
