"""EasyCode agent loop（pi-agent-core 对应层）。

pi 的语义：处理用户消息 → 执行 tool calls → 结果回喂 → 重复，直到模型回复不含
tool call。没有隐藏状态、不在背后注入用户看不见的内容；steering 在 turn 边界
注入（下一次模型调用前）；取消是一等公民；事件全量外发供 UI/CLI 消费。
预算（steps/cost/seconds）是可选的保险丝，默认宽松——与 pi 的差异仅在于
付费模型下保留一道显式的、可观测的开支闸门。
"""
from __future__ import annotations

import time


def agent_loop(*, messages, client, tools, ctx, on_event=lambda kind, data: None,
               cancel=None, steer_source=None, max_steps=200, max_cost=None, max_seconds=None):
    """就地追加 messages；返回 {"stop_reason": ..., "error"?: ...}。

    stop_reason: completed（模型不再调用工具）/ cancelled / max_steps / max_cost /
    max_seconds / provider_error。
    """
    started = time.monotonic()
    steps = 0

    def over_budget():
        if max_cost is not None and client.cost_estimate is not None and client.cost_estimate >= max_cost:
            return "max_cost"
        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            return "max_seconds"
        return None

    while True:
        if cancel is not None and cancel.is_set():
            on_event("done", {"stop_reason": "cancelled"})
            return {"stop_reason": "cancelled"}
        budget = over_budget()
        if budget:
            on_event("done", {"stop_reason": budget})
            return {"stop_reason": budget}
        # turn 边界：把运行中排队的用户插话注入下一轮上下文（pi 语义）。
        if steer_source is not None:
            pending = steer_source()
            if pending:
                on_event("steer_applied", {"text": pending})
                messages.append({"role": "user", "content": pending})
        try:
            message = client.query(messages, tools=tools.schemas(), cancel=cancel)
        except Exception as error:  # ProviderError 或任何传输异常：如实终态，不伪造完成
            on_event("error", {"message": str(error)})
            return {"stop_reason": "provider_error", "error": str(error)}
        messages.append(message)
        on_event("assistant_message", _public_message(message))
        on_event("usage", _usage(client))
        calls = message.get("tool_calls") or []
        if not calls:
            on_event("done", {"stop_reason": "completed"})
            return {"stop_reason": "completed"}
        for call in calls:
            if cancel is not None and cancel.is_set():
                on_event("done", {"stop_reason": "cancelled"})
                return {"stop_reason": "cancelled"}
            fn = call.get("function") or {}
            on_event("tool_call", {"id": call.get("id") or "", "name": fn.get("name"),
                                   "arguments": fn.get("arguments", "")})
            tool_started = time.monotonic()
            result = tools.dispatch(call, ctx)
            result["seconds"] = round(time.monotonic() - tool_started, 3)
            on_event("tool_result", result)
            messages.append({"role": "tool", "tool_call_id": call.get("id") or "",
                             "content": result["output"]})
        steps += 1
        if max_steps is not None and steps >= max_steps:
            on_event("done", {"stop_reason": "max_steps"})
            return {"stop_reason": "max_steps"}


def _public_message(message):
    return {"role": "assistant", "content": message.get("content") or "",
            "tool_calls": [{"id": call.get("id"), "name": (call.get("function") or {}).get("name"),
                            "arguments": (call.get("function") or {}).get("arguments", "")}
                           for call in message.get("tool_calls") or []],
            "aborted": bool(message.get("aborted"))}


def _usage(client):
    return {"prompt": client.tokens["prompt"], "completion": client.tokens["completion"],
            "cached": client.cached_tokens, "cost": client.cost_estimate, "calls": client.calls}
