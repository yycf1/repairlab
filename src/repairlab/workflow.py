"""Evidence memory and deterministic submission policy for an autonomous loop."""
from pydantic import BaseModel, Field


class WorkflowState(BaseModel):
    phase: str = "inspecting"
    task_kind: str = "repair"
    candidates: list[dict] = Field(default_factory=list)
    active_candidate: str | None = None
    plan_history: list[dict] = Field(default_factory=list)
    submission_policy: str = "feedback"
    revision: int = 0
    plan: list[str] = Field(default_factory=list)
    inspected_files: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    baseline_test: dict | None = None
    latest_test: dict | None = None
    hypothesis: dict | None = None
    hypotheses: list[dict] = Field(default_factory=list)
    consecutive_action_errors: int = 0

    @property
    def reproduction_confirmed(self):
        return bool(self.baseline_test and self.baseline_test["category"] in {"test_failure", "code_error"})

    def submission_blockers(self, digest):
        blockers = []
        if not self.plan:
            blockers.append("missing_plan: record an actionable plan")
        if self.baseline_test is None:
            blockers.append("baseline_not_run: test the unchanged source first")
        if not self.changed_files:
            blockers.append("empty_patch: there is no source change")
        if self.task_kind == "inspect":
            confirmed = [c for c in self.candidates if c["status"] == "confirmed"]
            if not confirmed:
                blockers.append("no_confirmed_candidate: reproduce a candidate before submitting")
            for candidate in confirmed:
                current = candidate.get("latest_probe") or {}
                if (current.get("category") != "ok" or current.get("revision") != self.revision
                        or current.get("source_digest") != digest):
                    blockers.append(f"reproducer_not_current: rerun {candidate['id']} on this source revision")
        if self.submission_policy == "single_shot":
            return blockers
        if not self.latest_test or self.latest_test["revision"] != self.revision or self.latest_test["source_digest"] != digest:
            blockers.append("tests_not_current: run development tests after the latest source change")
        elif self.latest_test["category"] != "ok":
            blockers.append("tests_not_passed: fix or report the latest failure")
        return blockers

    def snapshot(self, digest):
        data = self.model_dump()
        data["reproduction_confirmed"] = self.reproduction_confirmed
        data["submission_blockers"] = self.submission_blockers(digest)
        return data

    def model_memory(self, digest):
        data = self.snapshot(digest)
        data.pop("hypotheses")
        data.pop("plan_history")
        data["candidates"] = []
        for candidate in self.candidates:
            brief = {k: (v[:240] if isinstance(v, str) else v) for k, v in candidate.items()
                     if k not in {"code", "probe_history", "code_sha256", "basis_review"}}
            for key in ("baseline_probe", "latest_probe"):
                if brief.get(key):
                    brief[key] = {k: v for k, v in brief[key].items()
                                  if k in {"category", "revision", "counts", "run_id"}}
            data["candidates"].append(brief)
        return data
