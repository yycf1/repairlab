import pytest
from fastapi.testclient import TestClient

from repairlab.api import create_app


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def report(ready=True):
    return {"ready": ready, "backend": "local", "checked_at": 1730000000,
            "checks": [{"id": "snapshot", "status": "pass" if ready else "error",
                        "label": "仓库快照", "detail": "4 个文本文件。" if ready else "目录不可读。"}]}


def test_check_session_persists_as_history(client):
    created = client.post("/api/check-sessions", json={
        "fixture": "boundary-discount", "backend": "local", "collect": False, "report": report()})
    assert created.status_code == 200
    task = created.json()
    assert task["task_kind"] == "check"
    assert task["status"] == "checked"
    assert task["issue"] == "检查环境"
    # 会话出现在任务历史里，可重新打开
    listed = [t for t in client.get("/api/tasks").json() if t["id"] == task["id"]]
    assert listed and listed[0]["status"] == "checked"
    events = client.get(f"/api/tasks/{task['id']}/events").json()
    assert [e["kind"] for e in events] == ["preflight"]
    assert events[0]["data"]["ready"] is True
    assert events[0]["data"]["checks"][0]["label"] == "仓库快照"


def test_check_session_rejects_bad_report_and_unknown_fixture(client):
    assert client.post("/api/check-sessions", json={
        "fixture": "boundary-discount", "backend": "local", "collect": False,
        "report": {"ready": True, "checks": "not-a-list"}}).status_code == 400
    assert client.post("/api/check-sessions", json={
        "fixture": "no-such-workspace", "backend": "local", "collect": False,
        "report": report()}).status_code == 400


def test_check_session_blocks_follow_up(client):
    created = client.post("/api/check-sessions", json={
        "fixture": "boundary-discount", "backend": "local", "collect": True, "report": report()}).json()
    refused = client.post(f"/api/tasks/{created['id']}/follow-up", json={"text": "继续修复"})
    assert refused.status_code == 400
    assert "不支持追加指令" in refused.json()["detail"]
