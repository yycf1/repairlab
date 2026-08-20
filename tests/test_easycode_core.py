"""EasyCode T1：pi 式循环、原生 tool_calls 客户端、steering 注入与 EASYCODE_* 配置。

循环契约（对齐 pi）：模型回复不含 tool_calls 即完成；工具结果以 role:tool 回喂；
坏调用（坏 JSON/未知工具）变成模型可自纠的工具反馈而不是崩溃；steering 在
turn 边界（下一次模型调用前）注入；取消/预算是显式可观测的停止原因。
"""
import json
import threading

import httpx
import pytest

from easycode.core.loop import agent_loop
from easycode.providers.openai_compat import ChatCompletionsClient, ProviderError
from easycode.session import SessionStore
from easycode.settings import Settings
from easycode.tools import Registry, Tool, ToolContext


class ScriptedClient:
    """按剧本回放 assistant 消息；记录每次收到的上下文供断言。"""

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []
        self.calls = 0
        self.tokens = {"prompt": 0, "completion": 0}
        self.cached_tokens = 0
        self.cost_estimate = None

    def query(self, messages, tools=None, cancel=None):
        self.seen.append([dict(message) for message in messages])
        turn = dict(self.turns[min(self.calls, len(self.turns) - 1)])
        self.calls += 1
        usage = turn.pop("_usage", None)
        if usage:
            self.tokens["prompt"] += usage.get("prompt_tokens", 0)
            self.tokens["completion"] += usage.get("completion_tokens", 0)
        return turn


def echo_registry():
    registry = Registry()
    registry.register(Tool(name="echo", description="回显文本",
                           parameters={"type": "object", "properties": {"text": {"type": "string"}},
                                       "required": ["text"]},
                           run=lambda args, ctx: (f"echo:{args.get('text', '')}",
                                                  {"len": len(args.get("text", ""))})))
    return registry


def workspace_ctx(tmp_path):
    return ToolContext(workspace=tmp_path, runner=None, cancel=threading.Event())


def test_loop_completes_when_model_stops_calling_tools(tmp_path):
    client = ScriptedClient([{"role": "assistant", "content": "任务完成了。"}])
    events = []
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "任务"}]
    result = agent_loop(messages=messages, client=client, tools=echo_registry(),
                        ctx=workspace_ctx(tmp_path), on_event=lambda kind, data: events.append((kind, data)))
    assert result == {"stop_reason": "completed"}
    assert messages[-1]["content"] == "任务完成了。"
    kinds = [kind for kind, _ in events]
    assert kinds == ["assistant_message", "usage", "done"]


def test_loop_executes_tools_and_feeds_results_back(tmp_path):
    client = ScriptedClient([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]},
        {"role": "assistant", "content": "搞定"},
    ])
    events = []
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "任务"}]
    result = agent_loop(messages=messages, client=client, tools=echo_registry(),
                        ctx=workspace_ctx(tmp_path), on_event=lambda kind, data: events.append((kind, data)))
    assert result["stop_reason"] == "completed"
    tool_message = messages[3]  # [system, user, assistant(tool_calls), tool, assistant(最终)]
    assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "call_1"
    assert tool_message["content"] == "echo:hi"
    tool_events = [data for kind, data in events if kind == "tool_result"]
    assert tool_events[0]["ok"] and tool_events[0]["output"] == "echo:hi"
    assert tool_events[0]["details"] == {"len": 2}
    # 第二次模型调用看到了工具结果
    assert client.seen[1][3]["content"] == "echo:hi"


def test_loop_stops_at_max_steps_without_calling_model_again(tmp_path):
    class AlwaysToolClient(ScriptedClient):
        def query(self, messages, tools=None, cancel=None):
            self.seen.append([dict(message) for message in messages])
            self.calls += 1
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call_{self.calls}", "type": "function",
                 "function": {"name": "echo", "arguments": '{"text": "x"}'}}]}

    client = AlwaysToolClient([])
    events = []
    result = agent_loop(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "t"}],
                        client=client, tools=echo_registry(), ctx=workspace_ctx(tmp_path),
                        on_event=lambda kind, data: events.append((kind, data)), max_steps=2)
    assert result["stop_reason"] == "max_steps"
    assert client.calls == 2  # 两次工具轮后不再询问模型
    assert [kind for kind, _ in events][-1] == "done"


