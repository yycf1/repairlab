"""EasyCode CLI：run（无界面 pi 式循环）与 serve（web 工作台，T3 提供）。"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import uuid
from pathlib import Path

from easycode.core.loop import agent_loop
from easycode.prompt import system_prompt
from easycode.providers.openai_compat import ChatCompletionsClient
from easycode.session import SessionStore
from easycode.settings import Settings
from easycode.tools import ToolContext
from easycode.tools.builtins import build_registry
from easycode.runners import make_runner

SESSION_STATUS = {"completed": "completed", "provider_error": "error"}


def render_console(kind, data):
    if kind == "assistant_message":
        text = (data.get("content") or "").strip()
        if text:
            print(f"\n[assistant] {text}")
    elif kind == "tool_result":
        args = data.get("arguments")
        args_text = json.dumps(args, ensure_ascii=False)[:160] if args is not None else str(data.get("arguments_text", ""))[:160]
        print(f"\n[tool] {data['name']}({args_text})")
        for line in data.get("output", "").splitlines()[:40]:
            print("  " + line)
        if data.get("ok") is False:
            print("  [工具反馈错误，等待模型自纠]")
    elif kind == "steer_applied":
        print(f"\n[steer] 注入用户插话：{data['text'][:160]}")
    elif kind == "error":
        print(f"\n[error] {data['message']}", file=sys.stderr)
    elif kind == "done":
        print(f"\n[done] {data['stop_reason']}")


def cmd_run(args):
    settings = Settings()
    workspace = Path(args.cwd).resolve()
    if not workspace.is_dir():
        raise SystemExit(f"工作目录不存在：{workspace}")
    try:
        registry = build_registry({name.strip() for name in args.tools.split(",")} if args.tools else None)
    except ValueError as error:
        raise SystemExit(str(error))
    if not registry.names():
        raise SystemExit("没有可用工具。")
    try:
        runner = make_runner(args.backend, allow_local=args.backend == "local")
    except Exception as error:
        raise SystemExit(str(error))
    ctx = ToolContext(workspace=workspace, runner=runner, cancel=threading.Event(),
                      command_timeout=args.command_timeout)
    store = SessionStore(settings.data_dir / "sessions.sqlite3")
    session_id = uuid.uuid4().hex[:16]
    store.create(session_id, {"title": args.task[:80], "workspace": str(workspace),
                              "model": settings.model, "backend": args.backend,
                              "tools": registry.names(), "status": "running", "round": 1})

    def on_event(kind, data):
        store.event(session_id, kind, data)
        render_console(kind, data)

    if not settings.model_ready:
        raise SystemExit("未配置模型：请在 .env 设置 EASYCODE_MODEL / EASYCODE_BASE_URL / EASYCODE_API_KEY。")
    client = ChatCompletionsClient(model=settings.model, base_url=settings.base_url, api_key=settings.api_key)
    messages = [{"role": "system", "content": system_prompt(workspace, args.backend)},
                {"role": "user", "content": args.task}]
    result = None
    try:
        result = agent_loop(messages=messages, client=client, tools=registry, ctx=ctx,
                            on_event=on_event, cancel=ctx.cancel,
                            steer_source=lambda: store.take_queued(session_id),
                            max_steps=args.max_steps, max_cost=args.max_cost,
                            max_seconds=args.max_seconds)
    finally:
        stop = result["stop_reason"] if result else "error"
        store.update(session_id, status=SESSION_STATUS.get(stop, stop),
                     tokens=dict(client.tokens), cached_tokens=client.cached_tokens,
                     cost=client.cost_estimate, calls=client.calls)
        client.close()
    print(json.dumps({"session": session_id, **result}, ensure_ascii=False))
    return 0 if result["stop_reason"] == "completed" else 1


def main():
    parser = argparse.ArgumentParser(description="EasyCode — 极简编程 agent（pi 式循环 + 工具 + 沙箱 bash）")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="在指定目录无界面执行一个任务")
    run.add_argument("task", help="任务描述")
    run.add_argument("--cwd", default=".", help="工作目录（默认当前目录）")
    run.add_argument("--backend", choices=["docker", "local"], default="docker",
                     help="bash 执行后端：docker 一次性加固容器（默认）/ local 本机直跑（仅可信目录）")
    run.add_argument("--tools", default="",
                     help="逗号分隔的工具子集（默认 read,bash,edit,write；ls/grep 需显式开启）")
    run.add_argument("--max-steps", type=int, default=200, help="工具轮次保险丝（pi 无此旋钮，这里默认宽松）")
    run.add_argument("--max-cost", type=float, default=None, help="人民币成本上限（按牌价估算）")
    run.add_argument("--max-seconds", type=int, default=1800, help="总时限秒数")
    run.add_argument("--command-timeout", type=int, default=60, help="单条命令默认秒数")
    serve = commands.add_parser("serve", help="启动 web 工作台")
    serve.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    if args.command == "run":
        sys.exit(cmd_run(args))
    import uvicorn
    from easycode.api import create_app
    uvicorn.run(create_app(Settings()), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
