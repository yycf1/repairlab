"""Inspect a local repository without importing it; collection is an explicit execution step."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import time
import tomllib
import uuid

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet

from repairlab.repository import Repository, copy_repository, snapshot_files
from repairlab.runners import make_runner, process


def local_directory(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ValueError("请填写本机绝对目录，暂不支持网络共享目录。")
    # Reject drive roots before accessing the filesystem: the drive may not
    # exist on another machine (for example a GitHub Windows runner).
    if path == Path(path.anchor):
        raise ValueError("请选择具体的仓库目录，不能选择磁盘根目录。")
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("目录不能是符号链接或 junction。")
    try:
        path = path.resolve(strict=True)
    except OSError as error:
        raise ValueError("所选目录不存在或无法访问。") from error
    if not path.is_dir() or path == Path(path.anchor):
        raise ValueError("请选择具体的仓库目录，不能选择磁盘根目录。")
    return path


def validate_targets(repo, targets, files):
    if not targets or len(targets) > 30:
        raise ValueError("请设置 1～30 个测试路径或 pytest 节点。")
    for target in targets:
        if not target.strip() or len(target) > 500 or target.startswith("-") or "\x00" in target:
            raise ValueError("测试目标只能填写相对路径或 pytest 节点，不能填写命令及参数。")
        name = target.split("::", 1)[0].replace("\\", "/")
        path = repo.path(name)
        if path.is_file():
            if name not in files or path.suffix != ".py":
                raise ValueError(f"测试文件不在快照内或不是 Python 文件：{name}")
        elif path.is_dir() and "::" not in target:
            if not any(f.endswith(".py") and (name == "." or f.startswith(name.rstrip("/") + "/")) for f in files):
                raise ValueError(f"测试目录没有可复制的 Python 文件：{name}")
        else:
            raise ValueError(f"测试目标不存在：{name}")


def declarations(root, files):
    requirements, unsupported, requires_python = [], [], ""
    if "pyproject.toml" in files:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project = data.get("project", {})
        if not isinstance(project, dict) or not isinstance(project.get("dependencies", []), list):
            raise ValueError("pyproject.toml 的 project / dependencies 声明格式无效。")
        requirements.extend(project.get("dependencies", []))
        requires_python = project.get("requires-python", "")
        if not isinstance(requires_python, str) or not all(isinstance(r, str) for r in requirements):
            raise ValueError("Python 版本和依赖声明必须使用字符串。")
        if requires_python:
            SpecifierSet(requires_python)
        if project.get("dynamic") or data.get("tool", {}).get("poetry"):
            unsupported.append("动态项目元数据或 Poetry 依赖需要人工核对")
        if project.get("optional-dependencies") or data.get("dependency-groups") or data.get("build-system"):
            unsupported.append("可选组、开发组和构建依赖未自动展开；请在测试收集中验证实际导入")
    for filename in ("requirements.txt", "requirements-dev.txt"):
        if filename not in files:
            continue
        for line in (root / filename).read_text(encoding="utf-8").splitlines():
            line = re.split(r"\s+#", line, 1)[0].strip()
            if line and not line.startswith("#"):
                requirements.append(line)
    parsed = {}
    for text in requirements[:200]:
        try:
            requirement = Requirement(text)
            if requirement.url:
                unsupported.append("URL / 本地路径依赖需要人工准备")
            else:
                parsed[str(requirement)] = requirement
        except InvalidRequirement:
            unsupported.append("递归 requirements、安装选项或非标准依赖声明需要人工核对")
    if len(requirements) > 200:
        unsupported.append("超过 200 项的依赖声明未全部检查")
    return list(parsed.values()), requires_python, sorted(set(unsupported))


def runtime_info(backend, image, names, cwd):
    # Metadata lookup does not import the repository or installed packages.
    code = ("import importlib.metadata as m,json,sys,platform,os,re; names=" + repr(sorted(set(names) | {"pytest"})) + "; "
            "norm=lambda n:re.sub('[-_.]+','-',n.lower()); "
            "installed={norm(d.metadata['Name']):d.version for d in m.distributions() if d.metadata['Name']}; "
            "print(json.dumps({'python':platform.python_version(),'executable':sys.executable,"
            "'system':platform.system(),'markers':{'sys_platform':sys.platform,'os_name':os.name,"
            "'platform_machine':platform.machine(),'platform_release':platform.release(),'platform_version':platform.version(),"
            "'platform_python_implementation':platform.python_implementation(),'implementation_name':sys.implementation.name},"
            "'packages':{n:installed.get(norm(n)) for n in names}}))")
    container_name = "repairlab-probe-" + uuid.uuid4().hex[:12]
    argv = ([sys.executable, "-I", "-c", code] if backend == "local" else
            ["docker", "run", "--rm", "--pull", "never", "--name", container_name, "--network", "none", "--read-only", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--user", "10001:10001", "--cpus", "1", "--memory", "256m",
             "--pids-limit", "32", image, "python", "-I", "-c", code])
    try:
        result = process(argv, cwd=cwd, timeout=15)
    finally:
        if backend == "docker":
            process(["docker", "rm", "-f", container_name], cwd=cwd, timeout=10)
    if result["exit_code"] or result["timed_out"] or result["output_limited"]:
        raise ValueError("无法读取执行环境的 Python / 包版本，请检查解释器或 Docker 镜像。")
    return json.loads(result["stdout"])


def inspect_repository(profile, options, settings):
    checks = []
    def add(key, label, status, detail):
        checks.append({"id": key, "label": label, "status": status, "detail": detail})

    report = {"checked_at": time.time(), "backend": options.backend, "checks": checks,
              "ready": False, "can_register": False, "collection": None}
    try:
        source = local_directory(profile.source)
        if source == settings.root.resolve() or source.is_relative_to(settings.data_dir):
            raise ValueError("请选择待修复仓库，不能选择 RepairLab 本身或其任务数据目录。")
        paths = snapshot_files(source)
        files = {p.relative_to(source).as_posix() for p in paths}
        report["snapshot"] = {"files": len(paths), "bytes": sum(p.stat().st_size for p in paths),
                              "source": str(source)}
        add("snapshot", "仓库快照", "pass", f"{len(paths)} 个 UTF-8 文本文件；原仓库不会被修改。隐藏目录、凭据和非文本资源不复制。")
        repo = Repository(source, profile)
        if not profile.editable or len(profile.editable) > 500:
            raise ValueError("没有识别到可修改源码，或源码规则超过 500 项。")
        for pattern in profile.editable:
            normalized = pattern.replace("\\", "/")
            if not normalized or normalized.startswith(("/", "-")) or ":" in normalized or ".." in PurePosixPath(normalized).parts:
                raise ValueError("源码规则只能使用仓库内的相对路径或 glob。")
        editable = sorted(name for name in files if repo.editable(name))
        if not editable:
            raise ValueError("源码匹配规则未匹配到允许修改的 Python 文件；测试和配置始终受保护。")
        report["editable_files"] = editable
        add("editable", "可修改源码", "pass", f"匹配 {len(editable)} 个文件，例如：{'、'.join(editable[:4])}")
        validate_targets(repo, profile.development_tests, files)
        validate_targets(repo, profile.regression_tests, files)
        add("targets", "测试目标", "pass", "开发及回归目标均存在；运行固定的 python -m pytest，不执行自定义 shell 命令。")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}", profile.image):
            raise ValueError("Docker 镜像名格式无效。")
        if profile.acceptance:
            acceptance = local_directory(profile.acceptance)
            if acceptance == source or acceptance.is_relative_to(source):
                raise ValueError("独立验收目录必须放在仓库目录之外，避免 Agent 提前读取。")
            if not any(p.suffix == ".py" for p in snapshot_files(acceptance)):
                raise ValueError("验收目录没有 Python 测试文件。")
            add("acceptance", "外置验收", "pass", "已设置仓库外的验收目录；实际隔离能力取决于执行环境。")
        else:
            add("acceptance", "外置验收", "warning", "未提供外置验收；最终会在干净副本重跑开发和回归测试，不代表独立测试数据。")
        requirements, requires_python, unsupported = declarations(source, files)
        report["dependencies"] = [str(r) for r in requirements]
        report["can_register"] = True
    except (OSError, ValueError, TypeError) as error:
        add("structure", "仓库配置", "error", str(error))
        return report

    add("git", "补丁应用工具", "pass" if shutil.which("git") else "error",
        "Git 可用。" if shutil.which("git") else "未找到 Git，最终补丁验收需要 Git。")
    if options.collect_tests and options.backend == "local" and not options.allow_local:
        add("runtime", "测试执行权限", "error", "收集测试会导入代码，请先确认允许在本机运行可信仓库。")
    if not any(c["id"] == "runtime" and c["status"] == "error" for c in checks):
        try:
            if options.backend == "docker":
                make_runner(options.backend, options.allow_local)
                image_check = process(["docker", "image", "inspect", profile.image], cwd=settings.root, timeout=10)
                if image_check["exit_code"] or image_check["timed_out"]:
                    raise ValueError("Docker 镜像尚未准备。请先构建镜像；预检不会联网拉取或安装依赖。")
            info = runtime_info(options.backend, profile.image, [r.name for r in requirements], settings.root)
            report["runtime"] = info
            add("runtime", "执行环境", "pass", f"Python {info['python']} · {info['executable']}")
            if requires_python and not SpecifierSet(requires_python).contains(info["python"], prereleases=True):
                add("python_version", "Python 版本要求", "error", f"项目要求 {requires_python}，当前为 {info['python']}。")
            missing = []
            environment = {**default_environment(), **info.get("markers", {}), "python_version": ".".join(info["python"].split(".")[:2]),
                           "python_full_version": info["python"], "platform_system": info["system"], "extra": ""}
            for requirement in [Requirement("pytest"), *requirements]:
                if requirement.marker and not requirement.marker.evaluate(environment):
                    continue
                installed = info["packages"].get(requirement.name)
                if installed is None or (requirement.specifier and not requirement.specifier.contains(installed, prereleases=True)):
                    missing.append(str(requirement))
                if requirement.extras:
                    unsupported.append("依赖 extras 的传递依赖需要人工核对")
            add("dependencies", "声明的依赖", "error" if missing else "pass",
                "缺失或版本不符：" + "、".join(missing) if missing else "pytest 及识别到的直接依赖满足版本声明；未运行安装脚本。")
        except (OSError, ValueError, RuntimeError, InvalidSpecifier) as error:
            add("runtime", "执行环境", "error", str(error))
    if unsupported:
        add("dependency_scope", "依赖检查范围", "warning", "；".join(sorted(set(unsupported))))
    add("collection_scope", "测试收集", "warning", "静态预检不能证明测试能够导入；可单独执行测试收集，它会导入仓库代码。")
    if options.collect_tests and not any(c["status"] == "error" for c in checks):
        try:
            with tempfile.TemporaryDirectory(prefix="preflight-", dir=settings.data_dir) as temporary:
                workspace = Path(temporary) / "workspace"
                copy_repository(source, workspace)
                if profile.acceptance:
                    copy_repository(Path(profile.acceptance), workspace / "_acceptance")
                targets = list(dict.fromkeys(profile.development_tests + profile.regression_tests))
                if profile.acceptance:
                    targets.append("_acceptance")
                result = make_runner(options.backend, options.allow_local).run_tests(workspace, [*targets, "--collect-only"], 30,
                                                                                    image=profile.image)
                report["collection"] = result
                passed = result["exit_code"] == 0 and not any(result[k] for k in ("timed_out", "cancelled", "output_limited"))
                checks[:] = [c for c in checks if c["id"] != "collection_scope"]
                add("collection", "测试收集", "pass" if passed else "error",
                    "已在临时副本收集测试；未执行测试函数，也未修改原仓库。" if passed else "收集失败；请检查日志、导入路径及测试插件。")
        except (OSError, ValueError, RuntimeError) as error:
            add("collection", "测试收集", "error", str(error))
    report["ready"] = not any(c["status"] == "error" for c in checks)
    return report
