"""内置工具：read / write / edit / bash（默认）+ ls / grep（--tools 显式开启）。

接口对齐 pi：read 带 1 起始 offset/limit（默认 2000 行）；write 新建或整文件重写
（自动建父目录）；edit 的 old 必须精确且唯一匹配（外科手术式修改）；bash 的
command + 可选 timeout，非零退出码是反馈不是错误。执行后端由 ToolContext.runner
决定（Docker 加固容器或可信本机）——这是 EasyCode 相对 pi 默认 YOLO 的差异点。

安全边界刻意收敛为一条：所有路径必须落在工作目录内（read/write/edit/ls/grep 同源
校验）；不做文件类型/扩展名白名单——通用编程 agent 面对的是任意目录。
"""
from __future__ import annotations

from pathlib import Path

from easycode.tools import Registry, Tool

SKIP_DIRS = {".git", "__pycache__", ".easycode", "node_modules", ".venv", ".pytest_cache"}


def _resolve(workspace: Path, path: str) -> Path:
    candidate = (workspace / path).resolve()
    if not candidate.is_relative_to(workspace):
        raise ValueError(f"路径越界：{path}")
    return candidate


def _rel(workspace: Path, path: Path) -> str:
    return str(path.relative_to(workspace)).replace("\\", "/")


# ---- read --------------------------------------------------------------------

READ_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "相对于工作目录的文件路径"},
        "offset": {"type": "integer", "minimum": 1, "description": "起始行（1 起始），默认 1"},
        "limit": {"type": "integer", "minimum": 1, "description": "读取行数，默认 2000"},
    },
    "required": ["path"],
}


def read_tool():
    def run(args, ctx):
        path = _resolve(ctx.workspace, str(args.get("path", "")))
        if not path.is_file():
            raise ValueError(f"文件不存在或不是普通文件：{args.get('path')}")
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, min(int(args.get("limit") or 2000), 5000))
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        window = lines[offset - 1:offset - 1 + limit]
        numbered = "\n".join(f"{offset + index:>6}\t{line}" for index, line in enumerate(window))
        details = {"path": _rel(ctx.workspace, path), "start": offset,
                   "end": offset + len(window) - 1, "total_lines": len(lines),
                   "truncated": offset - 1 + limit < len(lines)}
        return (numbered or "（空文件或范围为空）"), details

    return Tool(name="read", description="读取工作目录内一个文本文件；默认前 2000 行，可用 offset/limit 翻页。",
                parameters=READ_PARAMETERS, run=run)


# ---- write -------------------------------------------------------------------

WRITE_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "相对于工作目录的文件路径"},
        "content": {"type": "string", "description": "完整文件内容（UTF-8 文本）"},
    },
    "required": ["path", "content"],
}


def write_tool():
    def run(args, ctx):
        path = _resolve(ctx.workspace, str(args.get("path", "")))
        content = str(args.get("content", ""))
        existed = path.is_file()
        before = path.read_text(encoding="utf-8", errors="replace") if existed else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        rel = _rel(ctx.workspace, path)
        action = "覆盖" if existed else "创建"
        return f"已{action} {rel}（{len(content)} 字符，{len(content.splitlines())} 行）", {
            "path": rel, "created": not existed, "chars": len(content),
            "before_lines": len(before.splitlines()), "before_chars": len(before),
            "before_head": "\n".join(before.splitlines()[:50])}

    return Tool(name="write", description="把完整内容写入工作目录内一个文件：新建或整体重写（自动创建父目录）；局部修改请用 edit。",
                parameters=WRITE_PARAMETERS, run=run)


# ---- edit --------------------------------------------------------------------

EDIT_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "相对于工作目录的文件路径"},
        "old": {"type": "string", "description": "要被替换的原文片段（必须逐字符精确匹配且全文唯一）"},
        "new": {"type": "string", "description": "替换后的新片段"},
    },
    "required": ["path", "old", "new"],
}


def edit_tool():
    def run(args, ctx):
        path = _resolve(ctx.workspace, str(args.get("path", "")))
        if not path.is_file():
            raise ValueError(f"文件不存在或不是普通文件：{args.get('path')}")
        old, new = str(args.get("old", "")), str(args.get("new", ""))
        if not old:
            raise ValueError("old 不能为空。")
        text = path.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        if count == 0:
            raise ValueError("old 在文件中未找到；请先 read 确认精确内容（含空白与缩进）。")
        if count > 1:
            raise ValueError(f"old 匹配 {count} 处，需要唯一匹配；请扩大上下文片段后重试。")
        path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
        rel = _rel(ctx.workspace, path)
        return f"已修改 {rel}（替换 1 处，{len(old)} → {len(new)} 字符）", {
            "path": rel, "replacements": 1, "old": old, "new": new}

    return Tool(name="edit", description="对工作目录内一个文件做精确替换：old 必须逐字符匹配且全文唯一；适合外科手术式小修改。",
                parameters=EDIT_PARAMETERS, run=run)


