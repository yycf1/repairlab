"""会话编排：线程池驱动 agent_loop，事件落库，steering/取消即时生效。

与 pi 的会话模型对齐：工作区就是用户给出的真实目录（无快照副本），模型在
其中直接读写；bash 默认进一次性加固容器。会话行是无 schema JSON，事件流
即完整执行记录（SSE 断线续传依赖事件游标）。
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from easycode.core.loop import agent_loop
from easycode.prompt import system_prompt
from easycode.providers.openai_compat import ChatCompletionsClient
from easycode.session import SessionStore
from easycode.tools import ToolContext
from easycode.tools.builtins import build_registry
from easycode.runners import make_runner

ACTIVE_STATUS = {"queued", "running"}
TERMINAL_STATUS = {"completed", "error", "cancelled", "max_steps", "max_cost", "max_seconds", "interrupted"}


class SessionService:
    def __init__(self, settings):
        self.settings = settings
        self.store = SessionStore(settings.data_dir / "sessions.sqlite3")
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="easycode")
        self.cancels: dict[str, threading.Event] = {}
        self.store.store.recover_interrupted()  # 服务重启后，运行中会话如实标记为中断

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)

    def start(self, *, cwd, task, backend="docker", tools=None,
              max_steps=200, max_cost=None, max_seconds=1800, command_timeout=60):
        workspace = Path(cwd).resolve()
        if not workspace.is_dir():
            raise ValueError(f"工作目录不存在：{workspace}")
        try:
            registry = build_registry(set(tools) if tools else None)
        except ValueError as error:
            raise ValueError(str(error))
        try:
            runner = make_runner(backend, allow_local=backend == "local")
        except Exception as error:
            raise ValueError(f"执行后端不可用：{error}")
        if not self.settings.model_ready:
            raise ValueError("未配置模型：请在 .env 设置 EASYCODE_MODEL / EASYCODE_BASE_URL / EASYCODE_API_KEY。")
        session_id = uuid.uuid4().hex[:16]
        cancel = threading.Event()
        self.cancels[session_id] = cancel
        self.store.create(session_id, {
            "title": task.strip()[:80], "task": task, "workspace": str(workspace),
            "model": self.settings.model, "backend": backend, "tools": registry.names(),
            "status": "queued", "round": 1,
            "limits": {"max_steps": max_steps, "max_cost": max_cost,
                       "max_seconds": max_seconds, "command_timeout": command_timeout},
            "tokens": {"prompt": 0, "completion": 0}, "cached_tokens": 0,
            "cost": None, "calls": 0})
        self.pool.submit(self._run, session_id, workspace, task, registry, runner, cancel,
                         backend, max_steps, max_cost, max_seconds, command_timeout)
        return self.store.get(session_id)

    def continue_session(self, session_id, text):
        """终态会话续跑（pi 的 continue 语义）：同一会话历史继续，round+1，预算沿用。"""
        session = self.store.get(session_id)
        if session is None:
            raise KeyError(session_id)
        if session["status"] in ACTIVE_STATUS:
            raise ValueError("会话仍在运行中。")
        limits = session.get("limits") or {}
        try:
            registry = build_registry(set(session.get("tools") or []) or None)
        except ValueError as error:
            raise ValueError(str(error))
        try:
            runner = make_runner(session.get("backend", "docker"),
                                 allow_local=session.get("backend") == "local")
        except Exception as error:
            raise ValueError(f"执行后端不可用：{error}")
        if not self.settings.model_ready:
            raise ValueError("未配置模型：请在 .env 设置 EASYCODE_MODEL / EASYCODE_BASE_URL / EASYCODE_API_KEY。")
        messages = self._rebuild_messages(session)
        messages.append({"role": "user", "content": text})
        self.store.event(session_id, "user_message", {"text": text})
        cancel = threading.Event()
        self.cancels[session_id] = cancel
        self.store.update(session_id, status="queued", round=session.get("round", 1) + 1)
        self.pool.submit(self._execute, session_id, Path(session["workspace"]), messages,
                         registry, runner, cancel, session.get("backend", "docker"),
                         limits.get("max_steps", 200),
                         limits.get("max_cost"), limits.get("max_seconds", 1800),
                         limits.get("command_timeout", 60))
        return self.store.get(session_id)

    def _rebuild_messages(self, session):
        """从事件流重建协议消息；被取消/中断会话里未配对的 tool_calls 补占位结果，
        保证续跑轮的消息序列对兼容端合法。"""
        messages = [{"role": "system", "content": system_prompt(Path(session["workspace"]),
                                                               session.get("backend"))},
                    {"role": "user", "content": session.get("task") or session.get("title") or ""}]
        pending_ids: list[str] = []
        for event in self.store.events(session["id"]):
            kind, data = event["kind"], event["data"]
            if kind == "assistant_message":
                calls = data.get("tool_calls") or []
                message = {"role": "assistant", "content": data.get("content") or ""}
                if calls:
                    message["tool_calls"] = [
                        {"id": call["id"], "type": "function",
                         "function": {"name": call["name"], "arguments": call.get("arguments") or "{}"}}
                        for call in calls if call.get("id")]
                    pending_ids.extend(call["id"] for call in message["tool_calls"])
                messages.append(message)
            elif kind == "tool_result":
                call_id = pending_ids.pop(0) if pending_ids else "unknown"
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": data.get("output", "")})
            elif kind == "steer_applied":
                messages.append({"role": "user", "content": data.get("text", "")})
        for call_id in pending_ids:  # 中断时工具未执行：占位结果保持协议完整
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": "（会话在此被中断，该工具调用未执行。）"})
        return messages

    def _run(self, session_id, workspace, task, registry, runner, cancel, backend,
             max_steps, max_cost, max_seconds, command_timeout):
        messages = [{"role": "system", "content": system_prompt(workspace, backend)},
                    {"role": "user", "content": task}]
        self._execute(session_id, workspace, messages, registry, runner, cancel, backend,
                      max_steps, max_cost, max_seconds, command_timeout)

    def _execute(self, session_id, workspace, messages, registry, runner, cancel, backend,
                 max_steps, max_cost, max_seconds, command_timeout):
        self.store.update(session_id, status="running")
        ctx = ToolContext(workspace=workspace, runner=runner, cancel=cancel,
                          command_timeout=command_timeout)

        def on_delta(text, seq):
            # 流式正文增量；None 表示重试需清空重画。
            self.store.event(session_id, "message_delta", {"text": text, "seq": seq})

        client = ChatCompletionsClient(model=self.settings.model, base_url=self.settings.base_url,
                                       api_key=self.settings.api_key, on_delta=on_delta)

        def on_event(kind, data):
            self.store.event(session_id, kind, data)

        result = None
        try:
            result = agent_loop(messages=messages, client=client, tools=registry, ctx=ctx,
                                on_event=on_event, cancel=cancel,
                                steer_source=lambda: self.store.take_queued(session_id),
                                max_steps=max_steps, max_cost=max_cost, max_seconds=max_seconds)
        except Exception as error:  # 编排层兜底；循环内部已捕获 provider 异常
            self.store.event(session_id, "error", {"message": str(error)})
            result = {"stop_reason": "provider_error", "error": str(error)}
        finally:
            stop = result["stop_reason"] if result else "provider_error"
            status = {"completed": "completed", "provider_error": "error"}.get(stop, stop)
            self.store.update(session_id, status=status, tokens=dict(client.tokens),
                              cached_tokens=client.cached_tokens, cost=client.cost_estimate,
                              calls=client.calls)
            client.close()
            self.cancels.pop(session_id, None)

    def steer(self, session_id, text):
        session = self.store.get(session_id)
        if session is None:
            raise KeyError(session_id)
        if session["status"] not in ACTIVE_STATUS:
            raise ValueError("会话已结束；结束后的新需求请新建会话。")
        depth = self.store.queue(session_id, text)
        self.store.event(session_id, "steer_queued", {"text": text, "depth": depth})
        return depth

    def cancel(self, session_id):
        event = self.cancels.get(session_id)
        if event is not None:
            event.set()
        self.store.clear_queued(session_id)  # 取消即停止，排队插话一并丢弃
