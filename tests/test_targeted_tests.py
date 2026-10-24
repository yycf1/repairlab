"""Targeted development-test runs: fast feedback, but submissions need one full pass."""
import pytest
from repairlab.model import ScriptedModel
from repairlab.models import Limits, StartRequest
from repairlab.service import Service
from repairlab.tools import PolicyError, ToolEnvironment


def decision(tool, **kwargs):
    return {"note": "test action", "action": {"tool": tool, **kwargs}}


def test_partial_run_scope_recorded_and_submission_blocked(settings):
    service = Service(settings)
    try:
        actions = [
            decision("update_plan", plan=["读取实现", "基线测试", "修改阈值", "子集重测后提交"]),
            decision("read_file", path="pricing.py"),
            decision("run_tests", targets=["tests/test_pricing.py"]),
            decision("record_finding", paths=["pricing.py"], summary="阈值 >10 应为 >=10"),
            decision("edit_file", path="pricing.py", old="quantity > 10", new="quantity >= 10"),
            decision("run_tests", targets=["tests/test_pricing.py"]),
            decision("submit", summary="只跑了子集就想提交"),
            decision("submit", summary="仍然只跑了子集"),
            decision("submit", summary="第三次"),
        ]
        task = service.start(StartRequest(backend="local", allow_local=True, limits=Limits(max_steps=14)),
                             background=False, model_override=ScriptedModel(actions))
        assert task["status"] == "blocked", task
        assert task["result"] == "no_progress"
        assert task["control_stats"].get("submission_blocked") == 3
        partials = [e["data"] for e in service.store.events(task["id"])
                    if e["kind"] == "milestone" and e["data"]["kind"] == "test_result"
                    and e["data"].get("scope") == "partial"]
        assert len(partials) == 2 and partials[-1]["category"] == "ok"
        assert any(b.startswith("tests_partial") for b in task["workflow"]["submission_blockers"])
    finally:
        service.close()


def test_targets_outside_development_scope_are_rejected(repo):
    environment = ToolEnvironment(repo, runner=None, limits=Limits(), emit=lambda *a: None, cancel=None)
    with pytest.raises(PolicyError) as exc:
        environment.select_targets(["_acceptance/test_acceptance.py"])
    assert exc.value.code == "test_target_outside_scope"
    assert environment.select_targets(["tests/test_code.py::test_one"]) == ["tests/test_code.py::test_one"]
    assert environment.select_targets([]) == []
