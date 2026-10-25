import httpx
import pytest
from fastapi.testclient import TestClient

from repairlab.api import create_app
from repairlab.model_config import PROVIDERS, mask_key, read_env_config, write_env_config
from repairlab.model_config import test_connection as check_connection


@pytest.fixture
def isolated_env(settings, monkeypatch, tmp_path):
    env_path = tmp_path / "settings.env"
    monkeypatch.setenv("REPAIRLAB_ENV_PATH", str(env_path))
    for key in ("REPAIRLAB_MODEL", "REPAIRLAB_BASE_URL", "REPAIRLAB_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return env_path


def test_presets_are_openai_compatible_https():
    assert len(PROVIDERS) >= 6
    for provider in PROVIDERS:
        assert {"id", "name", "base_url", "models", "key_url"} <= set(provider)
        if provider["id"] != "custom":
            assert provider["base_url"].startswith("https://")


def test_env_roundtrip_preserves_unrelated_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# comment\nREPAIRLAB_DATA_DIR=E:/data\nREPAIRLAB_MODEL=old\n", encoding="utf-8")
    write_env_config(path, model="m1", base_url="https://api.invalid/v1", api_key="k1")
    text = path.read_text(encoding="utf-8")
    assert "# comment" in text and "REPAIRLAB_DATA_DIR=E:/data" in text
    values = read_env_config(path)
    assert values["REPAIRLAB_MODEL"] == "m1"
    assert values["REPAIRLAB_BASE_URL"] == "https://api.invalid/v1"
    assert values["REPAIRLAB_API_KEY"] == "k1"
    write_env_config(path, model="m2", base_url="https://api.invalid/v1", api_key="k1")
    assert read_env_config(path)["REPAIRLAB_MODEL"] == "m2"
    assert path.read_text(encoding="utf-8").count("REPAIRLAB_MODEL=") == 1


def test_mask_never_reveals_full_key():
    assert mask_key("sk-abcdef1234567890") == "***7890"
    assert mask_key("short") == "已设置"
    assert mask_key("") == ""


def test_connection_reports_status_without_echoing_body():
    def respond(request):
        assert request.headers["Authorization"] == "Bearer test-key-never-public"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = check_connection("https://api.invalid/v1", "test-key-never-public", "test-model", client=client)
    assert result["ok"] and result["latency_ms"] is not None

    def unauthorized(_):
        return httpx.Response(401, text="Bearer quota: test-key-never-public leaked")
    with httpx.Client(transport=httpx.MockTransport(unauthorized)) as client:
        result = check_connection("https://api.invalid/v1", "test-key-never-public", "test-model", client=client)
    assert not result["ok"] and "401" in result["message"]
    assert "test-key-never-public" not in result["message"]

    def broken(_):
        raise httpx.ConnectError("no route")
    with httpx.Client(transport=httpx.MockTransport(broken)) as client:
        result = check_connection("https://api.invalid/v1", "test-key-never-public", "test-model", client=client)
    assert not result["ok"] and result["latency_ms"] is None


def test_connection_accepts_reasoning_output_and_flags_truly_empty():
    def thinking(_):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "",
                                     "reasoning_content": "思考过程"}, "finish_reason": "length"}]})
    with httpx.Client(transport=httpx.MockTransport(thinking)) as client:
        result = check_connection("https://api.invalid/v1", "test-key-never-public", "thinker", client=client)
    assert result["ok"] and "思考型" in result["message"]

    def empty(_):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": ""}}]})
    with httpx.Client(transport=httpx.MockTransport(empty)) as client:
        result = check_connection("https://api.invalid/v1", "test-key-never-public", "quiet", client=client)
    assert not result["ok"] and "choices" in result["message"]


def test_settings_api_lifecycle(settings, isolated_env):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/settings/model").json()["configured"] is False
        assert client.get("/api/settings/providers").json()[0]["id"] == "deepseek"

        saved = client.put("/api/settings/model", json={
            "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-abcdef1234567890"})
        assert saved.status_code == 200
        body = saved.json()
        assert body["configured"] is True
        assert body["api_key_masked"] == "***7890"
        assert body["provider"] == "deepseek"
        assert "sk-abcdef1234567890" not in saved.text

        # Hot reload: the running process now passes model_ready without a restart.
        assert client.get("/api/config").json()["model_configured"] is True

        # Saving again without a key keeps the stored one.
        updated = client.put("/api/settings/model", json={
            "model": "deepseek-reasoner", "base_url": "https://api.deepseek.com/v1"}).json()
        assert updated["model"] == "deepseek-reasoner"
        assert updated["api_key_masked"] == "***7890"

        values = read_env_config(isolated_env)
        assert values["REPAIRLAB_API_KEY"] == "sk-abcdef1234567890"
        assert values["REPAIRLAB_MODEL"] == "deepseek-reasoner"


def test_settings_api_validation(settings, isolated_env):
    with TestClient(create_app(settings)) as client:
        assert client.put("/api/settings/model", json={
            "model": "m", "base_url": "ftp://api.invalid/v1", "api_key": "k"}).status_code == 400
        assert client.put("/api/settings/model", json={
            "model": "", "base_url": "https://api.invalid/v1"}).status_code == 422
        # Test endpoint requires a key when none is stored.
        assert client.post("/api/settings/model/test", json={
            "model": "m", "base_url": "https://api.invalid/v1"}).status_code == 400


def test_model_profiles_lifecycle(settings, isolated_env):
    with TestClient(create_app(settings)) as client:
        # 空配置 → 选择器为空；保存即登记为档案并激活
        assert client.get("/api/settings/models").json()["items"] == []
        client.put("/api/settings/model", json={
            "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1", "api_key": "sk-abcdef1234567890"})
        listing = client.get("/api/settings/models").json()
        assert [p["label"] for p in listing["items"]] == ["deepseek-chat"]
        assert listing["items"][0]["active"] is True
        assert "sk-abcdef1234567890" not in str(listing)

        # 保存第二份 → 两项并存，新保存的成为激活项
        client.put("/api/settings/model", json={
            "model": "glm-4.6", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": "sk-zhipu12345678"})
        listing = client.get("/api/settings/models").json()
        assert len(listing["items"]) == 2
        assert next(p for p in listing["items"] if p["active"])["model"] == "glm-4.6"

        # 切换回第一份 → .env 与运行中的进程立即跟随
        target = next(p for p in listing["items"] if p["model"] == "deepseek-chat")
        switched = client.post("/api/settings/models/activate", json={"id": target["id"]}).json()
        assert switched["model"] == "deepseek-chat"
        assert client.get("/api/config").json()["model"] == "deepseek-chat"
        values = read_env_config(isolated_env)
        assert values["REPAIRLAB_MODEL"] == "deepseek-chat"
        assert values["REPAIRLAB_API_KEY"] == "sk-abcdef1234567890"

        # 未知档案 → 404；重复保存同 base_url+model 只更新不重复登记
        assert client.post("/api/settings/models/activate", json={"id": "missing"}).status_code == 404
        client.put("/api/settings/model", json={
            "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1"})
        assert len(client.get("/api/settings/models").json()["items"]) == 2
