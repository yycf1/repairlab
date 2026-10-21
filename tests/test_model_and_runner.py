import json
import sys
import threading
import httpx
import pytest
from pydantic import ValidationError
from repairlab.model import CompatibleModel, parse_decision
from repairlab.models import Limits
from repairlab.runners import DockerRunner, LocalRunner, ExecutionUnavailable, process
from repairlab._vendor.mini.exceptions import FormatError


def test_json_contract():
    assert parse_decision('```json\n{"action":{"tool":"list_files"}}\n```').action.tool == "list_files"
    with pytest.raises(ValidationError):
        parse_decision('{"action":{"tool":"edit_file","path":"code.py"}}')


def configured(settings, monkeypatch):
    monkeypatch.setenv("REPAIRLAB_MODEL", "test-model")
    monkeypatch.setenv("REPAIRLAB_BASE_URL", "https://model.invalid/v1/")
    monkeypatch.setenv("REPAIRLAB_API_KEY", "test-key-never-public")
    return settings


def test_model_protocol_usage_and_context(settings, monkeypatch):
    settings = configured(settings, monkeypatch)
    seen = []
    def respond(request):
        seen.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test-key-never-public"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"action":{"tool":"list_files"}}'}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = CompatibleModel(settings, Limits(context_chars=4000), client=client)
        model.state = {"plan": ["retain plan"]}
        messages = [{"role": "system", "content": "Required instructions"}, {"role": "user", "content": "issue"}]
        messages += [{"role": "user", "content": "x" * 12000} for _ in range(20)]
        result = model.query(messages)
        assert result["extra"]["actions"][0]["tool"] == "list_files"
        assert model.tokens == {"prompt": 10, "completion": 5}
        assert sum(len(m["content"]) for m in seen[0]["messages"]) <= 4000
        assert "retain plan" in json.dumps(seen[0])
        assert "test-key-never-public" not in json.dumps(settings.public())
        assert "test-key-never-public" not in json.dumps(model.serialize())


def test_invalid_response_feedback(settings, monkeypatch):
    settings = configured(settings, monkeypatch)
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": []})))
    model = CompatibleModel(settings, Limits(), client=client)
    with pytest.raises(FormatError):
        model.query([{"role": "system", "content": "rules"}, {"role": "user", "content": "issue"}])
    model.close()


def test_schema_feedback_identifies_bad_fields_without_echoing_input(settings, monkeypatch):
    settings = configured(settings, monkeypatch)
    content = json.dumps({"action": {"tool": "list_files", "arguments": {"private": "test-key-never-public"}}})
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "choices": [{"message": {"content": content}}]})))
    model = CompatibleModel(settings, Limits(), client=client)
    try:
        with pytest.raises(FormatError) as error:
            model.query([{"role": "system", "content": "rules"}, {"role": "user", "content": "inspect"}])
        feedback = error.value.messages[0]["content"]
        assert "action.arguments" in feedback and "extra_forbidden" in feedback
        assert "test-key-never-public" not in feedback and "private" not in feedback
    finally:
        model.close()


def test_long_memory_stays_valid_json_and_issue_is_preserved(settings, monkeypatch):
    settings = configured(settings, monkeypatch)
    captured = []
    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"action":{"tool":"list_files"}}'}}]})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    model = CompatibleModel(settings, Limits(context_chars=12000), client=client)
    model.state = {"plan": ["step" * 100] * 12, "hypothesis": {"summary": "evidence" * 100}}
    issue = "required behavior " * 300
    messages = [{"role": "system", "content": "instructions"}, {"role": "user", "content": issue}]
    messages += [{"role": "user", "content": "logs" * 4000} for _ in range(12)]
    model.query(messages)
    sent = captured[0]["messages"]
    assert sent[1]["content"] == issue
    memory = json.loads(sent[2]["content"])["controller_state"]
    assert memory["hypothesis"]["summary"] == "evidence" * 100
    assert sum(len(item["content"]) for item in sent) <= 12000
    model.close()


def test_provider_error_body_is_not_exposed(settings, monkeypatch):
    settings = configured(settings, monkeypatch)
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401, text="test-key-never-public")))
    model = CompatibleModel(settings, Limits(), client=client)
    with pytest.raises(RuntimeError) as error:
        model.query([{"role": "system", "content": "rules"}, {"role": "user", "content": "issue"}])
    assert "test-key-never-public" not in str(error.value)
    model.close()


def test_local_execution_requires_opt_in():
    with pytest.raises(ExecutionUnavailable):
        LocalRunner()


def test_docker_command_has_constraints(tmp_path):
    argv = DockerRunner.command(tmp_path, ["tests"], "repairlab-python:latest", "test-container")
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--pull") + 1] == "never"
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in argv
    assert argv[argv.index("--user") + 1] == "10001:10001"
    assert "--memory" in argv and "--pids-limit" in argv
    assert "readonly" in argv[argv.index("--mount") + 1]
    assert not any("KEY=" in arg for arg in argv)


def test_process_timeout_and_cancel(tmp_path):
    result = process([sys.executable, "-c", "import time;time.sleep(20)"], cwd=tmp_path, timeout=0.2)
    assert result["timed_out"] and result["seconds"] < 5
    cancel = threading.Event()
    cancel.set()
    result = process([sys.executable, "-c", "import time;time.sleep(20)"], cwd=tmp_path, timeout=30, cancel=cancel)
    assert result["cancelled"] and result["seconds"] < 5


def test_process_limits_output_even_when_program_exits_quickly(tmp_path):
    result = process([sys.executable, "-c", "print('x'*2100000)"], cwd=tmp_path, timeout=5)
    assert result["output_limited"]
    assert len(result["stdout"]) <= 12000
