from fastapi.testclient import TestClient
from repairlab.api import create_app
from repairlab.model import ScriptedModel
from repairlab.models import Limits, StartRequest


def test_api_history_sse_and_artifacts(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert len(client.get("/api/fixtures").json()) == 3
        assert client.post("/api/tasks", json={"mode": "model", "backend": "local", "allow_local": True}).status_code == 400
        assert client.post("/api/tasks", json={"backend": "local"}).status_code == 400
        assert client.post("/api/tasks", json={"fixture": "../../private"}).status_code == 422
        task = client.app.state.service.start(StartRequest(backend="local", allow_local=True), background=False,
            model_override=ScriptedModel([{"action": {"tool": "finish", "reason": "unresolved", "summary": "Need more evidence"}}]))
        task_id = task["id"]
        events = client.get(f"/api/tasks/{task_id}/events").json()
        cursor = events[-2]["id"]
        response = client.get(f"/api/tasks/{task_id}/stream", headers={"Last-Event-ID": str(cursor)})
        assert f"id: {events[-1]['id']}" in response.text
        assert f"id: {cursor}\n" not in response.text
        assert client.get(f"/api/tasks/{task_id}/artifacts/report").status_code == 200
        assert client.get(f"/api/tasks/{task_id}/artifacts/patch").status_code == 404
        assert task["status"] == "blocked"
        assert client.get(f"/api/tasks/{task_id}/artifacts/unknown").status_code == 404
        assert client.get("/api/tasks/invalid-id").status_code == 404
        assert len(client.get("/api/tasks").json()) == 1


def test_nonlocal_host_is_rejected(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/", headers={"Host": "external.invalid"}).status_code == 400


def test_resume_endpoint_validates_state(settings):
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/tasks/0123456789abcdef/resume").status_code == 404
        task = client.app.state.service.start(StartRequest(backend="local", allow_local=True), background=False,
            model_override=ScriptedModel([{"action": {"tool": "finish", "reason": "unresolved", "summary": "No patch"}}]))
        assert task["status"] == "blocked"
        # Terminal tasks carry no resume point; the endpoint explains instead of resuming.
        response = client.post(f"/api/tasks/{task['id']}/resume")
        assert response.status_code == 400
        assert "检查点" in response.json()["detail"]


def test_task_list_filters_by_fixture(settings):
    with TestClient(create_app(settings)) as client:
        service = client.app.state.service
        for fixture in ("boundary-discount", "csv-tags"):
            service.start(StartRequest(fixture=fixture, backend="local", allow_local=True), background=False,
                          model_override=ScriptedModel([{"action": {"tool": "finish", "reason": "unresolved", "summary": "x"}}]))
        all_tasks = client.get("/api/tasks").json()
        assert len(all_tasks) == 2
        filtered = client.get("/api/tasks", params={"fixture": "csv-tags"}).json()
        assert [t["fixture"] for t in filtered] == ["csv-tags"]


def test_workspace_browser_endpoints(settings):
    with TestClient(create_app(settings)) as client:
        task = client.app.state.service.start(StartRequest(backend="local", allow_local=True, limits=Limits(max_steps=2)),
            background=False, model_override=ScriptedModel([{"action": {"tool": "list_files"}}]))
        files = client.get(f"/api/tasks/{task['id']}/workspace/files")
        assert files.status_code == 200
        paths = [f["path"] for f in files.json()["files"]]
        assert "pricing.py" in paths and "tests/test_pricing.py" in paths
        content = client.get(f"/api/tasks/{task['id']}/workspace/file", params={"path": "pricing.py"})
        assert content.status_code == 200 and "quantity" in content.json()["text"]
        assert client.get(f"/api/tasks/{task['id']}/workspace/file",
                          params={"path": "../baseline/pricing.py"}).status_code == 400
        assert client.get(f"/api/tasks/{task['id']}/workspace/file",
                          params={"path": "tests/test_pricing.py"}).status_code == 200
        assert client.get("/api/tasks/0123456789abcdef/workspace/files").status_code == 404
        # 任务记录创建于工作区快照功能之前：没有 repository_profile 时返回可读的 404，而不是 500。
        client.app.state.service.store.update(task["id"], repository_profile=None)
        legacy_files = client.get(f"/api/tasks/{task['id']}/workspace/files")
        assert legacy_files.status_code == 404 and "未记录文件清单" in legacy_files.json()["detail"]
        legacy_file = client.get(f"/api/tasks/{task['id']}/workspace/file", params={"path": "pricing.py"})
        assert legacy_file.status_code == 404
        follow_up = client.post(f"/api/tasks/{task['id']}/follow-up", json={"text": ""})
        assert follow_up.status_code == 422  # empty instruction rejected by the request model
