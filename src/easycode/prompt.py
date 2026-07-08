"""极简系统提示。pi 的教训：系统提示 + 工具定义 <1000 tokens 就够，前沿模型
不需要长篇规则；用户定制靠 AGENTS.md（T2 落地分层加载）。

shell 环境提示按执行后端注入——实测中模型会在 Windows cmd 引号上反复空转，
明确告知 shell 类型与"写脚本再运行"的出路能省掉大量无效轮次。
"""
from __future__ import annotations

from pathlib import Path

import os

PROMPT = """你是 EasyCode，运行在用户机器上的编程 agent。当前工作目录：{workspace}。
工作方式：先 read 代码理解现状，再小心修改——edit 做精确替换（old 必须全文唯一），
write 只用于新建或整体重写；bash 运行命令查看与验证（非零退出码是反馈，不是错误）。
只做用户要求的事；改动前想清楚，完成后直接给出简短中文总结。
任务完成就不再调用工具，直接输出总结作为最终回答。"""

SHELL_HINT_LOCAL_NT = ("注意：bash 使用 Windows cmd 语法；工作目录已经是目标目录，无需 cd。"
                       "含引号的 python -c 很容易因转义失败，优先用 write 写临时脚本文件再用 python 运行。")
SHELL_HINT_LOCAL_POSIX = "注意：bash 使用 POSIX shell 语法；工作目录已经是目标目录，无需 cd。"
SHELL_HINT_DOCKER = "注意：bash 是一次性加固容器内的 Linux sh（断网、非 root），工作目录挂载在 /workspace。"

AGENTS_CLIP = 8000


def shell_hint(backend=None) -> str:
    if backend == "docker":
        return SHELL_HINT_DOCKER
    if backend == "local":
        return SHELL_HINT_LOCAL_NT if os.name == "nt" else SHELL_HINT_LOCAL_POSIX
    return ""


def load_agents_md(workspace: Path) -> str:
    """全局 → 项目 两层 AGENTS.md；缺失层跳过，内容超长中段截断。"""
    parts = []
    for layer, path in (("全局", Path.home() / ".easycode" / "AGENTS.md"),
                        ("项目", Path(workspace) / "AGENTS.md")):
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    parts.append(f"## {layer}指令（{path.name}）\n\n{text}")
        except OSError:
            continue
    joined = "\n\n".join(parts)
    if len(joined) > AGENTS_CLIP:
        return joined[:AGENTS_CLIP] + "\n…（AGENTS.md 过长，已截断）"
    return joined


def system_prompt(workspace, backend=None) -> str:
    prompt = PROMPT.format(workspace=workspace)
    hint = shell_hint(backend)
    if hint:
        prompt += "\n" + hint
    agents = load_agents_md(workspace)
    if agents:
        prompt += "\n\n# 用户自定义指令（AGENTS.md）\n\n" + agents
    return prompt
