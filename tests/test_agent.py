import json
import threading
import time
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


FIRST_PHASE = [
    decision("update_plan", plan=["读取实现", "确认基线失败", "修改并重测", "提交"]),
    decision("read_file", path="pricing.py"),
    decision("run_tests"),
    decision("record_finding", paths=["pricing.py"], summary="阈值应包含第十件"),
    decision("edit_file", path="pricing.py", old="quantity > 10", new="quantity >= 10"),
]


def run_partial_task(settings, service, max_steps=5):
    """Five productive steps then stop; simulates the state a crash would leave behind."""
    model = ScriptedModel(FIRST_PHASE)
    task = service.start(StartRequest(backend="local", allow_local=True, mode="model",
                                      limits=Limits(max_steps=max_steps)),
                         background=False, model_override=model)
    task_dir = settings.data_dir / "tasks" / task["id"]
    assert (task_dir / "artifacts" / "checkpoint.json").exists()
    assert (task_dir / "artifacts" / "trajectory.json").exists()
    return task, task_dir


def wait_for_terminal(service, task_id, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = service.store.get(task_id)
        if task["status"] in {"passed", "failed", "error", "blocked", "cancelled", "budget_exhausted", "reviewed"}:
            return task
        time.sleep(0.1)
    return service.store.get(task_id)


def test_resume_completes_interrupted_task(settings):
    service = Service(settings)
    try:
        task, task_dir = run_partial_task(settings, service)
        assert task["status"] == "budget_exhausted"
        # Simulate a restart mid-run, with the operator extending the step budget before resuming.
        service.store.update(task["id"], status="interrupted", resumeable=True,
                             limits={**task["limits"], "max_steps": 12})
        continuation = ScriptedModel([decision("run_tests"), decision("get_patch"),
                                      decision("submit", summary="提交候选补丁")])
        resumed = service.resume(task["id"], model_override=continuation)
        assert resumed["status"] in {"queued", "running"}
        final = wait_for_terminal(service, task["id"])
        assert final["status"] == "passed", final
        assert final["verification"]["passed"]
        # The injected continuation model counts only its own three calls.
        assert final["model_calls"] == 3
        # Agent-level step accounting continues across the restart: five steps plus three.
        trajectory = json.loads((task_dir / "artifacts" / "trajectory.json").read_text(encoding="utf-8"))
        assert trajectory["info"]["model_stats"]["api_calls"] == 8
        kinds = [e["kind"] for e in service.store.events(task["id"])]
        assert kinds.count("started") == 2 and kinds[0] == "started"
        assert "interrupted" not in kinds and "error" not in kinds
    finally:
        service.close()


def test_resume_rejects_tampered_workspace(settings):
    service = Service(settings)
    try:
        task, task_dir = run_partial_task(settings, service)
        service.store.update(task["id"], status="interrupted", resumeable=True,
                             limits={**task["limits"], "max_steps": 12})
        (task_dir / "workspace" / "pricing.py").write_text("def price(q):\n    return 0  # tampered\n", encoding="utf-8")
        with pytest.raises(ValueError, match="工作区"):
            service.resume(task["id"], model_override=ScriptedModel([]))
    finally:
        service.close()


def test_recover_interrupted_marks_checkpointed_tasks_resumeable(settings):
    service = Service(settings)
    try:
        task, _ = run_partial_task(settings, service)
        service.store.create("bare-restart", {"issue": "x"})
        service.store.update(task["id"], status="running")
        service.store.update("bare-restart", status="running")
        service.store.recover_interrupted()
        assert service.store.get(task["id"])["resumeable"] is True
        assert service.store.get("bare-restart")["resumeable"] is False
        assert service.store.get("bare-restart")["status"] == "interrupted"
    finally:
        service.close()


def test_follow_up_completes_task_in_same_workspace(settings):
    service = Service(settings)
    try:
        task, task_dir = run_partial_task(settings, service)
        baseline_before = (task_dir / "baseline" / "pricing.py").read_text(encoding="utf-8")
        continuation = ScriptedModel([decision("run_tests"), decision("get_patch"),
                                      decision("submit", summary="追加轮提交候选补丁")])
        followed = service.follow_up(task["id"], "继续完成修复并提交",
                                     model_override=continuation)
        assert followed["status"] in {"queued", "running"}
        final = wait_for_terminal(service, task["id"])
        assert final["status"] == "passed", final
        assert final["verification"]["passed"]
        assert final["round"] == 2
        assert final["model_calls"] == 8  # round usage accumulates across rounds (5 + 3)
        assert final["tokens"]["prompt"] >= 0
        instructions = [e for e in service.store.events(task["id"]) if e["kind"] == "instruction"]
        assert len(instructions) == 1 and instructions[0]["data"]["round"] == 2
        assert instructions[0]["data"]["text"] == "继续完成修复并提交"
        # The baseline snapshot is never rebuilt for follow-up rounds.
        assert (task_dir / "baseline" / "pricing.py").read_text(encoding="utf-8") == baseline_before
    finally:
        service.close()


def test_follow_up_rejects_demo_running_and_tampered_workspace(settings):
    service = Service(settings)
    try:
        demo = service.start(StartRequest(backend="local", allow_local=True), background=False)
        assert demo["status"] == "passed"
        with pytest.raises(ValueError, match="脚本演示"):
            service.follow_up(demo["id"], "继续")
        task, task_dir = run_partial_task(settings, service)
        service.store.update(task["id"], status="running")
        with pytest.raises(ValueError, match="结束"):
            service.follow_up(task["id"], "继续")
        service.store.update(task["id"], status="budget_exhausted")
        (task_dir / "workspace" / "pricing.py").write_text("def price(q):\n    return 0  # tampered\n", encoding="utf-8")
        with pytest.raises(ValueError, match="工作区"):
            service.follow_up(task["id"], "继续")
        with pytest.raises(ValueError, match="不能为空"):
            service.follow_up(task["id"], "  ")
    finally:
        service.close()