def test_steering_is_injected_at_turn_boundary(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.create("s1", {"title": "t"})
    registry = echo_registry()

    def run_and_queue(args, ctx):
        store.queue("s1", "先看看参数校验")  # 用户在工具执行期间插话
        return "echo:a", {}

    registry._tools["echo"].run = run_and_queue
    client = ScriptedClient([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "echo", "arguments": '{"text": "a"}'}}]},
        {"role": "assistant", "content": "按插话完成了"},
    ])
    events = []
    agent_loop(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "任务"}],
               client=client, tools=registry, ctx=workspace_ctx(tmp_path),
               on_event=lambda kind, data: events.append((kind, data)),
               steer_source=lambda: store.take_queued("s1"))
    # 第二轮模型调用前：工具结果之后、紧贴着注入用户插话
    assert client.seen[1][-1] == {"role": "user", "content": "先看看参数校验"}
    assert [data for kind, data in events if kind == "steer_applied"] == [{"text": "先看看参数校验"}]
    assert store.take_queued("s1") is None  # 队列被原子弹出，不会重复注入


def test_invalid_arguments_and_unknown_tools_become_self_correcting_feedback(tmp_path):
    registry = echo_registry()
    registry.register(Tool(name="boom", description="总是失败",
                           parameters={"type": "object", "properties": {}},
                           run=lambda args, ctx: (_ for _ in ()).throw(ValueError("boom 位置炸了"))))
    ctx = workspace_ctx(tmp_path)
    bad_json = registry.dispatch({"id": "c1", "function": {"name": "echo", "arguments": "{bad"}}, ctx)
    assert not bad_json["ok"] and "JSON" in bad_json["output"]
    unknown = registry.dispatch({"id": "c2", "function": {"name": "nope", "arguments": "{}"}}, ctx)
    assert not unknown["ok"] and "未知工具" in unknown["output"]
    tool_error = registry.dispatch({"id": "c3", "function": {"name": "boom", "arguments": "{}"}}, ctx)
    assert not tool_error["ok"] and "boom" in tool_error["output"]  # 工具异常变反馈不崩溃


def test_cancel_stops_between_tool_executions(tmp_path):
    registry = echo_registry()
    cancel = threading.Event()
    def run(args, ctx):
        cancel.set()  # 第一个工具执行期间用户按下停止
        return "echo:x", {}
    registry._tools["echo"].run = run
    events = []
    result = agent_loop(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "t"}],
                        client=ScriptedClient([
                            {"role": "assistant", "content": "", "tool_calls": [
                                {"id": "c1", "type": "function",
                                 "function": {"name": "echo", "arguments": '{"text": "x"}'}},
                                {"id": "c2", "type": "function",
                                 "function": {"name": "echo", "arguments": '{"text": "y"}'}}]}]),
                        tools=registry, ctx=ToolContext(workspace=tmp_path, cancel=cancel), cancel=cancel,
                        on_event=lambda kind, data: events.append((kind, data)))
    assert result["stop_reason"] == "cancelled"
    tool_results = [data for kind, data in events if kind == "tool_result"]
    assert len(tool_results) == 1  # 第二个工具不再执行


def test_cost_budget_stops_before_next_model_call(tmp_path):
    class BudgetClient(ScriptedClient):
        def query(self, messages, tools=None, cancel=None):
            message = super().query(messages, tools, cancel)
            self.cost_estimate = 5.0  # 第一次调用后开销超过预算
            return message

    client = BudgetClient([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"text": "x"}'}}]},
    ])
    result = agent_loop(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "t"}],
                        client=client, tools=echo_registry(), ctx=workspace_ctx(tmp_path),
                        max_cost=1.0)
    assert result["stop_reason"] == "max_cost"
    assert client.calls == 1  # 预算检查在下次模型调用之前


