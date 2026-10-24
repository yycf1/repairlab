from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal["list_files", "search", "read_file", "symbols", "references", "outline", "edit_file",
                  "run_tests", "update_plan", "record_finding", "get_patch", "rollback_edit", "submit", "finish",
                  "record_candidate", "resolve_candidate", "write_reproducer", "run_reproducer"]
    path: str = ""
    query: str = Field(default="", max_length=300)
    symbol: str = Field(default="", max_length=80)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=160, ge=1)
    old: str = Field(default="", max_length=30000)
    new: str = Field(default="", max_length=30000)
    plan: list[str] = Field(default_factory=list, max_length=12)
    summary: str = Field(default="", max_length=1500)
    paths: list[str] = Field(default_factory=list, max_length=8)
    reason: Literal["unresolved", "environment_error", "no_reproduction", "investigation_complete"] = "unresolved"
    candidate_id: str = Field(default="", max_length=20)
    expectation: str = Field(default="", max_length=1000)
    basis: str = Field(default="", max_length=1000)
    basis_paths: list[str] = Field(default_factory=list, max_length=8)
    disposition: Literal["rejected", "inconclusive"] = "inconclusive"
    code: str = Field(default="", max_length=12000)
    targets: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def required_arguments(self):
        if self.tool in {"read_file", "symbols", "edit_file"} and not self.path:
            raise ValueError("This tool requires path")
        if self.tool == "search" and not self.query.strip():
            raise ValueError("Search query cannot be empty")
        if self.tool == "edit_file" and not self.old:
            raise ValueError("An exact nonempty old fragment is required")
        if self.tool == "update_plan" and not self.plan:
            raise ValueError("Plan cannot be empty")
        if self.tool == "record_finding" and (not self.paths or not self.summary.strip()):
            raise ValueError("A finding requires inspected source paths and a testable hypothesis in summary")
        if self.tool == "record_candidate" and not all([self.paths, self.summary.strip(), self.expectation.strip(), self.basis.strip(), self.basis_paths]):
            raise ValueError("A candidate requires source paths, summary, expectation and a cited basis with basis_paths")
        if self.tool == "write_reproducer" and not self.code.strip():
            raise ValueError("Provide a minimal pytest test in code")
        if self.tool == "resolve_candidate" and not self.summary.strip():
            raise ValueError("Explain the evidence for rejecting or deferring the candidate")
        if self.tool == "references" and not self.symbol.strip():
            raise ValueError("references requires the symbol name to locate")
        if self.tool == "finish" and not self.summary.strip():
            raise ValueError("Explain why the task cannot continue in summary")
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=400, description="Brief action rationale, not hidden reasoning")
    action: Action


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_steps: int = Field(default=20, ge=1, le=60)
    max_tokens: int = Field(default=30000, ge=1000, le=120000)
    max_seconds: int = Field(default=300, ge=10, le=1800)
    command_seconds: int = Field(default=30, ge=1, le=120)
    context_chars: int = Field(default=24000, ge=4000, le=80000)
    max_test_runs: int = Field(default=8, ge=1, le=20)


class RepoProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    source: str
    editable: list[str] = Field(default_factory=lambda: ["src/**/*.py", "*.py"])
    protected: list[str] = Field(default_factory=list)
    development_tests: list[str] = Field(default_factory=lambda: ["tests"])
    acceptance: str | None = None
    regression_tests: list[str] = Field(default_factory=lambda: ["tests"])
    required_acceptance_tests: list[str] = Field(default_factory=list, max_length=5000)
    required_regression_tests: list[str] = Field(default_factory=list, max_length=5000)
    image: str = "repairlab-python:latest"
    demo: bool = False
    issue: str = Field(default="", max_length=6000)


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixture: str = Field(default="boundary-discount", pattern=r"^[a-z0-9-]+$")
    issue: str = Field(default="", max_length=6000)
    mode: Literal["demo", "model"] = "demo"
    backend: Literal["docker", "local"] = "docker"
    strategy: Literal["feedback", "single_shot"] = "feedback"
    task_kind: Literal["repair", "inspect"] = "repair"
    allow_local: bool = False
    limits: Limits = Field(default_factory=Limits)


class RepositoryCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: Literal["docker", "local"] = "docker"
    allow_local: bool = False
    collect_tests: bool = False


class RepositoryRegistration(RepositoryCheck):
    profile: RepoProfile
    authorize_directory: bool = False


class DirectoryDiscovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=2000)
    authorize_directory: bool = False


class DirectoryPick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    initial: str = Field(default="", max_length=2000)


class Observation(BaseModel):
    ok: bool
    category: str = "ok"
    data: dict = Field(default_factory=dict)
    error: str = ""


class ModelSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=300)
    api_key: str = Field(default="", max_length=300)


class FollowUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=6000)
