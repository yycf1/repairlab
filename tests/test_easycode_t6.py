"""EasyCode T6：工作台补强——tool_call 前置事件、details 增强、模型设置 API、目录选择端点。"""
import json

import pytest
from fastapi.testclient import TestClient

from easycode.api import create_app
from easycode.core.loop import agent_loop
from easycode.tools import ToolContext
from easycode.tools.builtins import build_registry

from test_easycode_core import ScriptedClient


def test_loop_emits_tool_call_before_execution(tmp_path):
    events = []
    client = ScriptedClient([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"text": "x"}'}}]},
        {"role": "assistant", "content": "done"},
    ])
    registry = build_registry({"read"})
    registry.register(_echo_tool())
    agent_loop(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "t"}],
               client=client, tools=registry, ctx=ToolContext(workspace=tmp_path),
               on_event=lambda kind, data: events.append((kind, data)))
    kinds = [kind for kind, _ in events]
    assert kinds.index("tool_call") < kinds.index("tool_result")
    call_event = [data for kind, data in events if kind == "tool_call"][0]
    assert call_event == {"id": "c1", "name": "echo", "arguments": '{"text": "x"}'}


def _echo_tool():
    from easycode.tools import Tool
    return Tool(name="echo", description="回显",
                parameters={"type": "object", "properties": {"text": {"type": "string"}}},
                run=lambda args, ctx: (f"echo:{args.get('text', '')}", {}))