def test_provider_error_is_an_honest_terminal(tmp_path):
    class BrokenClient(ScriptedClient):
        def query(self, messages, tools=None, cancel=None):
            raise ProviderError("模型接口返回 HTTP 401；未保存响应正文。")
    events = []
    result = agent_loop(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "t"}],
                        client=BrokenClient([]), tools=echo_registry(), ctx=workspace_ctx(tmp_path),
                        on_event=lambda kind, data: events.append((kind, data)))
    assert result["stop_reason"] == "provider_error" and "401" in result["error"]
    assert [kind for kind, _ in events] == ["error"]


# ---- ChatCompletionsClient：协议与传输（MockTransport，不打真实接口） ----

def make_client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return ChatCompletionsClient(model="qwen-test", base_url="https://api.invalid/v1",
                                 api_key="test-key-never-public",
                                 client=httpx.Client(transport=transport), **kwargs)


def completion_body(content="", tool_calls=None, usage=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}], "usage": usage or {}}


def test_client_parses_native_tool_calls_and_accounts_usage():
    seen = {}
    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["tools"] = json.loads(request.content)["tools"]
        return httpx.Response(200, json=completion_body(
            content="先读文件", usage={"prompt_tokens": 100, "completion_tokens": 10,
                                       "prompt_tokens_details": {"cached_tokens": 40}}))
    client = make_client(handler)
    message = client.query([{"role": "user", "content": "读一下"}],
                           tools=[{"type": "function", "function": {"name": "read"}}])
    assert seen["auth"] == "Bearer test-key-never-public"
    assert seen["tools"][0]["function"]["name"] == "read"
    assert message["content"] == "先读文件"
    assert client.tokens == {"prompt": 100, "completion": 10}
    assert client.cached_tokens == 40
    # qwen 牌价：(100-40)*0.8/1e6 + 40*0.2/1e6 + 10*2.0/1e6 = 7.6e-05
    assert abs(client.cost_estimate - 7.6e-05) < 1e-9


def test_client_retries_retryable_status_then_succeeds():
    calls = {"n": 0}
    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="upstream")
        return httpx.Response(200, json=completion_body(content="ok"))
    client = make_client(handler)
    message = client.query([{"role": "user", "content": "hi"}])
    assert message["content"] == "ok" and calls["n"] == 2


def test_client_error_message_never_carries_provider_body():
    def handler(request):
        return httpx.Response(401, text="invalid key: test-key-never-public")
    client = make_client(handler)
    with pytest.raises(ProviderError) as error:
        client.query([{"role": "user", "content": "hi"}])
    assert "401" in str(error.value) and "test-key-never-public" not in str(error.value)


def test_client_streaming_assembles_text_and_tool_call_fragments():
    body = "".join([
        'data: {"choices":[{"delta":{"content":"分析中"}}]}\n\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_9","type":"function",'
        '"function":{"name":"read","arguments":"{\\"pa"}}]}}]}\n\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"th\\":\\"a.py\\"}"}}]}}]}\n\n',
        'data: {"usage":{"prompt_tokens":7,"completion_tokens":3},"choices":[]}\n\n',
        'data: [DONE]\n\n',
    ]).encode()

    def handler(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    deltas = []
    client = make_client(handler, on_delta=lambda text, seq: deltas.append((text, seq)))
    message = client.query([{"role": "user", "content": "hi"}])
    assert message["content"] == "分析中"
    assert message["tool_calls"][0]["id"] == "call_9"
    assert message["tool_calls"][0]["function"] == {"name": "read", "arguments": '{"path":"a.py"}'}
    assert deltas and "".join(text for text, _ in deltas if text) == "分析中"
    assert client.tokens == {"prompt": 7, "completion": 3}


def test_settings_use_easycode_keys_only(monkeypatch):
    monkeypatch.delenv("EASYCODE_MODEL", raising=False)
    monkeypatch.delenv("EASYCODE_BASE_URL", raising=False)
    monkeypatch.delenv("EASYCODE_API_KEY", raising=False)
    settings = Settings()
    assert not settings.model_ready  # 未配置即不可用，不再回退旧变量名
    monkeypatch.setenv("EASYCODE_MODEL", "easy-model")
    monkeypatch.setenv("EASYCODE_BASE_URL", "https://easy.invalid/v1")
    monkeypatch.setenv("EASYCODE_API_KEY", "easy-key")
    fresh = Settings()
    assert fresh.model == "easy-model" and fresh.model_ready
