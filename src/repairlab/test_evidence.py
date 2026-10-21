"""Read bounded per-run pytest evidence; never fall back to console counts."""
from collections import Counter
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import tempfile
import uuid


@contextmanager
def evidence_directory():
    with tempfile.TemporaryDirectory(prefix="repairlab-evidence-") as name:
        root = Path(name)
        root.chmod(0o755)
        plugin = root / "repairlab_pytest_evidence.py"
        shutil.copyfile(Path(__file__).with_name("pytest_evidence.py"), plugin)
        plugin.chmod(0o644)
        output = root / "output"
        output.mkdir(mode=0o777)
        output.chmod(0o777)  # The container's fixed non-root UID writes only here.
        yield root, output / "results.json", uuid.uuid4().hex


def read_evidence(path, run_id, exit_code):
    try:
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            raise ValueError("invalid report file")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["schema_version"] != 1 or data["run_id"] != run_id or data["exit_code"] != exit_code:
            raise ValueError("report identity or exit code mismatch")
        collected, phases = data["collected"], data["phases"]
        if not isinstance(collected, list) or len(collected) > 5000 or len(collected) != len(set(collected)):
            raise ValueError("invalid test manifest")
        if not all(isinstance(n, str) and 0 < len(n) <= 4000 for n in collected):
            raise ValueError("invalid node id")
        if not isinstance(phases, dict) or not set(phases).issubset(collected):
            raise ValueError("uncollected test report")
        for key in ("deselected", "collection_errors"):
            if not isinstance(data[key], list) or not all(isinstance(n, str) for n in data[key]):
                raise ValueError("invalid collection evidence")
        nodes = []
        for node in collected:
            steps = phases.get(node, {})
            if not isinstance(steps, dict) or not set(steps).issubset({"setup", "call", "teardown"}):
                raise ValueError("invalid phases")
            for step in steps.values():
                if step["outcome"] not in {"passed", "failed", "skipped"} or type(step["wasxfail"]) is not bool:
                    raise ValueError("invalid outcome")
            if any(s["outcome"] == "failed" for s in steps.values()):
                outcome = "failed" if steps.get("call", {}).get("outcome") == "failed" else "error"
            elif any(s["outcome"] == "skipped" for s in steps.values()):
                outcome = "xfailed" if any(s["wasxfail"] for s in steps.values()) else "skipped"
            elif set(steps) != {"setup", "call", "teardown"}:
                outcome = "not_run"
            else:
                outcome = "xpassed" if any(s["wasxfail"] for s in steps.values()) else "passed"
            nodes.append({"nodeid": node, "outcome": outcome})
        return {"valid": True, "schema_version": 1, "run_id": run_id, "nodes": nodes,
                "counts": dict(Counter(n["outcome"] for n in nodes)),
                "deselected": data["deselected"], "collection_errors": data["collection_errors"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"valid": False, "error": "missing_or_invalid_test_report", "nodes": [], "counts": {}}


def suite_policy(result, required=()):
    evidence = result.get("test_evidence") or {}
    counts = evidence.get("counts", {})
    outcomes = {n["nodeid"]: n["outcome"] for n in evidence.get("nodes", [])}
    missing = [node for node in required if node not in outcomes]
    not_passed = [node for node in required if node in outcomes and outcomes[node] != "passed"]
    execution_ok = result["exit_code"] == 0 and not any(
        result[k] for k in ("timed_out", "cancelled", "output_limited"))
    reason = "verified"
    if not execution_ok:
        reason = "execution_failed"
    elif not evidence.get("valid"):
        reason = "missing_or_invalid_test_report"
    elif missing or not_passed:
        reason = "required_tests_not_passed"
    elif evidence.get("collection_errors") or any(counts.get(k, 0) for k in ("failed", "error", "not_run", "xpassed")):
        reason = "incomplete_or_failed_test_execution"
    elif not counts.get("passed", 0):
        reason = "no_passing_verification_tests"
    return {"passed": reason == "verified", "reason": reason, "required_count": len(required),
            "missing": missing, "not_passed": not_passed}
