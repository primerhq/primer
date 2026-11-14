"""Refresh the committed timing baseline from a successful CI JUnit report."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree


def from_junit(path: Path | list[Path], root: Path, source: str) -> dict:
    paths = [path] if isinstance(path, Path) else sorted(path)
    modules = {".".join(p.relative_to(root).with_suffix("").parts): p.relative_to(root).as_posix() for p in (root / "tests").rglob("*.py")}
    totals = defaultdict(lambda: {"seconds": 0.0, "tests": 0})
    cases = [case for p in paths for case in ElementTree.parse(p).getroot().findall(".//testcase")]
    if not cases or any(case.find("failure") is not None or case.find("error") is not None for case in cases):
        raise ValueError("timing baseline requires a nonempty passing report")
    for case in cases:
        module = case.get("classname", "")
        if not module and case.find("skipped") is not None:
            continue  # Collection-level import skips have no collected item.
        while module not in modules and module:
            module = module.rpartition(".")[0]
        if not module:
            raise ValueError(f"unknown JUnit module: {case.attrib['classname']}")
        entry = totals[modules[module]]
        entry["seconds"] += float(case.attrib["time"])
        entry["tests"] += 1
    for entry in totals.values():
        entry["seconds"] = round(entry["seconds"], 3)
    return {"source": source, "junit_sha256": hashlib.sha256(b"".join(p.read_bytes() for p in paths)).hexdigest(), "modules": dict(sorted(totals.items()))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path, nargs="+")
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    baseline = from_junit(args.junit, Path.cwd(), args.source)
    # One measured module per line keeps a refreshed baseline reviewable.
    rows = [f"    {json.dumps(path)}: {json.dumps(value)}" for path, value in baseline.pop("modules").items()]
    prefix = json.dumps(baseline, indent=2)[:-2]
    Path(__file__).with_name("ci_module_timings.json").write_text(prefix + ',\n  "modules": {\n' + ",\n".join(rows) + "\n  }\n}\n")
