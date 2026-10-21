from fastapi.testclient import TestClient
from repairlab.api import create_app
from repairlab.model import ScriptedModel
from repairlab.models import StartRequest


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