# ---- bash --------------------------------------------------------------------

BASH_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "要执行的 shell 命令（在工作目录内执行）"},
        "timeout": {"type": "integer", "minimum": 1, "description": "秒数上限，默认 60"},
    },
    "required": ["command"],
}


def bash_tool():
    def run(args, ctx):
        if ctx.runner is None:
            raise ValueError("当前会话没有可用的命令执行后端。")
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command 不能为空。")
        timeout = max(1, min(int(args.get("timeout") or ctx.command_timeout), 600))
        result = ctx.runner.run_command(ctx.workspace, command, timeout,
                                        cancel=ctx.cancel, image=ctx.image)
        details = {"exit_code": result["exit_code"], "seconds": result["seconds"],
                   "timed_out": result["timed_out"], "cancelled": result["cancelled"],
                   "output_limited": result["output_limited"],
                   "stdout": result["stdout"][-4000:], "stderr": result["stderr"][-2000:]}
        head = f"exit {result['exit_code']}" + ("（超时被终止）" if result["timed_out"] else "")
        output = head + ("\n--- stdout ---\n" + result["stdout"] if result["stdout"] else "") \
                      + ("\n--- stderr ---\n" + result["stderr"] if result["stderr"] else "")
        return output, details

    return Tool(name="bash", description="在工作目录执行一条 shell 命令并返回 stdout/stderr 与退出码；非零退出码不是错误，是反馈。",
                parameters=BASH_PARAMETERS, run=run)


# ---- ls（可选）----------------------------------------------------------------

LS_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "相对于工作目录的子目录，默认根目录"},
    },
}


def ls_tool():
    def run(args, ctx):
        base = _resolve(ctx.workspace, str(args.get("path") or "."))
        if not base.is_dir():
            raise ValueError(f"目录不存在：{args.get('path') or '.'}")
        entries = sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        lines = [entry.name + ("/" if entry.is_dir() else "") for entry in entries]
        return ("\n".join(lines) or "（空目录）"), {"path": _rel(ctx.workspace, base) or ".",
                                                   "count": len(lines)}

    return Tool(name="ls", description="列出一个目录的直接条目（目录带 / 后缀）；看全仓结构请配合 bash。",
                parameters=LS_PARAMETERS, run=run)


# ---- grep（可选）---------------------------------------------------------------

GREP_PARAMETERS = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "要查找的文本（大小写不敏感的子串匹配）"},
        "path": {"type": "string", "description": "搜索起点（文件或目录），默认整个工作目录"},
    },
    "required": ["pattern"],
}


def grep_tool():
    def run(args, ctx):
        pattern = str(args.get("pattern", ""))
        if not pattern:
            raise ValueError("pattern 不能为空。")
        base = _resolve(ctx.workspace, str(args.get("path") or "."))
        needle = pattern.casefold()
        hits, searched = [], 0
        files = [base] if base.is_file() else (
            path for path in base.rglob("*") if path.is_file()
            and not (set(path.parts) & SKIP_DIRS or any(part in SKIP_DIRS for part in path.parts)))
        for path in files:
            searched += 1
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, 1):
                if needle in line.casefold():
                    hits.append(f"{_rel(ctx.workspace, path)}:{number}: {line.strip()[:200]}")
                    if len(hits) >= 200:
                        break
            if len(hits) >= 200:
                hits.append("…（命中过多，已截断）")
                break
        header = f"共 {len(hits)} 处命中（扫描 {searched} 个文件）" if hits else "没有命中"
        return (header + ("\n" + "\n".join(hits) if hits else "")), {
            "hits": len(hits), "searched_files": searched}

    return Tool(name="grep", description="在整个工作目录按子串查找文本（大小写不敏感），返回 文件:行号: 内容 列表。",
                parameters=GREP_PARAMETERS, run=run)


# ---- 装配 ----------------------------------------------------------------------

# pi 对齐：默认四工具；ls/grep 属可选只读加速器，用 --tools 显式开启。
DEFAULT_TOOL_NAMES = ("read", "bash", "edit", "write")
TOOL_FACTORIES = {"read": read_tool, "bash": bash_tool, "edit": edit_tool, "write": write_tool,
                  "ls": ls_tool, "grep": grep_tool}


def build_registry(names=None):
    """names=None → 默认四工具；否则按集合装配（未知名报错，交 CLI 转为友好退出）。"""
    if names is None:
        names = set(DEFAULT_TOOL_NAMES)
    unknown = set(names) - set(TOOL_FACTORIES)
    if unknown:
        raise ValueError(f"未知工具：{'、'.join(sorted(unknown))}；可用：{'、'.join(TOOL_FACTORIES)}")
    registry = Registry()
    for name, factory in TOOL_FACTORIES.items():
        if name in names:
            registry.register(factory())
    return registry
