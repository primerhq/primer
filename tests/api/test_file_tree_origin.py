from datetime import datetime, timezone

from primer.api.routers.workspaces import MountRequest, _decorate_origins
from primer.model.workspace import FileEntry
from primer.workspace.mount_manifest import MountEntry, MountManifest


def _entry(path, kind="dir"):
    return FileEntry(path=path, kind=kind, size_bytes=0, modified_at=datetime.now(timezone.utc))


def test_fileentry_origin_defaults_none():
    assert _entry("a").origin is None


def test_decorate_marks_mount_root_dirs():
    m = MountManifest(mounts=[MountEntry(
        mount_id="wsmnt-1", collection_id="c", collection_name="C", dest="slo",
        mounted_at=datetime.now(timezone.utc), base=[])])
    entries = [_entry("slo"), _entry("other"), _entry("slo/x.md", kind="file")]
    out = _decorate_origins(entries, m)
    by = {e.path: e.origin for e in out}
    assert by["slo"] == "collection"
    assert by["other"] is None
    assert by["slo/x.md"] is None


def test_mount_request_has_expected_fields():
    req = MountRequest(collection_id="c1")
    assert req.collection_id == "c1"
    assert req.dest is None
