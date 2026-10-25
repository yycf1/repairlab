import pytest
from fastapi.testclient import TestClient

from repairlab.api import create_app


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def check_session(client):
    created = client.post("/api/check-sessions", json={
        "fixture": "boundary-discount", "backend": "local", "collect": False,
        "report": {"ready": True, "backend": "local", "checked_at": 1730000000,
                   "checks": [{"id": "snapshot", "status": "pass", "label": "仓库快照", "detail": "ok"}]}})
    assert created.status_code == 200
    return created.json()


def test_task_delete_removes_history_events_and_workspace(settings, client):
    task = check_session(client)
    task_dir = settings.data_dir / "tasks" / task["id"]
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "marker.txt").write_text("workspace", encoding="utf-8")

    assert client.delete(f"/api/tasks/{task['id']}").status_code == 200
    assert client.get(f"/api/tasks/{task['id']}").status_code == 404
    assert client.get(f"/api/tasks/{task['id']}/events").status_code == 404
    assert not task_dir.exists()
    # 重复删除 → 任务已不存在
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 404


def test_task_delete_rejects_malformed_id(client):
    assert client.delete("/api/tasks/not-a-valid-id").status_code == 404
