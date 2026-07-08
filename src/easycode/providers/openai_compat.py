"""OpenAI 兼容 Chat Completions 客户端：原生 tool_calls、流式、可中断。

对齐 pi 的设计教训：取消与部分结果是一等公民（流式中途取消返回已收到的部分）；
供应商错误正文与认证头绝不落库或外显（只报 HTTP 状态）；token 与费用是尽力估算
（牌价表未命中的模型不估算也不拦截）。
"""
from __future__ import annotations

import json
import time

import httpx

from easycode.model_config import cost_of, pricing_for_model

RETRY_STATUSES = {429, 500, 502, 503}


class ProviderError(RuntimeError):
    """传输/协议层失败；消息不携带供应商响应正文。"""


class _RetryStatus(Exception):
    def __init__(self, status):
        super().__init__(str(status))
        self.status = status


class ChatCompletionsClient:
    """一个实例绑定一个模型配置；usage 与费用跨调用累计在实例上。"""

    def __init__(self, *, model, base_url, api_key, client=None, on_delta=None,
                 max_tokens=4096, temperature=0):
        self.model = model
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.on_delta = on_delta  # on_delta(text, seq) 流式正文增量；None = 非流式
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.client = client or httpx.Client(timeout=httpx.Timeout(180, connect=10))
        self.pricing = pricing_for_model(model)
        self.tokens = {"prompt": 0, "completion": 0}
        self.cached_tokens = 0
        self.cost_estimate = None
        self.calls = 0
        self._streamed = False

    def query(self, messages, tools=None, cancel=None):
        """一次补全；返回 assistant 消息 dict（有工具调用带原生 tool_calls，否则只有 content）。"""
        self.calls += 1
        payload = {"model": self.model, "messages": messages,
                   "temperature": self.temperature, "max_tokens": self.max_tokens}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        usage, content, tool_calls, aborted = {}, "", [], False
        for attempt in range(2):
            self._streamed = False
            try:
                if self.on_delta is None:
                    body = self._post(payload)
                    usage = body.get("usage") or {}
                    message = (body.get("choices") or [{}])[0].get("message") or {}
                    content = message.get("content") or ""
                    tool_calls = _normalize_calls(message.get("tool_calls") or [])
                    aborted = bool(cancel and cancel.is_set())
                else:
                    content, tool_calls, usage, aborted = self._stream(payload, cancel)
            except _RetryStatus as retry:
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise ProviderError(f"模型接口返回 HTTP {retry.status}；未保存响应正文。") from None
            except httpx.HTTPError:
                if attempt == 0:
                    self._reset_stream()
                    time.sleep(0.5)
                    continue
                raise ProviderError("模型连接失败；请检查接口地址、网络与超时配置。") from None
            break
        self._account(usage)
        message = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        if aborted:
            message["aborted"] = True
        return message

    def close(self):
        self.client.close()

    def _headers(self):
        return {"Authorization": "Bearer " + self.api_key}

    def _post(self, payload):
        response = self.client.post(self.base_url + "/chat/completions",
                                    headers=self._headers(), json=payload)
        if response.status_code in RETRY_STATUSES:
            raise _RetryStatus(response.status_code)
        if response.is_error:
            raise ProviderError(f"模型接口返回 HTTP {response.status_code}；未保存响应正文。")
        return response.json()

    def _stream(self, payload, cancel):
        """SSE 增量：正文按节流回调外发，工具调用参数静默拼装；取消返回部分结果。"""
        parts, pending, seq = [], [], 0
        pending_calls, last_flush, usage = {}, time.monotonic(), {}
        aborted = False
        with self.client.stream("POST", self.base_url + "/chat/completions", headers=self._headers(),
                                json={**payload, "stream": True,
                                      "stream_options": {"include_usage": True}}) as response:
            if response.status_code in RETRY_STATUSES:
                raise _RetryStatus(response.status_code)
            if response.is_error:
                raise ProviderError(f"模型接口返回 HTTP {response.status_code}；未保存响应正文。")
            for line in response.iter_lines():
                if cancel is not None and cancel.is_set():
                    aborted = True
                    break
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                if not line.startswith("data:"):
                    continue
                payload_text = line[5:].strip()
                if payload_text == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload_text)
                except ValueError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                text = delta.get("content")
                if text:
                    parts.append(text)
                    pending.append(text)
                    now = time.monotonic()
                    if now - last_flush >= 0.2 or sum(len(item) for item in pending) >= 240:
                        seq += 1
                        self._streamed = True
                        self.on_delta("".join(pending), seq)
                        pending, last_flush = [], now
                for slot in delta.get("tool_calls") or []:
                    item = pending_calls.setdefault(slot.get("index", len(pending_calls)),
                                                    {"id": "", "name": "", "arguments": ""})
                    if slot.get("id"):
                        item["id"] = slot["id"]
                    fn = slot.get("function") or {}
                    if fn.get("name"):
                        item["name"] += fn["name"]
                    if fn.get("arguments"):
                        item["arguments"] += fn["arguments"]
        if pending and not aborted:
            seq += 1
            self._streamed = True
            self.on_delta("".join(pending), seq)
        tool_calls = [{"id": item["id"] or f"call_{index}", "type": "function",
                       "function": {"name": item["name"], "arguments": item["arguments"] or "{}"}}
                      for index, item in sorted(pending_calls.items())]
        return "".join(parts), tool_calls, usage, aborted

    def _reset_stream(self):
        if self.on_delta is not None and self._streamed:
            self.on_delta(None, 0)  # 重试会重复输出：让前端清掉已渲染的增量

    def _account(self, usage):
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0
        prompt_delta = usage.get("prompt_tokens", 0) or 0
        completion_delta = usage.get("completion_tokens", 0) or 0
        self.tokens["prompt"] += prompt_delta
        self.tokens["completion"] += completion_delta
        self.cached_tokens += int(cached)
        if self.pricing is not None:
            self.cost_estimate = (self.cost_estimate or 0.0) \
                + cost_of(self.pricing, prompt_delta, completion_delta, int(cached))


def _normalize_calls(raw_calls):
    calls = []
    for call in raw_calls or []:
        fn = call.get("function") or {}
        calls.append({"id": call.get("id") or f"call_{len(calls)}", "type": "function",
                      "function": {"name": fn.get("name", ""),
                                   "arguments": fn.get("arguments") or "{}"}})
    return calls