def test_edit_details_carry_old_new_for_diff(tmp_path):
    (tmp_path / "code.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    registry = build_registry()
    result = registry.dispatch({"id": "c1", "function": {"name": "edit", "arguments":
        json.dumps({"path": "code.py", "old": "return 1", "new": "return 2"})}},
        ToolContext(workspace=tmp_path))
    assert result["ok"]
    assert result["details"]["old"] == "return 1" and result["details"]["new"] == "return 2"


def test_write_details_carry_before_snapshot(tmp_path):
    (tmp_path / "f.txt").write_text("line1\nline2\n", encoding="utf-8")
    registry = build_registry()
    result = registry.dispatch({"id": "c1", "function": {"name": "write", "arguments":
        json.dumps({"path": "f.txt", "content": "new\ncontent\n"})}}, ToolContext(workspace=tmp_path))
    details = result["details"]
    assert details["created"] is False
    assert details["before_lines"] == 2 and details["before_head"] == "line1\nline2"
    fresh = registry.dispatch({"id": "c2", "function": {"name": "write", "arguments":
        json.dumps({"path": "g.txt", "content": "x"})}}, ToolContext(workspace=tmp_path))
    assert fresh["details"]["created"] and fresh["details"]["before_lines"] == 0


def test_bash_details_carry_output_limited(tmp_path, monkeypatch):
    class FakeRunner:
        name = "local"
        def run_command(self, workspace, command, timeout, *, cancel=None, image=None):
            return {"exit_code": 0, "seconds": 0.1, "timed_out": False, "cancelled": False,
                    "output_limited": True, "stdout": "out", "stderr": "", "command": command}
    registry = build_registry({"bash"})
    result = registry.dispatch({"id": "c1", "function": {"name": "bash", "arguments":
        json.dumps({"command": "echo hi"})}}, ToolContext(workspace=tmp_path, runner=FakeRunner()))
    assert result["ok"] and result["details"]["output_limited"] is True


# ---- 模型设置 API ----

@pytest.fixture
def model_client(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYCODE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EASYCODE_ENV_PATH", str(tmp_path / ".env"))
    # 隔离真实 .env 的影响：EASYCODE_* 置空；
    # monkeypatch 注册过的键在 PUT 的 apply_to_process 改写后也会被还原。
    for key in ("EASYCODE_MODEL", "EASYCODE_BASE_URL", "EASYCODE_API_KEY"):
        monkeypatch.setenv(key, "")
    app = create_app()
    with TestClient(app) as testclient:
        yield testclient


def test_model_settings_roundtrip_and_masking(model_client):
    assert model_client.get("/api/model").json()["configured"] is False
    saved = model_client.put("/api/model", json={
        "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1/",
        "api_key": "test-key-never-public"}).json()
    assert saved["configured"] is True and saved["provider"] == "deepseek"
    assert "test-key-never-public" not in json.dumps(saved)
    assert saved["api_key_masked"].startswith("***")
    # 留空 api_key 表示沿用已保存密钥
    updated = model_client.put("/api/model", json={
        "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1"}).json()
    assert updated["configured"] is True and updated["api_key_masked"]
    from easycode.model_config import read_env_config
    values = read_env_config(_env_path(model_client))
    assert values["EASYCODE_MODEL"] == "deepseek-chat"
    assert "test-key-never-public" in values["EASYCODE_API_KEY"]  # 留空未覆盖密钥
    providers = model_client.get("/api/providers").json()["items"]
    assert {p["id"] for p in providers} >= {"deepseek", "dashscope", "custom"}


def _env_path(testclient):
    from easycode.settings import Settings
    return Settings().env_path


def test_model_test_connection_reports_by_status_only(model_client, monkeypatch):
    recorded = {}
    def fake_test(base_url, api_key, model, *, client=None, timeout=12):
        recorded.update({"base_url": base_url, "api_key": api_key, "model": model})
        return {"ok": False, "message": "密钥无效或未授权（HTTP 401）。", "latency_ms": 5}
    monkeypatch.setattr("easycode.model_config.test_connection", fake_test)
    response = model_client.post("/api/model/test", json={
        "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1",
        "api_key": "test-key-never-public"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and "401" in body["message"]
    assert recorded == {"base_url": "https://api.deepseek.com/v1",
                        "api_key": "test-key-never-public", "model": "deepseek-chat"}


def test_put_model_requires_a_key_when_none_saved(model_client):
    response = model_client.put("/api/model", json={
        "model": "m", "base_url": "https://api.invalid/v1", "api_key": ""})
    assert response.status_code == 400 and "API Key" in response.json()["detail"]


def test_pick_directory_reports_platform_limitation(model_client, monkeypatch):
    from easycode import directory_picker
    def fake_choose(initial):
        raise RuntimeError("当前平台暂不支持系统目录选择，可手动填写路径。")
    monkeypatch.setattr(directory_picker, "choose_directory", fake_choose)
    response = model_client.post("/api/pick-directory", json={"initial": ""})
    assert response.status_code in {400, 501}
    assert "目录选择" in response.json()["detail"]


def test_registry_blocks_consecutive_identical_calls(tmp_path):
    registry = build_registry({"read"})
    registry.register(_echo_tool())
    ctx = ToolContext(workspace=tmp_path)
    call = {"id": "c", "function": {"name": "echo", "arguments": '{"text": "same"}'}}
    for _ in range(4):
        assert registry.dispatch(call, ctx)["ok"]
    blocked = registry.dispatch(call, ctx)
    assert not blocked["ok"] and "重复相同调用" in blocked["output"]
    # 中间插入不同动作后计数重置，原调用恢复可执行
    registry.dispatch({"id": "c2", "function": {"name": "echo", "arguments": '{"text": "other"}'}}, ctx)
    assert registry.dispatch(call, ctx)["ok"]


def test_system_prompt_includes_shell_hint_by_backend(tmp_path):
    import os
    from easycode.prompt import system_prompt, shell_hint
    local = system_prompt(tmp_path, "local")
    docker = system_prompt(tmp_path, "docker")
    if os.name == "nt":
        assert "cmd" in local and "无需 cd" in local
    else:
        assert "POSIX" in local
    assert "/workspace" in docker and "加固容器" in docker
    assert shell_hint(None) == ""  # 未指定后端不给提示
