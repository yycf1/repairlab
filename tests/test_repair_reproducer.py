"""Uncovered-defect repair: the reproducer quad anchors fixes when no existing test fails."""
import pytest
from repairlab.model import ScriptedModel
from repairlab.models import Limits, RepoProfile, StartRequest
from repairlab.service import Service


@pytest.fixture
def uncovered_repo(tmp_path):
    """Baseline tests pass; the issue describes an upper bound no test covers."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "shapes.py").write_text("def clamp(value):\n    return max(0, value)\n", encoding="utf-8")
    (root / "tests" / "test_shapes.py").write_text(
        "from shapes import clamp\n\n\ndef test_clamp_floor():\n    assert clamp(-5) == 0\n", encoding="utf-8")
    return RepoProfile(name="clamp", source=str(root), editable=["shapes.py"],
                       development_tests=["tests"], regression_tests=["tests"])


def decision(tool, **kwargs):
    return {"note": "test action", "action": {"tool": tool, **kwargs}}


REPRODUCER = "from shapes import clamp\n\n\ndef test_clamp_upper():\n    assert clamp(150) == 100\n"


def service_for(settings, repo_profile):
    return Service(settings)


def test_uncovered_defect_requires_confirmed_reproducer(settings, uncovered_repo):
    service = Service(settings)
    try:
        actions = [
            decision("update_plan", plan=["读取实现", "基线测试", "复现上限缺陷", "修复并重测"]),
            decision("read_file", path="shapes.py"),
            decision("run_tests"),
            decision("write_reproducer", code=REPRODUCER),
            decision("run_reproducer"),
            decision("record_finding", paths=["shapes.py"], summary="clamp 只保底 0，缺少 100 的上限"),
            decision("edit_file", path="shapes.py", old="return max(0, value)",
                     new="return min(100, max(0, value))"),
            decision("run_reproducer"),
            decision("run_tests"),
            decision("submit", summary="clamp 增加 100 上限，复现先失败后通过，原测试保持通过"),
        ]
        task = service.start(StartRequest(issue="clamp 应同时限制最大值 100，当前只保底 0；请修复上限判断。",
                                          mode="model", backend="local", allow_local=True,
                                          limits=Limits(max_steps=16)),
                             profile=uncovered_repo, background=False, model_override=ScriptedModel(actions))
        assert task["status"] == "passed", task
        events = service.store.events(task["id"])
        repro_milestones = [e["data"] for e in events if e["kind"] == "milestone" and e["data"]["kind"] == "repro"]
        first, rerun = repro_milestones[0], repro_milestones[-1]
        assert first["confirmed"] is True and first["first_probe"] is True
        assert rerun["confirmed"] is True and rerun["first_probe"] is False
        probes = task["verification"]["generated_reproducers"]
        assert len(probes) == 1 and probes[0]["evidence"]["category"] == "ok"
        assert task["verification"]["regression"]["policy"]["passed"]
    finally:
        service.close()


def test_edit_without_failing_evidence_is_blocked(settings, uncovered_repo):
    service = Service(settings)
    try:
        actions = [
            decision("update_plan", plan=["读取实现", "基线测试", "直接修改上限", "重测后提交"]),
            decision("read_file", path="shapes.py"),
            decision("run_tests"),
            decision("record_finding", paths=["shapes.py"], summary="凭问题描述直接加上限"),
            decision("edit_file", path="shapes.py", old="return max(0, value)",
                     new="return min(100, max(0, value))"),
            decision("finish", reason="unresolved", summary="缺少失败证据，无法安全修改，停止。"),
        ]
        task = service.start(StartRequest(issue="clamp 应同时限制最大值 100。", mode="model", backend="local",
                                          allow_local=True, limits=Limits(max_steps=12)),
                             profile=uncovered_repo, background=False, model_override=ScriptedModel(actions))
        assert task["status"] == "blocked", task
        assert task["result"] == "unresolved"
        assert task["control_stats"].get("no_failing_evidence") == 1
        blockers = task["workflow"]["submission_blockers"]
        assert any(b.startswith("no_failing_evidence") for b in blockers)
    finally:
        service.close()
