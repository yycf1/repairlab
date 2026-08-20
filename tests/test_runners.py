"""执行后端（bash 工具的引擎）：超时/取消/输出上限、本机直跑的显式允许、容器加固 argv。

覆盖 process 层与两种后端的命令执行路径（bash 工具只走 run_command）。
"""
import os
import shutil
import subprocess
import sys
import threading

import pytest

from easycode.runners import DockerRunner, ExecutionUnavailable, LocalRunner, process


def test_local_execution_requires_opt_in():
    with pytest.raises(ExecutionUnavailable):
        LocalRunner()


def test_local_run_command_executes_and_captures(tmp_path):
    runner = LocalRunner(allowed=True)
    result = runner.run_command(tmp_path, "echo local-ok", 30)
    assert result["exit_code"] == 0 and "local-ok" in result["stdout"]
    assert result["isolation"] == "trusted_local_only"
    failed = runner.run_command(tmp_path, "exit 3" if os.name != "nt" else "cmd /c exit 3", 30)
    assert failed["exit_code"] != 0


def test_docker_command_run_keeps_hardening_and_writable_mount(tmp_path):
    argv = DockerRunner.command_run(tmp_path, "echo hi", "easycode-python:latest", "cmd-x")
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert argv[argv.index("--user") + 1] == "10001:10001"
    assert "--cap-drop" in argv and "--pids-limit" in argv
    mount = argv[argv.index("--mount") + 1]
    assert "type=bind" in mount and "readonly" not in mount
    assert argv[-3:] == ["sh", "-c", "echo hi"]
    assert not any("KEY=" in arg for arg in argv)


def test_process_timeout_and_cancel(tmp_path):
    result = process([sys.executable, "-c", "import time;time.sleep(20)"], cwd=tmp_path, timeout=0.2)
    assert result["timed_out"] and result["seconds"] < 5
    cancel = threading.Event()
    cancel.set()
    result = process([sys.executable, "-c", "import time;time.sleep(20)"], cwd=tmp_path, timeout=30, cancel=cancel)
    assert result["cancelled"] and result["seconds"] < 5


def test_process_limits_output_even_when_program_exits_quickly(tmp_path):
    result = process([sys.executable, "-c", "print('x'*2100000)"], cwd=tmp_path, timeout=5)
    assert result["output_limited"]
    assert len(result["stdout"]) <= 12000


DOCKER_BIN = shutil.which("docker") or r"C:\Users\86166\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe"


def _docker_ready():
    try:
        probe = subprocess.run([DOCKER_BIN, "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True, timeout=20)
        return probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _docker_ready(), reason="Docker engine not running")
def test_docker_run_command_roundtrip_and_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", os.path.dirname(DOCKER_BIN) + os.pathsep + os.environ["PATH"])
    runner = DockerRunner(check=False)
    result = runner.run_command(tmp_path, "echo container-ok && touch /workspace/from-container", 60)
    assert result["exit_code"] == 0 and "container-ok" in result["stdout"]
    assert result["isolation"] == "docker"
    assert (tmp_path / "from-container").is_file()
    timed_out = runner.run_command(tmp_path, "sleep 30", 3)
    assert timed_out["timed_out"] and timed_out["seconds"] < 20
