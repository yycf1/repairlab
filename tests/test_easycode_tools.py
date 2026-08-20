"""EasyCode T2：write/edit/ls/grep 工具、路径边界、AGENTS.md 分层加载。

工具面默认与 pi 对齐（read/bash/edit/write；ls/grep 显式开启）；路径安全只有
一条规则——必须落在工作目录内；edit 的 old 必须精确且唯一匹配（pi 同款纪律）。
"""
import threading

from easycode.core.loop import agent_loop
from easycode.prompt import system_prompt
from easycode.tools import Tool, ToolContext
from easycode.tools.builtins import DEFAULT_TOOL_NAMES, build_registry

from test_easycode_core import ScriptedClient


def make_ctx(tmp_path, runner=None):
    return ToolContext(workspace=tmp_path, runner=runner, cancel=threading.Event())


def dispatch(registry, name, args, ctx):
    return registry.dispatch({"id": "c1", "function": {"name": name, "arguments": args}}, ctx)


def test_write_creates_nested_and_overwrites(tmp_path):
    registry = build_registry()
    ctx = make_ctx(tmp_path)
    result = dispatch(registry, "write", {"path": "pkg/util.py", "content": "x = 1\n"}, ctx)
    assert result["ok"] and "创建" in result["output"]
    assert (tmp_path / "pkg" / "util.py").read_text(encoding="utf-8") == "x = 1\n"
    again = dispatch(registry, "write", {"path": "pkg/util.py", "content": "x = 2\n"}, ctx)
    assert again["ok"] and "覆盖" in again["output"]
    assert again["details"]["created"] is False


def test_edit_requires_unique_exact_match(tmp_path):
    (tmp_path / "code.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    registry = build_registry()
    ctx = make_ctx(tmp_path)
    missing = dispatch(registry, "edit", {"path": "code.py", "old": "return 2", "new": "return 3"}, ctx)
    assert not missing["ok"] and "未找到" in missing["output"]
    (tmp_path / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    ambiguous = dispatch(registry, "edit", {"path": "dup.py", "old": "x = 1", "new": "x = 2"}, ctx)
    assert not ambiguous["ok"] and "唯一" in ambiguous["output"]
    ok = dispatch(registry, "edit", {"path": "code.py", "old": "return 1", "new": "return 2"}, ctx)
    assert ok["ok"] and ok["details"]["replacements"] == 1
    assert "return 2" in (tmp_path / "code.py").read_text(encoding="utf-8")


def test_all_file_tools_refuse_path_escape(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    registry = build_registry()
    ctx = make_ctx(tmp_path)
    for name, args in (("read", {"path": "../outside.txt"}),
                       ("write", {"path": "../evil.txt", "content": "x"}),
                       ("edit", {"path": "../outside.txt", "old": "s", "new": "t"})):
        result = dispatch(registry, name, args, ctx)
        assert not result["ok"] and "越界" in result["output"], name
    assert not (tmp_path.parent / "evil.txt").exists()


def test_ls_and_grep_are_optional_accelerators(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("value = 42\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.py").write_text("value = 0\n", encoding="utf-8")
    registry = build_registry({"ls", "grep"})
    ctx = make_ctx(tmp_path)
    ls = dispatch(registry, "ls", {}, ctx)
    assert ls["ok"] and "__pycache__/" in ls["output"] and "src/" in ls["output"]
    grep = dispatch(registry, "grep", {"pattern": "VALUE"}, ctx)
    assert grep["ok"] and "src/main.py:1" in grep["output"]  # 大小写不敏感
    assert "__pycache__" not in grep["output"]  # 噪声目录跳过
    assert grep["details"]["hits"] == 1


def test_default_registry_matches_pi_four_tools():
    registry = build_registry()
    assert tuple(sorted(registry.names())) == tuple(sorted(DEFAULT_TOOL_NAMES))
    assert {"read", "bash", "edit", "write"} == set(DEFAULT_TOOL_NAMES)
    subset = build_registry({"read", "grep"})
    assert set(subset.names()) == {"read", "grep"}
    try:
        build_registry({"nope"})
        raise AssertionError("未知工具应报错")
    except ValueError as error:
        assert "nope" in str(error)


def test_agents_md_layers_global_then_project(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".easycode").mkdir(parents=True)
    (fake_home / ".easycode" / "AGENTS.md").write_text("总是用中文回复。", encoding="utf-8")
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "AGENTS.md").write_text("本项目用 pytest。", encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    prompt = system_prompt(tmp_path / "ws")
    assert prompt.index("总是用中文回复") < prompt.index("本项目用 pytest")  # 全局在前、项目在后
    assert "AGENTS.md" in prompt
    # 无 AGENTS.md 时不出现注入段
    (tmp_path / "plain").mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "empty-home")
    assert "用户自定义指令" not in system_prompt(tmp_path / "plain")


def test_agents_md_overlong_content_is_clipped(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("长" * 20000, encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nohome")
    prompt = system_prompt(tmp_path)
    assert len(prompt) < 10000 and "已截断" in prompt


def test_file_tools_work_through_the_loop(tmp_path):
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    client = ScriptedClient([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "edit",
             "arguments": '{"path": "note.txt", "old": "hello", "new": "hello easycode"}'}}]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "read",
             "arguments": '{"path": "note.txt"}'}}]},
        {"role": "assistant", "content": "改好了"},
    ])
    events = []
    result = agent_loop(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "改一行"}],
                        client=client, tools=build_registry(), ctx=make_ctx(tmp_path),
                        on_event=lambda kind, data: events.append((kind, data)))
    assert result["stop_reason"] == "completed"
    assert "hello easycode" in (tmp_path / "note.txt").read_text(encoding="utf-8")
    tool_outputs = [data["output"] for data in (d for k, d in events if k == "tool_result")]
    assert any("hello easycode" in output for output in tool_outputs)


def test_tool_dataclass_is_minimal_surface():
    # Registry 的公开面刻意小：schemas()/dispatch()；工具即 name+description+parameters+run。
    registry = build_registry()
    schemas = registry.schemas()
    assert [schema["function"]["name"] for schema in schemas] == list(registry.names())
    assert all("parameters" in schema["function"] for schema in schemas)
    assert isinstance(Tool, type)
