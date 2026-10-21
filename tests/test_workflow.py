import json
import threading
import pytest
from repairlab.feedback import test_feedback as extract_feedback
from repairlab.models import Limits
from repairlab.tools import ToolEnvironment
from repairlab.agent import RepairAgent
from repairlab.model import ScriptedModel


def result(code=0, log="1 passed"):
    return {"exit_code": code, "stdout": log, "stderr": "", "cancelled": False,
            "timed_out": False, "output_limited": False, "seconds": 0.01,
            **({"test_evidence": {"valid": True, "counts": {"passed": 1},
                 "nodes": [{"nodeid": "tests/test_code.py::test_answer", "outcome": "passed"}]}} if code == 0 else {})}


class Runner:
    name = "test-double"

    def __init__(self, results):
        self.results = iter(results)

    def run_tests(self, *args, **kwargs):
        return next(self.results)


def environment(repo, results):
    return ToolEnvironment(repo, Runner(results), Limits(), lambda *args: None, threading.Event())


def perform(env, tool, **kwargs):
    return env.execute({"tool": tool, **kwargs})


def prepare(env):
    assert perform(env, "update_plan", plan=["Read, reproduce, change, validate"])["ok"]
    assert perform(env, "read_file", path="code.py")["ok"]
    perform(env, "run_tests")
    assert perform(env, "record_finding", paths=["code.py"], summary="Return value is one lower than expected")["ok"]


def edit(env, old="return 1", new="return 2"):
    return perform(env, "edit_file", path="code.py", old=old, new=new)


def test_edit_requires_baseline_before_modifying_source(repo):
    env = environment(repo, [])
    perform(env, "update_plan", plan=["Fix"])
    perform(env, "read_file", path="code.py")
    perform(env, "record_finding", paths=["code.py"], summary="Need correct return value")
    observation = edit(env)
    assert observation["category"] == "policy_error"
    assert "baseline_not_run" in observation["error"]
    assert "return 1" in repo.path("code.py").read_text()


def test_submission_is_bound_to_revision_and_source_hash(repo):
    env = environment(repo, [result(1, "FAILED tests/test_code.py::test_answer"), result(), result()])
    prepare(env)
    assert env.state.reproduction_confirmed
    assert edit(env)["ok"]
    assert "tests_not_current" in perform(env, "submit")["error"]
    perform(env, "run_tests")
    assert perform(env, "submit")["ok"]
    perform(env, "record_finding", paths=["code.py"], summary="Further correct the boundary")
    assert edit(env, "return 2", "return 3")["ok"]
    assert "tests_not_current" in perform(env, "submit")["error"]
    perform(env, "run_tests")
    repo.path("code.py").write_text("def answer():\n    return 4\n", encoding="utf-8")
    assert "tests_not_current" in perform(env, "submit")["error"]


def test_rollback_restores_source_and_invalidates_tests(repo):
    env = environment(repo, [result(1, "1 failed"), result()])
    prepare(env)
    original = repo.path("code.py").read_text()
    edit(env)
    perform(env, "run_tests")
    assert "return 2" in perform(env, "get_patch")["data"]["patch"]
    assert perform(env, "rollback_edit")["ok"]
    assert repo.path("code.py").read_text() == original
    assert env.state.revision == 2
    blockers = env.state.submission_blockers(env.digest())
    assert any("empty_patch" in b for b in blockers)
    assert any("tests_not_current" in b for b in blockers)


def test_hypothesis_must_reference_read_file_and_current_revision(repo):
    env = environment(repo, [result(1)])
    observation = perform(env, "record_finding", paths=["code.py"], summary="Guess")
    assert observation["category"] == "policy_error"
    prepare(env)
    assert edit(env)["ok"]
    assert "missing_current_hypothesis" in edit(env, "return 2", "return 3")["error"]


def test_baseline_pass_is_not_claimed_as_reproduction(repo):
    env = environment(repo, [result()])
    prepare(env)
    assert not env.state.reproduction_confirmed
    assert not env.model_memory()["reproduction_confirmed"]


