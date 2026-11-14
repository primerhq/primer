"""Measured whole-module CI partition, applied AFTER normal pytest selection.

Each xdist worker independently derives the same partition from its actual
collection. Reports let the final gate prove a disjoint, complete union without
collecting or executing the suite again in a planning job.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import defaultdict
from pathlib import Path

import pytest

WEIGHTS = Path(__file__).with_name("ci_module_timings.json")
SHARDS = 4


def digest(values) -> str:
    return hashlib.sha256(json.dumps(sorted(values), separators=(",", ":")).encode()).hexdigest()


def partition(nodeids: list[str], timings: dict, count: int) -> tuple[dict[str, int], list[float]]:
    """Longest estimated module first; stable path/index tie breaks.

    New modules always participate, at an estimated minimum of ten seconds.
    Growing modules scale their previous measured cost by collected item count.
    Neither zero-duration skipped modules nor a missing timing entry is omitted.
    """
    if count < 1 or len(nodeids) != len(set(nodeids)):
        raise ValueError("invalid shard count or duplicate collected node IDs")
    modules = defaultdict(list)
    for nodeid in nodeids:
        modules[nodeid.split("::", 1)[0]].append(nodeid)
    costs = {}
    for path, items in modules.items():
        known = timings.get(path)
        cost = (float(known["seconds"]) * max(1, len(items) / known["tests"])) if known else max(10.0, len(items) * 0.05)
        if not math.isfinite(cost) or cost < 0:
            raise ValueError(f"invalid module timing: {path}")
        costs[path] = max(cost, 0.001)
    loads = [0.0] * count
    assigned = {}
    for path in sorted(modules, key=lambda p: (-costs[p], p)):
        index = min(range(count), key=lambda i: (loads[i], i))
        assigned[path] = index
        loads[index] += costs[path]
    return assigned, loads


def validate_reports(reports: list[dict], *, count: int, source_sha: str, weights_sha: str) -> None:
    if len(reports) != count or {r["shard"] for r in reports} != set(range(count)):
        raise ValueError("missing, duplicate or unexpected shard report")
    selected = []
    inventories = set()
    owners = {}
    for report in reports:
        if not report["complete"] or report["exitstatus"] != 0:
            raise ValueError("failed or incomplete shard")
        if report["shards"] != count or report["source_sha"] != source_sha or report["weights_sha256"] != weights_sha:
            raise ValueError("shard source, count or timing baseline mismatch")
        inventories.add((report["inventory_count"], report["inventory_sha256"]))
        ids = report["selected"]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("empty shard or duplicate node IDs")
        for module in {nodeid.split("::", 1)[0] for nodeid in ids}:
            if module in owners:
                raise ValueError(f"module split across shards: {module}")
            owners[module] = report["shard"]
        selected.extend(ids)
    if len(inventories) != 1 or len(selected) != len(set(selected)):
        raise ValueError("collection drift or overlapping shards")
    if (len(selected), digest(selected)) != next(iter(inventories)):
        raise ValueError("shard union does not equal the full selected collection")


def pytest_addoption(parser):
    group = parser.getgroup("CI module shards")
    group.addoption("--ci-shard", type=int, default=None)
    group.addoption("--ci-shards", type=int, default=SHARDS)
    group.addoption("--ci-shard-report", default="ci-results/unit/report.json")


def pytest_configure(config):
    index = config.getoption("ci_shard")
    if index is not None:
        if not 0 <= index < config.getoption("ci_shards"):
            raise pytest.UsageError("ci-shard must be within ci-shards")
        config.pluginmanager.register(ShardPlugin(config), "ci-module-partition")


class ShardPlugin:
    def __init__(self, config):
        self.config = config
        self.info = None
        self.workers = []
        self.collections = []

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, config, items):
        result = yield  # Includes built-in -m/-k and any repository selection.
        baseline = json.loads(WEIGHTS.read_text())
        ids = [item.nodeid for item in items]
        index, count = config.getoption("ci_shard"), config.getoption("ci_shards")
        assigned, loads = partition(ids, baseline["modules"], count)
        kept, removed = [], []
        for item in items:
            (kept if assigned[item.nodeid.split("::", 1)[0]] == index else removed).append(item)
        self.info = {
            "shard": index, "shards": count,
            "source_sha": os.environ.get("GITHUB_SHA", "local"),
            "weights_sha256": hashlib.sha256(WEIGHTS.read_bytes()).hexdigest(),
            "inventory_count": len(ids), "inventory_sha256": digest(ids),
            "selected": sorted(item.nodeid for item in kept),
            "estimated_worker_seconds": loads[index],
        }
        # Set during collection: xdist sends workeroutput from its own
        # sessionfinish wrapper, before an outer wrapper would finish.
        if hasattr(config, "workeroutput"):
            config.workeroutput["ci_module_shard"] = self.info
        items[:] = kept
        config.hook.pytest_deselected(items=removed)
        return result

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        self.collections.append(sorted(ids))

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        self.workers.append(node.workeroutput.get("ci_module_shard") if not error else None)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session, exitstatus):
        result = yield  # Honor other hooks that change the final exit status.
        if hasattr(self.config, "workerinput"):
            return result
        infos = self.workers or [self.info]
        info = infos[0]
        valid = bool(info and info["selected"] and all(i == info for i in infos))
        valid = valid and all(ids == info["selected"] for ids in self.collections)
        if not valid and session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        report = dict(info or {})
        report.update(complete=valid and session.exitstatus == 0, exitstatus=int(session.exitstatus))
        path = Path(self.config.getoption("ci_shard_report"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, sort_keys=True) + "\n")
        return result
