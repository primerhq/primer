"""The vendor manifest is a complete, byte-exact supply-chain inventory."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "ui" / "vendor"
MANIFEST = VENDOR / "MANIFEST.md"
_SHA256 = re.compile(r"`([0-9a-f]{64})`")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _entries() -> dict[str, tuple[str, str, str, str, str]]:
    entries: dict[str, tuple[str, str, str, str, str]] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        fields = [field.strip() for field in line.strip().strip("|").split("|")]
        if len(fields) != 6:
            continue
        path, origin, version, digest_field, license_name, date_added = fields
        digest = _SHA256.fullmatch(digest_field)
        if digest is None:
            continue
        assert path not in entries, f"duplicate vendor manifest entry: {path}"
        assert all((origin, version, license_name, date_added)), (
            f"vendor manifest metadata is incomplete: {path}"
        )
        assert _DATE.fullmatch(date_added), f"vendor manifest date is invalid: {path}"
        entries[path] = (
            origin,
            version,
            digest.group(1),
            license_name,
            date_added,
        )
    return entries


def test_manifest_lists_every_vendor_file_once() -> None:
    files = {
        path.relative_to(VENDOR).as_posix()
        for path in VENDOR.rglob("*")
        if path.is_file() and path != MANIFEST
    }
    listed = set(_entries())

    assert listed == files, (
        f"missing from manifest: {sorted(files - listed)}; "
        f"listed but absent: {sorted(listed - files)}"
    )


def test_manifest_hashes_match_current_vendor_bytes() -> None:
    for relative, (_, _, expected, _, _) in _entries().items():
        actual = hashlib.sha256((VENDOR / relative).read_bytes()).hexdigest()
        assert actual == expected, (
            f"{relative} changed without updating ui/vendor/MANIFEST.md: "
            f"expected {expected}, got {actual}"
        )
