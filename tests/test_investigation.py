import json
import threading

import pytest
from fastapi.testclient import TestClient

from repairlab.api import create_app
from repairlab.model import ScriptedModel
from repairlab.models import Limits, RepoProfile, StartRequest
from repairlab.repository import Repository, copy_repository
from repairlab.runners import LocalRunner
from repairlab.service import Service
from repairlab.tools import ToolEnvironment
from repairlab.verification import Verifier


CODE = "from calculator import answer\ndef test_contract():\n    assert answer() == 2\n"


def do(env, tool, **kwargs):
    return env.execute({"tool": tool, **kwargs})


@pytest.fixture
def investigation(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    (source / "calculator.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    (source / "README.md").write_text("answer() must return 2", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_basic.py").write_text("def test_basic():\n    assert True\n", encoding="utf-8")
    profile = RepoProfile(name="investigation", source=str(source), editable=["calculator.py"])
    baseline, workspace = tmp_path / "task" / "baseline", tmp_path / "task" / "workspace"
    copy_repository(source, baseline)
    copy_repository(source, workspace)
    env = ToolEnvironment(Repository(workspace, profile), LocalRunner(allowed=True), Limits(), lambda *args: None,
                          threading.Event(), task_kind="inspect", baseline=baseline)
    do(env, "update_plan", plan=["Read the behavioral contract, test, investigate"])
    do(env, "read_file", path="calculator.py")
    do(env, "read_file", path="README.md")
    assert do(env, "run_tests")["ok"]
    return env, profile


def candidate(env, expectation="answer returns 2"):
    return do(env, "record_candidate", paths=["calculator.py"], summary="Investigate return contract",
              expectation=expectation, basis="README explicitly requires 2", basis_paths=["README.md"])


def confirmed(env):
    assert candidate(env)["ok"]
    assert do(env, "write_reproducer", candidate_id="c1", code=CODE)["ok"]
    observation = do(env, "run_reproducer", candidate_id="c1")
    assert observation["category"] == "test_failure", observation
    assert env.state.candidates[0]["status"] == "confirmed"


def fix(env):
    assert do(env, "record_finding", candidate_id="c1", paths=["calculator.py"], summary="Return the documented value")["ok"]
    assert do(env, "edit_file", path="calculator.py", old="return 1", new="return 2")["ok"]


def test_uncovered_bug_baseline_probe_fix_and_submission_gates(investigation):
    env, profile = investigation
    original_tests = env.repo.path("tests/test_basic.py").read_bytes()
    confirmed(env)
    assert not env.state.reproduction_confirmed  # Original suite passed; probe evidence stays distinct.
    fix(env)
    assert do(env, "run_tests")["ok"]
    assert "reproducer_not_current" in do(env, "submit")["error"]
    assert do(env, "run_reproducer", candidate_id="c1")["ok"]
    assert do(env, "submit")["ok"]
    assert env.repo.path("tests/test_basic.py").read_bytes() == original_tests
    assert not list(env.repo.root.glob("test_repairlab*"))
    assert "return 1" in (env.investigation.baseline / "calculator.py").read_text()
    # A source change not accompanied by a revision still invalidates both forms of evidence.
    env.repo.path("calculator.py").write_text("def answer():\n    return 3\n", encoding="utf-8")
    blockers = env.state.submission_blockers(env.digest())
    assert any("reproducer_not_current" in b for b in blockers)


def test_cannot_edit_an_unconfirmed_candidate(investigation):
    env, _ = investigation
    candidate(env)
    assert not do(env, "record_finding", candidate_id="c1", paths=["calculator.py"], summary="Guess")["ok"]
    assert not do(env, "edit_file", path="calculator.py", old="return 1", new="return 2")["ok"]
    assert "return 1" in env.repo.path("calculator.py").read_text()


def test_passing_probe_can_be_rejected_and_does_not_confirm(investigation):
    env, _ = investigation
    candidate(env, "answer returns 1")
    do(env, "write_reproducer", candidate_id="c1", code=CODE.replace("== 2", "== 1"))
    assert do(env, "run_reproducer", candidate_id="c1")["ok"]
    assert env.state.candidates[0]["status"] == "suspected"
    assert not do(env, "finish", reason="investigation_complete", summary="Done")["ok"]
    do(env, "resolve_candidate", candidate_id="c1", disposition="rejected", summary="The observed behavior meets the expectation")
    assert do(env, "finish", reason="investigation_complete", summary="No confirmed issue in the checked path")["ok"]
    assert env.state.phase == "reviewed"


@pytest.mark.parametrize("code,expected", [
    ("def test_bad():\n    import missing_dependency_for_probe\n    assert 1 == 2\n", "reproducer_error"),
    ("from missing_dependency_for_probe import answer\ndef test_bad():\n    assert answer() == 2\n", "environment_error"),
    ("import pytest\n@pytest.mark.skip(reason='not exercised')\ndef test_skip():\n    assert 1 == 2\n", "reproducer_no_assertion_result"),
    ("from pathlib import Path\ndef test_mutate():\n    Path('calculator.py').write_text('modified')\n    assert 1 == 2\n", "probe_workspace_changed"),
])
def test_bad_probe_cannot_confirm_a_problem(investigation, code, expected):
    env, _ = investigation
    candidate(env)
    assert do(env, "write_reproducer", candidate_id="c1", code=code)["ok"]
    result = do(env, "run_reproducer", candidate_id="c1")
    assert result["category"] == expected, result
    assert env.state.candidates[0]["status"] == "suspected"
    assert "return 1" in env.repo.path("calculator.py").read_text()


def test_probe_is_immutable_and_hash_checked(investigation):
    env, _ = investigation
    confirmed(env)
    assert not do(env, "write_reproducer", candidate_id="c1", code=CODE.replace("== 2", "== 1"))["ok"]
    path = env.investigation.artifacts / "reproducer-c1.py"
    path.write_text(CODE + "# tampered\n", encoding="utf-8")
    assert "changed after" in do(env, "run_reproducer", candidate_id="c1")["error"]


def test_clean_verifier_rejects_patch_that_only_passes_original_tests(investigation):
    env, profile = investigation
    confirmed(env)
    do(env, "record_finding", candidate_id="c1", paths=["calculator.py"], summary="Deliberately wrong patch for verification test")
    do(env, "edit_file", path="calculator.py", old="return 1", new="return 3")
    assert do(env, "run_tests")["ok"]
    result = Verifier(env.runner, env.limits).verify(env.investigation.baseline, env.repo.root,
        env.repo.root.parent / "verification", profile, probes=env.investigation.confirmed_probes())
    assert result["acceptance"]["exit_code"] == 0 and result["regression"]["exit_code"] == 0
    assert not result["passed"]
    assert result["reason"] == "generated_reproducer_failed"


def test_completed_investigation_cannot_hide_an_unverified_patch(investigation):
    env, _ = investigation
    confirmed(env)
    fix(env)
    assert not do(env, "finish", reason="investigation_complete", summary="All done")["ok"]
    assert do(env, "finish", reason="unresolved", summary="Unverified changes retained")["ok"]


def test_probe_uses_shared_budget_and_local_execution_policy(investigation):
    env, _ = investigation
    candidate(env)
    do(env, "write_reproducer", candidate_id="c1", code=CODE)
    env.limits.max_test_runs = env.test_runs
    assert "budget" in do(env, "run_reproducer", candidate_id="c1")["error"]
    assert env.state.candidates[0].get("baseline_probe") is None


def test_candidates_require_read_behavioral_basis_and_bound_scope(investigation):
    env, _ = investigation
    assert not do(env, "record_candidate", paths=["calculator.py"], summary="Guess", expectation="2",
                  basis="unread", basis_paths=["tests/test_basic.py"])["ok"]
    assert not do(env, "record_candidate", paths=["../escape.py"], summary="Guess", expectation="2",
                  basis="contract", basis_paths=["README.md"])["ok"]


def decision(tool, **kwargs):
    return {"action": {"tool": tool, **kwargs}}


def investigation_actions():
    return [
        decision("update_plan", plan=["Read documentation and tests before hypothesizing"]),
        decision("read_file", path="labels.py"), decision("read_file", path="README.md"),
        decision("run_tests"),
        decision("record_candidate", paths=["labels.py"], summary="Potential empty labels and whitespace",
                 expectation="Trim labels and omit empty values", basis="README gives explicit parsing rules", basis_paths=["README.md"]),
        decision("write_reproducer", candidate_id="c1", code="from labels import parse_labels\ndef test_trim():\n    assert parse_labels(' A,, B ') == ['A', 'B']\n"),
        decision("run_reproducer", candidate_id="c1"),
        decision("update_plan", plan=["Reproduced a missing behavior; fix and check regressions"], summary="Existing tests passed but the probe failed"),
        decision("record_finding", candidate_id="c1", paths=["labels.py"], summary="Split preserves whitespace and empty items"),
        decision("edit_file", path="labels.py", old='return text.split(",")', new='return [label.strip() for label in text.split(",") if label.strip()]'),
        decision("run_reproducer", candidate_id="c1"), decision("run_tests"), decision("get_patch"),
        decision("submit", summary="Candidate reproduced, corrected and checked against original tests"),
    ]


def sample_profile(settings):
    base = settings.root / "examples" / "investigation"
    data = json.loads((base / "repository.json").read_text(encoding="utf-8"))
    data.update(source=str(base / "repo"), acceptance=str(base / "acceptance"))
    return RepoProfile.model_validate(data)


def test_service_investigation_end_to_end_and_clean_probe_verification(settings):
    service = Service(settings)
    try:
        profile = sample_profile(settings)
        task = service.start(StartRequest(task_kind="inspect", mode="model", backend="local", allow_local=True),
                             profile=profile, background=False, model_override=ScriptedModel(investigation_actions()))
        assert task["status"] == "passed", task
        milestones = [e["data"] for e in service.store.events(task["id"]) if e["kind"] == "milestone"]
        kinds = [m["kind"] for m in milestones]
        for required in ("candidate", "repro", "hypothesis", "submitted", "verdict"):
            assert required in kinds, kinds
        first_repro = next(m for m in milestones if m["kind"] == "repro")
        assert first_repro["confirmed"] is True and first_repro["first_probe"] is True
        assert milestones[-1]["kind"] == "verdict" and milestones[-1]["passed"] is True
        assert task["workflow"]["baseline_test"]["category"] == "ok"
        assert task["workflow"]["candidates"][0]["baseline_probe"]["category"] == "test_failure"
        assert task["verification"]["generated_reproducers"][0]["evidence"]["category"] == "ok"
        assert not task["verification"]["generated_reproducers"][0]["independent_oracle"]
        assert len(task["workflow"]["plan_history"]) == 2
        assert "原有测试通过" not in task["issue"]  # No answer or defect description provided to the model.
        report = settings.data_dir / "tasks" / task["id"] / "artifacts" / "report.json"
        assert json.loads(report.read_text(encoding="utf-8"))["task_kind"] == "inspect"
        assert 'return text.split(",")' in (settings.root / "examples/investigation/repo/labels.py").read_text()
    finally:
        service.close()


def test_review_without_patch_has_distinct_terminal_state_and_sse(settings):
    with TestClient(create_app(settings)) as client:
        service = client.app.state.service
        actions = [decision("update_plan", plan=["Inspect and report"]), decision("read_file", path="labels.py"),
                   decision("run_tests"), decision("finish", reason="investigation_complete", summary="Limited check; no confirmed issues")]
        task = service.start(StartRequest(task_kind="inspect", mode="model", backend="local", allow_local=True),
                             profile=sample_profile(settings), background=False, model_override=ScriptedModel(actions))
        assert task["status"] == "reviewed", task
        assert "verification" not in task
        assert client.get(f"/api/tasks/{task['id']}/stream").status_code == 200
        assert client.get(f"/api/tasks/{task['id']}/artifacts/report").status_code == 200
        assert client.post("/api/tasks", json={"task_kind":"inspect", "mode":"demo"}).status_code == 400
        assert client.post("/api/tasks", json={"task_kind":"inspect", "strategy":"single_shot"}).status_code == 400