def test_environment_failure_blocks_speculative_edits(repo):
    env = environment(repo, [result(2, "ModuleNotFoundError: No module named dependency")])
    prepare(env)
    assert "baseline_unavailable" in edit(env)["error"]
    outcome = perform(env, "finish", reason="environment_error", summary="Missing dependency; operator must prepare the image")
    assert outcome["ok"] and outcome["data"]["reason"] == "environment_error"


def test_baseline_environment_retry_can_recover(repo):
    env = environment(repo, [result(2, "ImportError"), result(1, "1 failed")])
    prepare(env)
    perform(env, "run_tests")
    assert edit(env)["ok"]


@pytest.mark.parametrize("code,log,expected", [
    (1, "FAILED tests/test_code.py::test_answer - AssertionError", "test_failure"),
    (2, "SyntaxError: expected ':'", "code_error"),
    (2, "IndentationError: unexpected indent", "code_error"),
    (2, "ModuleNotFoundError: package", "environment_error"),
    (2, "fixture collection failed", "collection_error"),
    (5, "no tests ran", "environment_error"),
    (-9, "killed", "environment_error"),
])
def test_collection_errors_are_not_all_environment_failures(repo, code, log, expected):
    assert extract_feedback(result(code, log), repo.root, 1, 0, "hash")["category"] == expected


def test_failure_evidence_extracts_nodes_and_source_locations(repo):
    log = 'code.py:2: AssertionError\nFAILED tests/test_code.py::test_answer - mismatch\n1 failed, 2 passed'
    evidence = extract_feedback(result(1, log), repo.root, 3, 2, "hash")
    assert evidence["failed_nodes"] == ["tests/test_code.py::test_answer"]
    assert evidence["source_locations"] == [{"path": "code.py", "line": 2}]
    assert evidence["counts"] == {"failed": 1, "passed": 2}
    assert evidence["revision"] == 2 and evidence["run_id"] == 3


def test_source_mutation_during_test_cannot_be_used_as_baseline(repo):
    class MutatingRunner:
        name = "trusted-test-double"
        def run_tests(self, *args, **kwargs):
            repo.path("code.py").write_text("value = 100\n", encoding="utf-8")
            return result()
    env = ToolEnvironment(repo, MutatingRunner(), Limits(), lambda *args: None, threading.Event())
    observation = perform(env, "run_tests")
    assert observation["category"] == "workspace_changed_during_test"
    assert env.state.baseline_test is None


def test_agent_receives_memory_even_after_chat_window_changes(repo):
    env = environment(repo, [result(1)])
    prepare(env)
    def choose(messages):
        assert "code.py" in model.state["inspected_files"]
        assert model.state["baseline_test"]["category"] == "test_failure"
        assert model.state["hypothesis"]["summary"]
        return {"action": {"tool": "finish", "reason": "unresolved", "summary": "Report remaining evidence"}}
    model = ScriptedModel(choose)
    outcome = RepairAgent(model, env, Limits(), lambda *args: None, env.cancel).run("Repair")
    assert outcome["exit_status"] == "unresolved"
    assert json.dumps(model.state)


def test_real_syntax_error_can_be_repaired_after_collection_failure(repo):
    from repairlab.runners import LocalRunner
    # Avoid shadowing Python's stdlib code module when pytest imports pdb.
    repo.path("code.py").unlink()
    repo.path("broken_module.py").write_text("def answer()\n    return 2\n", encoding="utf-8")
    repo.path("tests/test_code.py").write_text("from broken_module import answer\ndef test_answer():\n    assert answer() == 2\n", encoding="utf-8")
    env = ToolEnvironment(repo, LocalRunner(allowed=True), Limits(), lambda *args: None, threading.Event())
    perform(env, "update_plan", plan=["Inspect collection failure"])
    perform(env, "read_file", path="broken_module.py")
    before = perform(env, "run_tests")
    assert before["category"] == "code_error", before
    assert env.state.reproduction_confirmed
    perform(env, "record_finding", paths=["broken_module.py"], summary="Function header misses its colon")
    assert perform(env, "edit_file", path="broken_module.py", old="def answer()", new="def answer():")["ok"]
    assert perform(env, "run_tests")["ok"]
    assert perform(env, "submit")["ok"]
