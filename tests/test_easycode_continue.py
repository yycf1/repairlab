"""EasyCode T4：continue 续跑（事件流重建协议消息）、中断占位、JSONL 导出。

pi 的 continue 语义：同一会话带着完整历史继续，round+1；重建必须产出对兼容端
合法的消息序列——被取消/中断的会话里未配对的 tool_calls 补占位工具结果。
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from easycode.api import create_app


class ScriptedTurnsClient:
    """多阶段脚本：记录每次收到的上下文，供断言续跑轮是否带着完整历史。"""
    instances = []

    def __init__(self, *, model, base_url, api_key, on_delta=None, max_tokens=0, temperature=0):
        self.on_delta = on_delta
        self.tokens = {"prompt": 5, "completion": 2}
        self.cached_tokens = 0
        self.cost_estimate = 0.001
        self.calls = 0
        ScriptedTurnsClient.instances.append(self)

    def query(self, messages, tools=None, cancel=None):
        self.calls += 1
        self.last_messages = [dict(m) for m in messages]
        rounds = sum(1 for m in messages if m.get("role") == "tool")
        if self.phase == 1 and rounds >= 1:
            return {"role": "assistant", "content": "第一轮完成。"}
        if self.phase == 2 and rounds >= 1:
            return {"role": "assistant", "content": "第二轮完成。"}
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{self.calls}", "type": "function",
             "function": {"name": "read", "arguments": json.dumps({"path": "a.txt"})}}]}


def wait_terminal(testclient, session_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        session = testclient.get(f"/api/sessions/{session_id}").json()
        if session["status"] not in {"queued", "running"}:
            return session
        time.sleep(0.1)
    raise AssertionError("会话未在时限内到达终态")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYCODE_DATA_DIR", str(tmp_path / "easycode-data"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr("easycode.service.ChatCompletionsClient", ScriptedTurnsClient)
    ScriptedTurnsClient.instances = []
    ScriptedTurnsClient.phase = 1
    app = create_app()
    with TestClient(app) as testclient:
        testclient.workspace = str(workspace)
        yield testclient


def test_continue_carries_full_history_in_same_session(client):
    created = client.post("/api/sessions", json={"cwd": client.workspace, "task": "第一轮任务",
                                                 "backend": "local", "tools": ["read"]}).json()
    first = wait_terminal(client, created["id"])
    assert first["status"] == "completed" and first["round"] == 1

    ScriptedTurnsClient.phase = 2
    continued = client.post(f"/api/sessions/{created['id']}/continue", json={"text": "请接着做第二轮"})
    assert continued.status_code == 200
    final = wait_terminal(client, created["id"])
    assert final["status"] == "completed" and final["round"] == 2
    # 续跑实例的第一次调用看到了：system + 任务 + 第一轮助手消息/工具结果 + 新指令
    continued_client = ScriptedTurnsClient.instances[-1]
    seen = continued_client.last_messages
    roles = [m["role"] for m in seen]
    assert roles[0] == "system" and roles[1] == "user"
    assert "第一轮完成。" in json.dumps(seen, ensure_ascii=False)
    assert seen[-1] == {"role": "user", "content": "请接着做第二轮"}
    assert sum(1 for m in seen if m["role"] == "tool") >= 1
    # 事件流记录了续跑用户消息
    events = client.get(f"/api/sessions/{created['id']}/events").json()
    user_messages = [e["data"]["text"] for e in events if e["kind"] == "user_message"]
    assert user_messages == ["请接着做第二轮"]


def test_continue_rejected_while_running(client):
    created = client.post("/api/sessions", json={"cwd": client.workspace, "task": "t",
                                                 "backend": "local", "tools": ["read"]}).json()
    deadline = time.time() + 10
    while time.time() < deadline:
        session = client.get(f"/api/sessions/{created['id']}").json()
        if session["status"] == "running":
            break
        time.sleep(0.05)
    response = client.post(f"/api/sessions/{created['id']}/continue", json={"text": "插话"})
    assert response.status_code == 400 and "仍在运行" in response.json()["detail"]
    wait_terminal(client, created["id"])


def test_rebuild_stubs_unpaired_tool_calls_after_interruption(client):
    """被取消的会话可能存在“assistant 发起工具调用但未执行”的残缺序列；
    continue 重建时必须补占位工具结果，否则兼容端拒收。"""
    created = client.post("/api/sessions", json={"cwd": client.workspace, "task": "t",
                                                 "backend": "local", "tools": ["read"]}).json()
    session_id = created["id"]
    wait_terminal(client, session_id)
    # 直接注入一段残缺历史：assistant 发起两个工具调用，只有一个工具结果
    from easycode.settings import Settings
    from easycode.session import SessionStore
    store = SessionStore(Settings().data_dir / "sessions.sqlite3")
    store.event(session_id, "assistant_message", {
        "content": "", "tool_calls": [
            {"id": "pair-1", "name": "read", "arguments": '{"path": "a.txt"}'},
            {"id": "orphan-2", "name": "read", "arguments": '{"path": "b.txt"}'}]})
    store.event(session_id, "tool_result", {"name": "read", "output": "hello\n", "details": {}})
    ScriptedTurnsClient.phase = 2
    ScriptedTurnsClient.instances = []
    response = client.post(f"/api/sessions/{session_id}/continue", json={"text": "从中断处继续"})
    assert response.status_code == 200
    wait_terminal(client, session_id)
    rebuilt = ScriptedTurnsClient.instances[-1].last_messages
    tool_results = [m for m in rebuilt if m["role"] == "tool"]
    ids = {m["tool_call_id"] for m in tool_results}
    assert {"pair-1", "orphan-2"} <= ids  # 缺失的 orphan-2 得到占位结果
    orphan = next(m for m in tool_results if m["tool_call_id"] == "orphan-2")
    assert "中断" in orphan["content"]


def test_export_is_documented_jsonl(client):
    created = client.post("/api/sessions", json={"cwd": client.workspace, "task": "导出测试",
                                                 "backend": "local", "tools": ["read"]}).json()
    wait_terminal(client, created["id"])
    response = client.get(f"/api/sessions/{created['id']}/export")
    assert response.status_code == 200
    assert "jsonl" in response.headers["content-disposition"]
    lines = [json.loads(line) for line in response.text.strip().splitlines()]
    assert lines[0]["kind"] == "session" and lines[0]["data"]["task"] == "导出测试"
    kinds = {line["kind"] for line in lines[1:]}
    assert {"assistant_message", "tool_result", "done"} <= kinds
