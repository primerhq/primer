"""Fail-closed gate for all unit partitions plus the one docs coverage run."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import coverage

from scripts.tests.ci_shards import SHARDS, WEIGHTS, validate_reports


def validate_jobs(needs: dict) -> None:
    if set(needs) != {"checks", "unit"} or any(job.get("result") != "success" for job in needs.values()):
        raise ValueError("lint/docs and every unit matrix job must succeed")


def verify_inputs(root: Path, *, source_sha: str, count: int = SHARDS) -> list[Path]:
    names = ["ci-coverage-docs", *[f"ci-coverage-unit-{i}" for i in range(count)]]
    if {p.name for p in root.iterdir()} != set(names):
        raise ValueError("missing or unexpected coverage artifact")
    reports = [json.loads((root / name / "report.json").read_text()) for name in names[1:]]
    validate_reports(reports, count=count, source_sha=source_sha, weights_sha=hashlib.sha256(WEIGHTS.read_bytes()).hexdigest())
    paths = [root / name / "coverage" for name in names]
    for path in paths:
        if not path.is_file():
            raise ValueError(f"missing coverage data: {path}")
        data = coverage.CoverageData(basename=str(path))
        data.read()  # Corrupt, empty or XML-only artifacts cannot pass.
        if not data.measured_files():
            raise ValueError(f"empty coverage data: {path}")
    return paths


def combine(paths: list[Path], *, output: Path, fail_under: float = 80) -> float:
    """Combine RAW measurements, never averages of shard percentages."""
    cov = coverage.Coverage(data_file=str(output))
    with tempfile.TemporaryDirectory() as temp:
        for index, path in enumerate(paths):
            shutil.copyfile(path, Path(temp) / f"{output.name}.{index}")
        cov.combine(data_paths=[temp], strict=True, keep=True)
    cov.save()
    total = cov.report(show_missing=True)
    cov.xml_report(outfile=str(output.with_name("coverage.xml")))
    # Match coverage/pytest-cov's configured rounding at the original gate.
    if (fail_under == 100 and total != 100) or round(total, cov.get_option("report:precision")) < fail_under:
        raise ValueError(f"combined coverage {total:.2f}% is below {fail_under}%")
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--needs", required=True)
    args = parser.parse_args()
    validate_jobs(json.loads(args.needs))
    paths = verify_inputs(args.artifacts, source_sha=args.source_sha)
    combine(paths, output=Path(".coverage"))


if __name__ == "__main__":
    main()
