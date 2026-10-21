"""Loaded explicitly by the runner in a separate pytest process.

This records test execution, not a sandbox against malicious Python code.
"""
import json
import os
from pathlib import Path

_phases = {}
_collection_errors = []
_deselected = []


def pytest_runtest_logreport(report):
    _phases.setdefault(report.nodeid, {})[report.when] = {
        "outcome": report.outcome, "wasxfail": hasattr(report, "wasxfail"),
    }


def pytest_collectreport(report):
    if report.failed:
        _collection_errors.append(report.nodeid)


def pytest_deselected(items):
    _deselected.extend(item.nodeid for item in items)


def pytest_sessionfinish(session, exitstatus):
    data = {
        "schema_version": 1, "run_id": os.environ["REPAIRLAB_TEST_RUN_ID"],
        "exit_code": int(exitstatus), "collected": [item.nodeid for item in session.items],
        "deselected": _deselected, "collection_errors": _collection_errors, "phases": _phases,
    }
    target = Path(os.environ["REPAIRLAB_TEST_REPORT"])
    target.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
