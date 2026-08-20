"""EasyCode T3：service/api 层——会话生命周期、SSE 数据源、steering/取消的 HTTP 契约。

模型客户端整体替换为 FakeClient（脚本化 turns），执行后端用 local（无 Docker 依赖）；
数据目录指向 tmp，保证与真实 .easycode 会话库完全隔离。
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from easycode.api import create_app


class FakeClient:
    """脚本化模型：前 N-1 轮调 read 工具，之后给最终回答。"""
    script_turns = 2
    instances = []

    def __init__(self, *, model, base_url, api_key, on_delta=None, max_tokens=0, temperature=0):
        self.on_delta = on_delta
        self.tokens = {"prompt": 5, "completion": 2}
        self.cached_tokens = 0
        self.cost_estimate = 0.001
        self.calls = 0
        FakeClient.instances.append(self)

    def query(self, messages, tools=None, cancel=None):
        self.calls += 1
        if self.on_delta is not None:
            self.on_delta("正在分析…", self.calls)
        if sum(1 for m in messages if m.get("role") == "tool") >= self.script_turns - 1:
            return {"role": "assistant", "content": "任务完成，已确认。"}
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{self.calls}", "type": "function",
             "function": {"name": "read", "arguments": json.dumps({"path": "a.txt"})}}]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYCODE_DATA_DIR", str(tmp_path / "easycode-data"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr("easycode.service.ChatCompletionsClient", FakeClient)
    FakeClient.instances = []
    app = create_app()
    with TestClient(app) as testclient:
        testclient.workspace = str(workspace)
        yield testclient


def wait_terminal(testclient, session_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        session = testclient.get(f"/api/sessions/{session_id}").json()
        if session["status"] not in {"queued", "running"}:
            return session
        time.sleep(0.1)
    raise AssertionError("会话未在时限内到达终态")


def test_session_lifecycle_runs_to_completed(client):
    response = client.post("/api/sessions", json={"cwd": client.workspace, "task": "看看 a.txt",
                                                  "backend": "local", "tools": ["read"]})
    assert response.status_code == 201
    session = response.json()
    assert session["status"] in {"queued", "running"}
    final = wait_terminal(client, session["id"])
    assert final["status"] == "completed"
    assert final["calls"] >= 1
    events = client.get(f"/api/sessions/{session['id']}/events").json()
    kinds = [event["kind"] for event in events]
    assert "assistant_message" in kinds and "tool_result" in kinds and "message_delta" in kinds
    finals = [event["data"]["content"] for event in events
              if event["kind"] == "assistant_message" and event["data"].get("content")]
    assert finals[-1] == "任务完成，已确认。"
    usage = [event["data"] for event in events if event["kind"] == "usage"]
    assert usage and usage[-1]["calls"] >= 2
    # 工具结果事件包含 details（给 UI 的分离通道）
    tool_events = [event["data"] for event in events if event["kind"] == "tool_result"]
    assert tool_events[0]["name"] == "read" and "details" in tool_events[0]


def test_index_and_config_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "EasyCode" in page.text
    config = client.get("/api/config").json()
    assert config["name"] == "EasyCode" and "model_configured" in config


def test_start_validation_rejects_bad_input(client):
    assert client.post("/api/sessions", json={"cwd": "Z:/no/such/dir", "task": "x",
                                              "backend": "local"}).status_code == 400
    assert client.post("/api/sessions", json={"cwd": client.workspace, "task": "x",
                                              "backend": "nope"}).status_code == 422
    assert client.post("/api/sessions", json={"cwd": client.workspace, "task": "x",
                                              "backend": "local", "tools": ["nope"]}).status_code == 400


def test_steer_rejected_after_terminal_and_cancel_is_idempotent(client):
    created = client.post("/api/sessions", json={"cwd": client.workspace, "task": "t",
                                                 "backend": "local", "tools": ["read"]}).json()
    session_id = created["id"]
    wait_terminal(client, session_id)
    blocked = client.post(f"/api/sessions/{session_id}/steer", json={"text": "晚到的插话"})
    assert blocked.status_code == 400 and "已结束" in blocked.json()["detail"]
    cancelled = client.post(f"/api/sessions/{session_id}/cancel", json={})
    assert cancelled.status_code == 200
    # 队列原语：运行中排队 → 取消清空（在 service 层直接验证）
    from easycode.settings import Settings
    from easycode.session import SessionStore
    store = SessionStore(Settings().data_dir / "sessions.sqlite3")
    store.create("manual", {"title": "t", "status": "running"})
    assert store.queue("manual", "插话") == 1
    store.clear_queued("manual")
    assert store.take_queued("manual") is None


def test_delete_removes_session_and_events(client):
    created = client.post("/api/sessions", json={"cwd": client.workspace, "task": "t",
                                                 "backend": "local", "tools": ["read"]}).json()
    wait_terminal(client, created["id"])
    assert client.delete(f"/api/sessions/{created['id']}").status_code == 200
    assert client.get(f"/api/sessions/{created['id']}").status_code == 404
    assert all(session["id"] != created["id"] for session in client.get("/api/sessions").json())
