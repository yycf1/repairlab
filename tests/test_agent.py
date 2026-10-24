import threading
from types import SimpleNamespace
import pytest
from repairlab.agent import RepairAgent
from repairlab.model import ScriptedModel
from repairlab.models import Limits, StartRequest
from repairlab.service import Service
from repairlab.tools import ToolEnvironment


def decision(tool, **kwargs):
    return {"note": "test action", "action": {"tool": tool, **kwargs}}


@pytest.mark.parametrize("fixture,strategy,expected", [
    ("boundary-discount", "feedback", "passed"),
    ("boundary-discount", "single_shot", "passed"),
    ("csv-tags", "single_shot", "failed"),
    ("csv-tags", "feedback", "passed"),
    ("pagination", "feedback", "passed"),
    ("pagination", "single_shot", "passed"),
])
def test_real_pytest_repair_pipeline(settings, fixture, strategy, expected):
    service = Service(settings)
    try:
        task = service.start(StartRequest(fixture=fixture, strategy=strategy, backend="local", allow_local=True), background=False)
        assert task["status"] == expected, task
        assert task["verification"]["independent_acceptance"]
        assert task["verification"]["regression"] is not None
        events = service.store.events(task["id"])
        if fixture == "csv-tags" and strategy == "feedback":
            edits = [e for e in events if e["kind"] == "action" and e["data"]["action"]["tool"] == "edit_file"]
            assert len(edits) == 2
        assert events[-1]["kind"] == "finished"
        assert not (settings.data_dir / "tasks" / task["id"] / "workspace" / "_acceptance").exists()
        assert (settings.data_dir / "tasks" / task["id"] / "artifacts" / "report.json").exists()
    finally:
        service.close()


def test_false_success_is_rejected_without_patch(settings):
    service = Service(settings)
    try:
        task = service.start(StartRequest(backend="local", allow_local=True), background=False,
                             model_override=ScriptedModel(lambda _: decision("submit", summary="Everything is fixed")))
        assert task["status"] == "blocked"
        assert task["result"] == "no_progress"
        assert "verification" not in task
        assert any(e["data"].get("category") == "policy_error" for e in service.store.events(task["id"]))
    finally:
        service.close()


def test_adversarial_no_plan_edit_is_blocked_with_control_stats(settings):
    service = Service(settings)
    model = ScriptedModel([decision("edit_file", path="pricing.py", old="quantity > 10", new="quantity >= 10")
                           for _ in range(3)])
    try:
        task = service.start(StartRequest(backend="local", allow_local=True), background=False, model_override=model)
        assert task["status"] == "blocked"
        assert task["result"] == "no_progress"
        assert "verification" not in task
        assert "patch" not in task
        assert task["control_stats"] == {"missing_plan": 3}
        assert (settings.data_dir / "tasks" / task["id"] / "artifacts" / "report.json").exists()
    finally:
        service.close()


def test_step_budget_stops_loop(repo):
    limits = Limits(max_steps=2)
    model = ScriptedModel(lambda _: decision("list_files"))
    env = ToolEnvironment(repo, SimpleNamespace(name="unit"), limits, lambda *args: None, threading.Event())
    agent = RepairAgent(model, env, limits, lambda *args: None, env.cancel)
    result = agent.run("Inspect")
    assert result["exit_status"] == "LimitsExceeded"
    assert model.model_calls == 2


def test_cancel_before_query(repo):
    cancel = threading.Event()
    cancel.set()
    model = ScriptedModel([])
    env = ToolEnvironment(repo, SimpleNamespace(name="unit"), Limits(), lambda *args: None, cancel)
    result = RepairAgent(model, env, Limits(), lambda *args: None, cancel).run("Inspect")
    assert result["exit_status"] == "cancelled"
    assert model.model_calls == 0


def test_token_budget_stops_before_next_query(repo):
    model = ScriptedModel([])
    model.tokens["prompt"] = 1000
    env = ToolEnvironment(repo, SimpleNamespace(name="unit"), Limits(max_tokens=1000), lambda *args: None, threading.Event())
    result = RepairAgent(model, env, env.limits, lambda *args: None, env.cancel).run("Inspect")
    assert result["exit_status"] == "token_budget"


def test_repeated_action_guard_and_bad_schema(repo):
    env = ToolEnvironment(repo, None, Limits(), lambda *args: None, threading.Event())
    for _ in range(3):
        assert env.execute({"tool": "list_files"})["ok"]
    assert env.execute({"tool": "list_files"})["category"] == "tool_error"
    assert env.execute({"tool": "shell", "command": "anything"})["category"] == "tool_error"


def test_task_restart_preserves_history(settings):
    from repairlab.storage import Store
    store = Store(settings.data_dir / "history.sqlite3")
    store.create("task", {"issue": "original"})
    store.update("task", status="running")
    cursor = store.event("task", "action", {"tool": "read_file"})
    store.recover_interrupted()
    assert store.get("task")["status"] == "interrupted"
    assert store.get("task")["issue"] == "original"
    assert [e["kind"] for e in store.events("task", cursor)] == ["interrupted"]


def test_data_directory_lease_prevents_false_restart_recovery(settings):
    service = Service(settings)
    try:
        with pytest.raises(RuntimeError, match="REPAIRLAB_DATA_DIR"):
            Service(settings)
    finally:
        service.close()
    restarted = Service(settings)
    restarted.close()


def test_budget_exit_keeps_unverified_patch_and_evidence(settings):
    service = Service(settings)
    model = ScriptedModel([
        decision("update_plan", plan=["Inspect and reproduce"]),
        decision("read_file", path="pricing.py"),
        decision("run_tests"),
        decision("record_finding", paths=["pricing.py"], summary="Threshold should include ten"),
        decision("edit_file", path="pricing.py", old="quantity > 10", new="quantity >= 10"),
    ])
    try:
        task = service.start(StartRequest(backend="local", allow_local=True, limits=Limits(max_steps=5)),
                             background=False, model_override=model)
        assert task["status"] == "budget_exhausted"
        assert task["unverified_patch"]
        assert "verification" not in task
        assert task["model_calls"] == 5
        assert task["workflow"]["baseline_test"]["category"] == "test_failure"
        assert task["workflow"]["revision"] == 1
        assert (settings.data_dir / "tasks" / task["id"] / "artifacts" / "patch.diff").exists()
    finally:
        service.close()
