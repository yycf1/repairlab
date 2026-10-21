from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from repairlab.test_evidence import suite_policy
from repairlab.repository import copy_repository, diff_repositories
from repairlab.investigation import execute_probe


class Verifier:
    def __init__(self, runner, limits):
        self.runner, self.limits = runner, limits

    def verify(self, baseline: Path, workspace: Path, destination: Path, profile, *, cancel=None, probes=()):
        patch, changed, violations = diff_repositories(baseline, workspace, profile)
        artifacts = destination.parent / "artifacts"
        artifacts.mkdir(exist_ok=True)
        patch_path = artifacts / "patch.diff"
        patch_path.write_text(patch, encoding="utf-8", newline="\n")
        result = {"passed": False, "changed_files": changed, "violations": violations,
                  "patch": patch, "acceptance": None, "regression": None}
        if violations or not changed:
            result["reason"] = "protected_file_changed" if violations else "empty_patch"
            return result
        copy_repository(baseline, destination)
        apply = subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=destination,
                               capture_output=True, timeout=15)
        if apply.returncode:
            result.update(reason="patch_not_applicable", error=apply.stderr.decode("utf-8", errors="replace")[:2000])
            return result
        subprocess.run(["git", "apply", str(patch_path)], cwd=destination, check=True, capture_output=True, timeout=15)
        # No developer-workspace artifacts/caches can contaminate final verification.
        if profile.acceptance:
            acceptance_source = Path(profile.acceptance).resolve(strict=True)
            if acceptance_source.is_relative_to(workspace.resolve()):
                raise ValueError("Acceptance cases must live outside the agent workspace")
            shutil.copytree(acceptance_source, destination / "_acceptance")
            acceptance_targets = ["_acceptance"]
        else:
            acceptance_targets = profile.development_tests
        result["acceptance"] = self.runner.run_tests(destination, acceptance_targets,
            self.limits.command_seconds, cancel=cancel, image=profile.image)
        result["regression"] = self.runner.run_tests(destination, profile.regression_tests,
            self.limits.command_seconds, cancel=cancel, image=profile.image)
        for name in ("acceptance", "regression"):
            check = result[name]
            check["counts"] = (check.get("test_evidence") or {}).get("counts", {})
            check["policy"] = suite_policy(check, getattr(profile, f"required_{name}_tests"))
        failures = [result[name]["policy"]["reason"] for name in ("acceptance", "regression")
                    if not result[name]["policy"]["passed"]]
        result["passed"] = not failures
        result["reason"] = failures[0] if failures else "verified"
        result["generated_reproducers"] = []
        for probe in probes:
            checked = execute_probe(destination, destination.parent / "verification-probes" / probe["id"],
                                    probe["code"], probe["id"], self.runner, profile, self.limits, cancel, 0, 0, "final-patch")
            result["generated_reproducers"].append({"id": probe["id"], "code_sha256": probe["code_sha256"],
                                                  **checked, "independent_oracle": False})
        if any(p["evidence"]["category"] != "ok" for p in result["generated_reproducers"]):
            result.update(passed=False, reason="generated_reproducer_failed")
        result["independent_acceptance"] = bool(profile.acceptance)
        return result
