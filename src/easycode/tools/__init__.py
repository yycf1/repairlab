"""工具注册表与执行上下文（pi 式：output 给模型，details 给 UI）。"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class ToolContext:
    workspace: Path
    runner: object | None = None      # run_command(workspace, command, timeout, *, cancel, image)
    cancel: threading.Event | None = None
    command_timeout: int = 60
    image: str | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                                        # JSON Schema
    run: Callable[[dict, ToolContext], tuple[str, dict]]    # -> (给模型的 output, 给 UI 的 details)


class Registry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._last_signature = None
        self._repeat_count = 0

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def names(self):
        return list(self._tools)

    def schemas(self):
        return [{"type": "function",
                 "function": {"name": tool.name, "description": tool.description,
                              "parameters": tool.parameters}}
                for tool in self._tools.values()]

    def dispatch(self, call, ctx):
        """永不抛异常：坏调用变成模型可自纠的工具结果反馈（pi-agent-core 的校验职责）。

        连续相同的调用（中间无其他动作）达 5 次会被护栏拦下——这是重复调用守卫，
        中间插入任何其他动作都会重新计数。
        """
        fn = (call or {}).get("function") or {}
        name = fn.get("name", "")
        raw = fn.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except ValueError:
            return {"name": name, "arguments_text": str(raw)[:400], "ok": False,
                    "output": f"错误：{name} 的参数不是有效 JSON；请严格按 Schema 输出。",
                    "details": {"error": "invalid_arguments"}}
        tool = self._tools.get(name)
        if tool is None:
            return {"name": name, "arguments": args, "ok": False,
                    "output": f"错误：未知工具 {name}；可用工具：{'、'.join(self._tools) or '（无）'}。",
                    "details": {"error": "unknown_tool"}}
        signature = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)[:2000]}"
        if signature == self._last_signature:
            self._repeat_count += 1
        else:
            self._last_signature = signature
            self._repeat_count = 1
        if self._repeat_count >= 5:
            return {"name": name, "arguments": args, "ok": False,
                    "output": "已连续重复相同调用 5 次且没有变化；请换一种做法"
                              "（改命令、写脚本文件再运行、调整参数或换个工具），不要原样重试。",
                    "details": {"error": "repeated_call"}}
        try:
            output, details = tool.run(args, ctx)
        except Exception as error:  # 工具失败是反馈不是崩溃
            return {"name": name, "arguments": args, "ok": False,
                    "output": (f"工具执行出错：{error}")[:4000], "details": {}}
        return {"name": name, "arguments": args, "ok": True, "output": _clip(output), "details": details}


def _clip(text, cap=12000):
    if len(text) <= cap:
        return text
    marker = "\n[输出过长，中段已截断]\n"
    side = max(0, (cap - len(marker)) // 2)
    return text[:side] + marker + text[-side:]
