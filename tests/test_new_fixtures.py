"""Bundled fixtures for the extended capability set: uncovered defect and multi-file repair."""
import pytest
from repairlab.model import ScriptedModel
from repairlab.models import Limits, StartRequest
from repairlab.service import Service


def decision(tool, **kwargs):
    return {"note": "test action", "action": {"tool": tool, **kwargs}}


def test_quota_guard_uncovered_defect_reproducer_flow(settings):
    service = Service(settings)
    try:
        actions = [
            decision("update_plan", plan=["读取实现", "基线测试", "构造保留额复现", "修复并全量重测"]),
            decision("read_file", path="quota.py"),
            decision("run_tests"),
            decision("write_reproducer",
                     code="from quota import can_withdraw\n\n\ndef test_withdraw_below_min_balance():\n"
                          "    assert can_withdraw(100, 95) is False\n"),
            decision("run_reproducer"),
            decision("record_finding", paths=["quota.py"], summary="缺少最低保留额 10 元的判断"),
            decision("edit_file", path="quota.py", old="return amount <= balance",
                     new="return amount <= balance - MIN_BALANCE"),
            decision("run_reproducer"),
            decision("run_tests"),
            decision("submit", summary="加上最低保留额规则；复现先失败后通过，原测试保持通过"),
        ]
        task = service.start(StartRequest(fixture="quota-guard", mode="model", backend="local", allow_local=True,
                                          limits=Limits(max_steps=16)),
                             background=False, model_override=ScriptedModel(actions))
        assert task["status"] == "passed", task
        probes = task["verification"]["generated_reproducers"]
        assert probes and probes[0]["evidence"]["category"] == "ok"
        assert task["verification"]["acceptance"]["policy"]["passed"]
        assert task["verification"]["regression"]["policy"]["passed"]
    finally:
        service.close()


def test_order_pipeline_multi_file_repair(settings):
    service = Service(settings)
    try:
        actions = [
            decision("update_plan", plan=["读取两个文件", "基线测试", "修复 reserve 校验",
                                          "修复 place_order 流程", "重测后提交"]),
            decision("read_file", path="stock.py"),
            decision("read_file", path="order.py"),
            decision("run_tests"),
            decision("record_finding", paths=["stock.py"], summary="reserve 缺少库存不足校验"),
            decision("edit_file", path="stock.py", old="    stock[sku] = stock.get(sku, 0) - quantity\n    return stock[sku]",
                     new="    if quantity > stock.get(sku, 0):\n        return None\n"
                         "    stock[sku] = stock.get(sku, 0) - quantity\n    return stock[sku]"),
            decision("record_finding", paths=["order.py"], summary="place_order 未拒绝非正数量且不处理扣减失败"),
            decision("edit_file", path="order.py", old="    reserve(stock, sku, quantity)\n    return True",
                     new="    if quantity <= 0:\n        return False\n"
                         "    if reserve(stock, sku, quantity) is None:\n        return False\n    return True"),
            decision("run_tests"),
            decision("get_patch"),
            decision("submit", summary="库存与下单两处流程均按约定修复"),
        ]
        task = service.start(StartRequest(fixture="order-pipeline", mode="model", backend="local", allow_local=True,
                                          limits=Limits(max_steps=16)),
                             background=False, model_override=ScriptedModel(actions))
        assert task["status"] == "passed", task
        assert sorted(task["changed_files"]) == ["order.py", "stock.py"]
        assert task["verification"]["acceptance"]["policy"]["passed"]
    finally:
        service.close()


def test_demo_mode_rejected_for_model_only_fixtures(settings):
    service = Service(settings)
    try:
        with pytest.raises(ValueError, match="脚本演示路径"):
            service.start(StartRequest(fixture="quota-guard", mode="demo", backend="local", allow_local=True),
                          background=False)
    finally:
        service.close()


def test_fixtures_flag_demo_availability(settings):
    service = Service(settings)
    try:
        flags = {f["id"]: f["demo"] for f in service.fixtures()}
        assert flags["boundary-discount"] is True
        assert flags["quota-guard"] is False
        assert flags["order-pipeline"] is False
    finally:
        service.close()
