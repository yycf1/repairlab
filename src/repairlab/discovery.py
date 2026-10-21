"""Infer a conservative repository profile from files and static pytest configuration."""
from __future__ import annotations

import configparser
from fnmatch import fnmatchcase
from glob import escape
from pathlib import PurePosixPath
import shlex
import tomllib

from repairlab.models import RepoProfile
from repairlab.preflight import local_directory
from repairlab.repository import Repository, snapshot_files, source_matches

NON_SOURCE = {"docs", "doc", "examples", "example", "benchmarks", "benchmark", "vendor", "third_party", "build", "dist", "testing"}


def values(value):
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    return shlex.split(value) if isinstance(value, str) else []


def discover_repository(value, settings):
    root = local_directory(value)
    if root == settings.root.resolve() or root.is_relative_to(settings.data_dir):
        raise ValueError("请选择待修复仓库，不能选择 RepairLab 本身或其任务数据目录。")
    paths = snapshot_files(root)
    files = {p.relative_to(root).as_posix() for p in paths}
    notes, name, description, options, origin = [], root.name, "", {}, "文件结构"
    if "pyproject.toml" in files:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project = data.get("project", data.get("tool", {}).get("poetry", {}))
        if isinstance(project, dict):
            name = project.get("name") or name
            description = project.get("description") or ""
        options = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
        if options:
            origin = "pyproject.toml"
    for filename, section in (("pytest.ini", "pytest"), ("setup.cfg", "tool:pytest"), ("tox.ini", "pytest")):
        if filename not in files:
            continue
        config = configparser.ConfigParser(interpolation=None)
        try:
            config.read(root / filename, encoding="utf-8")
        except configparser.Error:
            raise ValueError(f"{filename} 配置格式无效，请修正后重新识别。") from None
        if filename == "setup.cfg" and name == root.name and config.has_option("metadata", "name"):
            name = config.get("metadata", "name")
        if config.has_section(section) and (filename == "pytest.ini" or not options):
            options = dict(config[section])
            origin = filename
            if filename == "pytest.ini":
                break
    if not isinstance(options, dict):
        raise ValueError("pytest 配置格式无效，可在高级设置中指定测试目录。")
    patterns = values(options.get("python_files")) or ["test_*.py", "*_test.py"]
    test_files = sorted(f for f in files if f.endswith(".py") and any(fnmatchcase(PurePosixPath(f).name, p) for p in patterns))
    configured = values(options.get("testpaths"))
    targets = []
    for target in configured:
        normalized = target.replace("\\", "/").rstrip("/")
        relative = PurePosixPath(normalized)
        if relative.is_absolute() or ".." in relative.parts or ":" in normalized or normalized.startswith(("-", ".")):
            notes.append("项目配置中的测试路径不受支持，请在高级设置中确认。")
            continue
        matching = [f for f in test_files if f == normalized or f.startswith(normalized + "/") or source_matches(f, normalized)]
        if matching:
            if (root / normalized).exists():
                targets.append(normalized)
            else:
                targets.extend(matching)
        else:
            notes.append(f"配置中的测试目标 {normalized} 未找到对应测试文件。")
    if not targets:
        for filename in test_files:
            parts = PurePosixPath(filename).parts
            directories = ["/".join(parts[:i + 1]) for i, part in enumerate(parts[:-1]) if part.lower() in {"test", "tests", "testing"}]
            targets.append(directories[0] if directories else filename)
        origin = origin if not configured else "文件结构（配置目标未匹配）"
    targets = sorted(set(targets))
    targets = [t for t in targets if not any(t.startswith(other + "/") for other in targets if other != t)]
    if len(targets) > 30:
        notes.append("测试分布在超过 30 个位置，请在高级设置中指定更集中的测试目录。")
    if not targets:
        notes.append("未发现 pytest 测试。请在高级设置中指定已有测试，或先为仓库补充测试用例。")
    profile = RepoProfile(name=str(name)[:100], description=str(description)[:500], source=str(root),
                          development_tests=targets, regression_tests=list(targets), protected=[escape(f) for f in test_files])
    repo = Repository(root, profile)
    source_files = sorted(f for f in files if repo.editable(f) and not any(p.lower() in NON_SOURCE for p in PurePosixPath(f).parts[:-1]))
    profile.editable = [("./" if "/" not in f and escape(f) != f else "") + escape(f) for f in source_files]
    if not source_files:
        notes.append("未识别到可修复的 Python 源码；目前只支持 Python/pytest 仓库。")
    return {"profile": profile.model_dump(), "summary": {"file_count": len(files), "source_count": len(source_files),
            "test_count": len(test_files), "test_targets": targets, "test_origin": origin, "notes": notes,
            "needs_configuration": not source_files or not targets or len(targets) > 30}}
