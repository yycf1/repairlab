from __future__ import annotations

import difflib
import hashlib
from collections import Counter
from pydantic import ValidationError

from repairlab.feedback import test_feedback
from repairlab.investigation import Investigation
from repairlab.models import Action, Observation
from repairlab.repository import Repository
from repairlab.workflow import WorkflowState


class PolicyError(ValueError):
    """Deterministic gate rejection with a stable short code for aggregate control statistics."""

    def __init__(self, message, code="policy_error"):
        super().__init__(message)
        self.code = code


def source_digest(repo):
    """Content fingerprint over editable files; the revision-bound evidence key."""
    fingerprint = hashlib.sha256()
    for name in repo.files():
        if repo.editable(name):
            fingerprint.update(name.encode() + b"\0" + repo.path(name).read_bytes() + b"\0")
    return fingerprint.hexdigest()


class ToolEnvironment:
    def __init__(self, repo: Repository, runner, limits, emit, cancel, *, task_kind="repair", baseline=None):
        self.repo, self.runner, self.limits = repo, runner, limits
        self.emit, self.cancel = emit, cancel
        self.baseline = baseline
        self.state = WorkflowState(task_kind=task_kind)
        self.investigation = Investigation(self, baseline)
        self.test_runs = 0
        self.control_stats = Counter()
        self.repeats = Counter()
        self.last_test = None
        self.original = {name: self.repo.path(name).read_text(encoding="utf-8")
                         for name in self.repo.files() if self.repo.editable(name)}
        self.initial_digest = self.digest()
        self.undo = []

    @property
    def plan(self):
        return self.state.plan

    def digest(self):
        return source_digest(self.repo)

    def export_extra_state(self):
        """Environment bits that live outside WorkflowState and must survive a restart."""
        return {"test_runs": self.test_runs, "initial_digest": self.initial_digest,
                "undo": [[name, text] for name, text in self.undo]}

    def restore_extra_state(self, data):
        self.test_runs = data["test_runs"]
        self.initial_digest = data["initial_digest"]
        self.undo = [(name, text) for name, text in data["undo"]]
        # Repeated-action counting restarts: replayed probes after a restart are not evidence of a loop.
        self.repeats.clear()
        if self.baseline is not None:
            untouched = Repository(self.baseline, self.repo.profile)
            self.original = {name: untouched.path(name).read_text(encoding="utf-8")
                             for name in self.repo.files() if self.repo.editable(name)}

    def source_changed(self):
        self.state.revision += 1
        self.state.changed_files = [name for name, text in self.original.items()
                                   if self.repo.path(name).read_text(encoding="utf-8") != text]
        self.state.phase = "testing"

    def canonical(self, name):
        return self.repo.path(name).relative_to(self.repo.root).as_posix()

    def before_edit(self, name):
        if not self.repo.editable(name):
            raise PolicyError("Only allowed source files may be changed; tests/configuration are protected",
                              code="protected_file")
        if not self.plan:
            raise PolicyError("missing_plan: use update_plan before editing", code="missing_plan")
        if self.state.baseline_test is None:
            raise PolicyError("baseline_not_run: run tests on the unchanged source before editing",
                              code="baseline_not_run")
        if self.state.baseline_test["category"] not in {"ok", "test_failure", "code_error"}:
            raise PolicyError("baseline_unavailable: inspect the collection/environment failure, then finish if blocked",
                              code="baseline_unavailable")
        if (self.state.task_kind == "repair" and self.state.baseline_test["category"] == "ok"
                and not any(c["id"].startswith("issue") and c["status"] == "confirmed" for c in self.state.candidates)):
            raise PolicyError("no_failing_evidence: baseline tests pass; confirm the defect with "
                              "write_reproducer + run_reproducer before editing", code="no_failing_evidence")
        if name not in self.state.inspected_files:
            raise PolicyError("source_not_read: use read_file on the target source first", code="source_not_read")
        finding = self.state.hypothesis
        if not finding or finding["revision"] != self.state.revision or name not in finding["paths"]:
            raise PolicyError("missing_current_hypothesis: record_finding for this source revision and target file",
                              code="missing_current_hypothesis")
        if self.state.task_kind == "inspect":
            candidate = self.investigation.candidate(finding.get("candidate_id", ""))
            if candidate["status"] != "confirmed" or name not in candidate["paths"]:
                raise PolicyError("candidate_not_confirmed: link a reproduced candidate covering this source file",
                                  code="candidate_not_confirmed")

    def execute(self, raw, cwd=""):
        try:
            action = Action.model_validate(raw)
            signature = action.model_dump_json() + self.digest()
            self.repeats[signature] += 1
            if self.repeats[signature] >= 4:
                raise ValueError("Repeated identical action without a source change; choose another action")
            if self.cancel.is_set():
                observation = Observation(ok=False, category="cancelled", error="Task cancelled")
            else:
                observation = self.dispatch(action)
        except PolicyError as exc:
            observation = Observation(ok=False, category="policy_error", error=str(exc))
            self.control_stats[exc.code] += 1
        except (ValueError, OSError, SyntaxError, ValidationError) as exc:
            observation = Observation(ok=False, category="tool_error", error=str(exc)[:1000])
            self.control_stats["tool_error"] += 1
        if observation.category in {"policy_error", "tool_error"}:
            self.state.consecutive_action_errors += 1
        else:
            self.state.consecutive_action_errors = 0
        snapshot = self.state.snapshot(self.digest())
        snapshot["control_stats"] = dict(self.control_stats)
        self.emit("workflow_state", snapshot)
        return observation.model_dump()

    def dispatch(self, action):
        tool = action.tool
        if tool == "list_files":
            data = {"files": self.repo.files()}
        elif tool == "search":
            data = self.repo.search(action.query)
        elif tool == "read_file":
            data = self.repo.read(action.path, action.start_line, action.end_line)
            name = self.canonical(action.path)
            if name not in self.state.inspected_files:
                self.state.inspected_files = (self.state.inspected_files + [name])[-50:]
        elif tool == "symbols":
            data = self.repo.symbols(action.path)
        elif tool == "references":
            data = self.repo.references(action.symbol.strip())
        elif tool == "outline":
            data = self.repo.outline()
        elif tool == "update_plan":
            self.state.plan = action.plan
            self.state.plan_history = (self.state.plan_history + [{"items": action.plan, "revision": self.state.revision,
                                      "reason": action.summary}])[-20:]
            self.emit("plan", {"items": self.plan, "reason": action.summary})
            data = {"plan": self.plan}
        elif tool == "record_finding":
            paths = [self.canonical(p) for p in action.paths]
            if any(p not in self.state.inspected_files for p in paths):
                raise PolicyError("Findings must reference files already inspected with read_file",
                                  code="finding_not_read")
            finding = {"summary": action.summary, "paths": paths, "revision": self.state.revision,
                       "test_run_id": (self.state.latest_test or {}).get("run_id"), "candidate_id": action.candidate_id}
            if self.state.task_kind == "inspect":
                candidate = self.investigation.candidate(action.candidate_id)
                if candidate["status"] != "confirmed" or any(p not in candidate["paths"] for p in paths):
                    raise PolicyError("Link the finding to a confirmed candidate covering these paths",
                                      code="finding_not_linked")
                self.state.active_candidate = candidate["id"]
            self.state.hypothesis = finding
            self.state.hypotheses = (self.state.hypotheses + [finding])[-20:]
            data = {"hypothesis": finding, "note": "This is a model hypothesis, not an established root cause."}
        elif tool == "edit_file":
            name = self.canonical(action.path)
            self.before_edit(name)
            before = self.repo.path(name).read_text(encoding="utf-8")
            data = self.repo.edit(name, action.old, action.new)
            if data["changed"]:
                self.undo = (self.undo + [(name, before)])[-4:]
                self.source_changed()
        elif tool == "rollback_edit":
            if not self.undo:
                raise PolicyError("There is no tool edit to undo", code="rollback_empty")
            name, content = self.undo.pop()
            self.repo.path(name).write_text(content, encoding="utf-8", newline="\n")
            self.source_changed()
            data = {"restored": name, "revision": self.state.revision, "note": "Run tests again; earlier evidence is stale."}
        elif tool == "get_patch":
            patch = []
            for name in self.state.changed_files:
                patch.extend(difflib.unified_diff(self.original[name].splitlines(keepends=True),
                    self.repo.path(name).read_text(encoding="utf-8").splitlines(keepends=True),
                    fromfile=f"a/{name}", tofile=f"b/{name}"))
            text = "".join(patch)
            data = {"patch": text[:12000], "truncated": len(text) > 12000, "revision": self.state.revision}
        elif tool == "run_tests":
            return self.run_tests(action.targets)
        elif tool == "record_candidate":
            if self.state.task_kind == "repair":
                raise PolicyError("record_candidate is for inspection mode; confirm the issue defect with write_reproducer",
                                  code="candidate_unavailable")
            data = self.investigation.record(action)
        elif tool == "resolve_candidate":
            data = self.investigation.resolve(action)
        elif tool == "write_reproducer":
            data = self.investigation.write(action)
        elif tool == "run_reproducer":
            data = self.investigation.run(action)
            category = data["evidence"]["category"]
            return Observation(ok=category == "ok", category=category, data=data)
        elif tool == "submit":
            blockers = self.state.submission_blockers(self.digest())
            if blockers:
                raise PolicyError("Submission blocked: " + "; ".join(blockers), code="submission_blocked")
            self.state.phase = "submitted"
            data = {"submitted": True, "summary": action.summary}
        else:
            if action.reason == "investigation_complete":
                if self.state.task_kind != "inspect" or self.state.changed_files:
                    raise PolicyError("Investigation completion requires inspection mode with no source changes",
                                      code="finish_requires_inspection")
                if not self.state.plan or self.state.baseline_test is None or not self.state.inspected_files:
                    raise PolicyError("Inspect files, record a plan and run baseline tests before completing",
                                      code="finish_missing_evidence")
                if self.state.baseline_test["category"] != "ok" and not self.state.candidates:
                    raise PolicyError("Baseline has failures; investigate candidates or finish with an unresolved/environment reason",
                                      code="finish_baseline_failures")
                if any(c["status"] == "suspected" for c in self.state.candidates):
                    raise PolicyError("Resolve suspected candidates as rejected or inconclusive before completing",
                                      code="finish_suspected_candidates")
                self.state.phase = "reviewed"
            else:
                self.state.phase = "blocked"
            data = {"finished": True, "reason": action.reason, "summary": action.summary}
        return Observation(ok=True, data=data)

    def run_tests(self, targets=None):
        if self.test_runs >= self.limits.max_test_runs:
            raise PolicyError("Development test-run budget exhausted; finish with the remaining blocker",
                              code="test_budget_exhausted")
        self.test_runs += 1
        before = self.digest()
        selected = self.select_targets(targets or [])
        data = self.runner.run_tests(self.repo.root, selected or self.repo.profile.development_tests,
            self.limits.command_seconds, cancel=self.cancel, image=self.repo.profile.image)
        self.last_test = data
        evidence = test_feedback(data, self.repo.root, self.test_runs, self.state.revision, before)
        evidence["scope"] = "partial" if selected else "all"
        if selected:
            evidence["targets"] = selected
        if self.digest() != before:
            self.source_changed()
            evidence.update(category="workspace_changed_during_test", hint="Source changed during testing; restart from a trusted baseline.")
        if before == self.initial_digest and self.state.revision == 0:
            self.state.baseline_test = evidence
        self.state.latest_test = evidence
        category = evidence["category"]
        self.state.phase = ("ready_to_submit" if category == "ok" and self.state.changed_files else
                            "repairing" if category in {"ok", "test_failure", "code_error"} else "blocked")
        return Observation(ok=category == "ok", category=category, data={**data, "evidence": evidence})

    def select_targets(self, targets):
        """Subset runs are feedback accelerators only; scope is recorded and submissions need a full pass."""
        if not targets:
            return []
        development = self.repo.profile.development_tests
        selected = []
        for raw in targets:
            file_part, _, node = raw.partition("::")
            target = self.canonical(file_part) + (f"::{node}" if node else "")
            if not any(file_part == d or file_part.startswith(d.rstrip("/") + "/") for d in development):
                raise PolicyError(f"test target outside development scope: {target}; allowed roots: "
                                  + ", ".join(development), code="test_target_outside_scope")
            if target not in selected:
                selected.append(target)
        return selected

    def model_memory(self):
        return self.state.model_memory(self.digest())

    def get_template_vars(self):
        return {"repository": self.repo.profile.name}

    def serialize(self):
        return {"environment": {"backend": self.runner.name, "plan": self.plan, "test_runs": self.test_runs,
                                "workflow": self.state.snapshot(self.digest()),
                                "control_stats": dict(self.control_stats)}}
