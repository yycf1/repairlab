"""EasyCode T7：工作台 2.0——会话重命名（PATCH）API 契约。

数据目录指向 tmp，与真实 .easycode 会话库完全隔离；会话行经 store 直建，
不拉起执行线程（rename 只动标题，不涉及模型与后端）。
"""
import pytest
from fastapi.testclient import TestClient

from easycode.api import create_app
from easycode.session import SessionStore
from easycode.settings import Settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYCODE_DATA_DIR", str(tmp_path / "easycode-data"))
    app = create_app()
    with TestClient(app) as testclient:
        yield testclient


def make_session(session_id="sessrename0001"):
    store = SessionStore(Settings().data_dir / "sessions.sqlite3")
    store.create(session_id, {"title": "旧标题", "task": "看一眼", "workspace": ".",
                              "backend": "docker", "tools": ["read"], "round": 1})
    return session_id


def test_rename_updates_title_only(client):
    session_id = make_session()
    response = client.patch(f"/api/sessions/{session_id}", json={"title": "  新标题  "})
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "新标题"  # 落库前去首尾空白
    assert body["task"] == "看一眼" and body["round"] == 1  # 其他字段不动
    assert client.get(f"/api/sessions/{session_id}").json()["title"] == "新标题"
    titles = [s["title"] for s in client.get("/api/sessions").json()]
    assert titles == ["新标题"]


def test_rename_missing_session_returns_404(client):
    assert client.patch("/api/sessions/nope", json={"title": "x"}).status_code == 404


def test_rename_rejects_blank_and_oversized(client):
    session_id = make_session()
    # 空串/超长由 pydantic 拦（422），纯空白由端点拦（400）
    assert client.patch(f"/api/sessions/{session_id}", json={"title": ""}).status_code == 422
    assert client.patch(f"/api/sessions/{session_id}", json={"title": "x" * 201}).status_code == 422
    assert client.patch(f"/api/sessions/{session_id}", json={"title": "   "}).status_code == 400
    assert client.get(f"/api/sessions/{session_id}").json()["title"] == "旧标题"
