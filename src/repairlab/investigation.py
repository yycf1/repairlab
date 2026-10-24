"""Candidate evidence and immutable, generated pytest probes outside the editable workspace.

Generated tests are model hypotheses, not independent acceptance criteria. They run
under the same trusted-local/Docker policy as existing tests; AST checks are NOT a sandbox.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from repairlab.feedback import test_feedback
from repairlab.repository import Repository, copy_repository


def fingerprint(root, profile):
    repo = Repository(root, profile)
    return {name: hashlib.sha256(repo.path(name).read_bytes()).hexdigest() for name in repo.files()}


def execute_probe(source, destination, code, candidate_id, runner, profile, limits, cancel, run_id, revision, digest):
    copy_repository(source, destination)
    filename = f"test_repairlab_probe_{candidate_id}.py"
    path = destination / filename
    if path.exists():
        raise ValueError("Generated probe filename collides with a repository file")
    path.write_text(code, encoding="utf-8", newline="\n")
    before = fingerprint(destination, profile)
    result = runner.run_tests(destination, [filename], limits.command_seconds, cancel=cancel, image=profile.image)
    evidence = test_feedback(result, destination, run_id, revision, digest)
    if fingerprint(destination, profile) != before:
        evidence["category"] = "probe_workspace_changed"
    elif evidence["category"] == "test_failure":
        # Runtime imports, fixtures, accidental NameErrors, etc. must not confirm a bug.
        log = result.get("stdout", "") + result.get("stderr", "")
        if "AssertionError" not in log or not any(filename in node for node in evidence["failed_nodes"]):
            evidence["category"] = "reproducer_error"
    elif evidence["category"] in {"ok", "insufficient_test_evidence"} and not evidence["counts"].get("passed"):
        evidence["category"] = "reproducer_no_assertion_result"
    return {"result": result, "evidence": evidence}


class Investigation:
    def __init__(self, env, baseline=None):
        self.env = env
        self.baseline = Path(baseline) if baseline else None
        self.root = env.repo.root.parent / "investigation"
        self.artifacts = env.repo.root.parent / "artifacts"

    def candidate(self, candidate_id):
        if self.env.state.task_kind == "repair":
            return self.repair_candidate(candidate_id)
        for candidate in self.env.state.candidates:
            if candidate["id"] == candidate_id:
                return candidate
        raise ValueError("Unknown candidate_id")

    def repair_candidate(self, candidate_id, create=False):
        """Repair mode derives one implicit candidate from the issue text; resolve closes it,
        and the next write_reproducer opens a fresh numbered one."""
        issues = [c for c in self.env.state.candidates if c["id"].startswith("issue")]
        if candidate_id:
            for candidate in issues:
                if candidate["id"] == candidate_id:
                    return candidate
            raise ValueError("Unknown candidate_id")
        open_candidates = [c for c in issues if c["status"] not in {"rejected", "inconclusive"}]
        if open_candidates:
            return open_candidates[0]
        if not create:
            raise ValueError("No open issue candidate; call write_reproducer first")
        candidate = {"id": "issue" if not issues else f"issue-{len(issues) + 1}",
                     "summary": "问题描述所指的缺陷", "expectation": "", "basis": "任务问题描述",
                     "basis_paths": [], "paths": [], "status": "suspected", "resolution": "",
                     "probe_history": []}
        self.env.state.candidates = (self.env.state.candidates + [candidate])[-8:]
        return candidate

    def record(self, action):
        env = self.env
        if len(env.state.candidates) >= 8:
            raise ValueError("Candidate limit reached (8); investigate the existing evidence")
        if env.state.revision != 0:
            raise ValueError("Discover candidates before editing; start another task for a new investigation")
        paths = [env.canonical(p) for p in action.paths]
        basis_paths = [env.canonical(p) for p in action.basis_paths]
        if any(p not in env.state.inspected_files for p in paths + basis_paths):
            raise ValueError("Read source and behavioral basis files before citing them")
        if not any(env.repo.editable(p) for p in paths):
            raise ValueError("Candidate must reference an editable source file")
        candidate = {"id": f"c{len(env.state.candidates) + 1}", "status": "suspected", "paths": paths,
                     "summary": action.summary, "expectation": action.expectation, "basis": action.basis,
                     "basis_paths": basis_paths, "basis_review": "model_interpretation_not_independent_oracle",
                     "revision": env.state.revision, "probe_history": []}
        env.state.candidates.append(candidate)
        env.state.phase = "investigating"
        return {"candidate": candidate, "next": "Verify the expectation, then write and run a minimal reproducer; do not assume the hypothesis is true."}

    def resolve(self, action):
        candidate = self.candidate(action.candidate_id)
        if self.env.state.revision:
            raise ValueError("Cannot discard a candidate after editing; finish unresolved or start a new task")
        candidate.update(status=action.disposition, resolution=action.summary)
        return {"candidate": candidate}

    def write(self, action):
        candidate = self.repair_candidate(action.candidate_id, create=True) \
            if self.env.state.task_kind == "repair" else self.candidate(action.candidate_id)
        if candidate["status"] in {"rejected", "inconclusive"}:
            raise ValueError("This candidate is closed")
        if candidate.get("code_sha256"):
            raise ValueError("Reproducer is immutable; reject a flawed candidate and record a new one")
        if self.env.state.revision:
            raise ValueError("Write the reproducer before editing source")
        tree = ast.parse(action.code)
        tests = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")]
        if not tests or not any(isinstance(n, ast.Assert) and not isinstance(n.test, ast.Constant)
                                for test in tests for n in ast.walk(test)):
            raise ValueError("Reproducer must contain a test_ function with a nonconstant assertion of repository behavior")
        self.artifacts.mkdir(parents=True, exist_ok=True)
        path = self.artifacts / f"reproducer-{candidate['id']}.py"
        code = action.code.rstrip() + "\n"
        path.write_text(code, encoding="utf-8", newline="\n")
        candidate.update(code_sha256=hashlib.sha256(code.encode()).hexdigest(), artifact=path.name)
        if self.baseline is None:
            self.baseline = self.root / "baseline"
            copy_repository(self.env.repo.root, self.baseline)
        return {"artifact": path.name, "code_sha256": candidate["code_sha256"],
                "note": "Generated hypothesis test, not independent acceptance. Original tests remain unchanged."}

    def code(self, candidate):
        code = (self.artifacts / candidate["artifact"]).read_text(encoding="utf-8")
        if hashlib.sha256(code.encode()).hexdigest() != candidate["code_sha256"]:
            raise ValueError("Reproducer changed after being recorded")
        return code

    def run(self, action):
        env = self.env
        candidate = self.candidate(action.candidate_id)
        if candidate["status"] in {"rejected", "inconclusive"}:
            raise ValueError("This candidate is closed")
        if not candidate.get("code_sha256"):
            raise ValueError("Write a reproducer first")
        if env.state.baseline_test is None:
            raise ValueError("Run the original development tests before the probe")
        if env.test_runs >= env.limits.max_test_runs:
            raise ValueError("Test-run budget exhausted; finish with evidence collected so far")
        baseline = candidate.get("baseline_probe") is None
        if baseline and env.state.revision:
            raise ValueError("First reproduce on unchanged source before editing")
        env.test_runs += 1
        digest, revision = env.digest(), env.state.revision
        source = self.baseline if baseline else env.repo.root
        output = execute_probe(source, self.root / f"run-{env.test_runs}", self.code(candidate), candidate["id"],
                               env.runner, env.repo.profile, env.limits, env.cancel, env.test_runs, revision, digest)
        evidence = output["evidence"]
        if env.digest() != digest:
            evidence["category"] = "workspace_changed_during_test"
        candidate["probe_history"].append(evidence)
        candidate["latest_probe"] = evidence
        if baseline:
            candidate["baseline_probe"] = evidence
            if evidence["category"] == "test_failure":
                candidate["status"] = "confirmed"
                env.state.active_candidate = candidate["id"]
            # A passing probe does not prove absence: leave it for an explicit resolution.
        env.state.phase = "investigating" if not env.state.changed_files else "testing"
        return {**output, "candidate_id": candidate["id"], "status": candidate["status"],
                "note": "Confirmed means an assertion mismatch was reproduced against a cited, model-interpreted expectation; review the expectation itself."}

    def confirmed_probes(self):
        return [{"id": c["id"], "code": self.code(c), "code_sha256": c["code_sha256"]}
                for c in self.env.state.candidates if c["status"] == "confirmed"]
