"""Run fixed scripted scenarios and export portable, auditable evidence (no model calls)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile

from repairlab.models import StartRequest
from repairlab.service import Service
from repairlab.settings import ROOT, Settings


def fixture_digest(fixture):
    root = ROOT / "examples" / fixture
    manifest = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def evaluate(backend):
    records = []
    with tempfile.TemporaryDirectory(prefix="repairlab-evaluation-") as temporary:
        previous = os.environ.get("REPAIRLAB_DATA_DIR")
        os.environ["REPAIRLAB_DATA_DIR"] = str(Path(temporary) / "data")
        service = None
        try:
            service = Service(Settings())
            for fixture in ("boundary-discount", "csv-tags", "pagination"):
                for strategy in ("single_shot", "feedback"):
                    expected = "failed" if (fixture, strategy) == ("csv-tags", "single_shot") else "passed"
                    task = service.start(StartRequest(fixture=fixture, strategy=strategy, mode="demo",
                                         backend=backend, allow_local=backend == "local"), background=False)
                    verification = task.get("verification") or {}
                    records.append({"fixture": fixture, "fixture_sha256": fixture_digest(fixture),
                        "strategy": strategy, "expected_status": expected, "status": task["status"],
                        "matches_expectation": task["status"] == expected,
                        "scripted_calls": task.get("model_calls"), "tokens": task.get("tokens"),
                        "seconds": task.get("seconds"), "reason": verification.get("reason"),
                        "patch_sha256": hashlib.sha256(verification.get("patch", "").encode()).hexdigest(),
                        "suites": {name: {"counts": verification[name]["counts"],
                                         "policy": verification[name]["policy"],
                                         "nodes": verification[name]["test_evidence"]["nodes"]}
                                   for name in ("acceptance", "regression") if verification.get(name)}})
        finally:
            if service:
                service.close()
            if previous is None:
                os.environ.pop("REPAIRLAB_DATA_DIR", None)
            else:
                os.environ["REPAIRLAB_DATA_DIR"] = previous
    return {"schema_version": 1, "kind": "scripted_mechanism_demo", "real_model_evaluation": False,
            "scope": "3 bundled fixtures x 2 scripted strategies; no model success-rate claim",
            "backend": backend, "python": platform.python_version(), "platform": platform.system(),
            "all_expectations_met": all(r["matches_expectation"] for r in records), "results": records}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("local", "docker"), default="docker")
    parser.add_argument("--allow-local", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.backend == "local" and not args.allow_local:
        parser.error("Bundled fixture execution on the host requires --allow-local")
    report = evaluate(args.backend)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"all_expectations_met": report["all_expectations_met"], "scenarios": len(report["results"])}))
    raise SystemExit(0 if report["all_expectations_met"] else 1)
