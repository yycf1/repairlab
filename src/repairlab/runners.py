from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from repairlab.test_evidence import evidence_directory, read_evidence


class ExecutionUnavailable(RuntimeError):
    pass


def process(argv, *, cwd, timeout, env=None, cancel=None):
    """Bounded log files prevent unbounded pipe buffers; kill the process tree on timeout/cancel."""
    started = time.monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL, **kwargs)
        timed_out = cancelled = False
        output_limited = False
        while child.poll() is None:
            timed_out = time.monotonic() - started > timeout
            cancelled = bool(cancel and cancel.is_set())
            output_limited = os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > 2_000_000
            if timed_out or cancelled or output_limited:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, timeout=10)
                else:
                    import signal
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.kill()
                break
            time.sleep(0.05)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        output_limited = output_limited or os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > 2_000_000
        def output(handle):
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 12000))
            return handle.read(12000).decode("utf-8", errors="replace")
        return {"exit_code": child.returncode, "timed_out": timed_out, "cancelled": cancelled,
                "output_limited": output_limited, "stdout": output(out), "stderr": output(err),
                "seconds": round(time.monotonic() - started, 3), "command": argv}


class LocalRunner:
    name = "local"

    def __init__(self, *, allowed=False):
        if not allowed:
            raise ExecutionUnavailable("本地执行需要明确启用；仅适用于可信仓库，不提供容器隔离。")

    def run_tests(self, workspace: Path, tests: list[str], timeout, *, cancel=None, image=None):
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ["KEY", "TOKEN", "SECRET", "PASSWORD"])}
        env.update({"PYTHONPATH": str(workspace) + os.pathsep + str(workspace / "src"),
                    "PYTHONUTF8": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        # Operator environment options must not silently filter critical tests.
        env.pop("PYTEST_ADDOPTS", None)
        with evidence_directory() as (harness, report, run_id):
            env.update(PYTHONPATH=str(harness) + os.pathsep + env["PYTHONPATH"],
                       REPAIRLAB_TEST_REPORT=str(report), REPAIRLAB_TEST_RUN_ID=run_id)
            result = process([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                              "-p", "repairlab_pytest_evidence", "--rootdir", str(workspace), *tests],
                             cwd=workspace, timeout=timeout, env=env, cancel=cancel)
            result["test_evidence"] = read_evidence(report, run_id, result["exit_code"])
        result["isolation"] = "trusted_local_only"
        return result


class DockerRunner:
    name = "docker"

    def __init__(self, *, check=True):
        if check:
            if not shutil.which("docker"):
                raise ExecutionUnavailable("未安装 Docker；请安装并启动 Docker，或为可信样例明确选择本地执行。")
            ping = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=15)
            if ping.returncode:
                raise ExecutionUnavailable("Docker 服务不可用，请先启动 Docker Desktop。")

    @staticmethod
    def command(workspace, tests, image, name, *, harness=None, run_id=None):
        evidence_args = []
        pytest_args = []
        pythonpath = "/workspace:/workspace/src"
        if harness is not None:
            evidence_args = ["--mount", f"type=bind,source={harness.resolve()},target=/harness,readonly",
                             "--mount", f"type=bind,source={(harness / 'output').resolve()},target=/reports",
                             "--env", "REPAIRLAB_TEST_REPORT=/reports/results.json",
                             "--env", f"REPAIRLAB_TEST_RUN_ID={run_id}"]
            pytest_args = ["-p", "repairlab_pytest_evidence", "--rootdir", "/workspace"]
            pythonpath = "/harness:" + pythonpath
        return ["docker", "run", "--rm", "--pull", "never", "--name", name, "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "10001:10001",
                "--cpus", "1", "--memory", "512m", "--pids-limit", "64",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--mount",
                f"type=bind,source={workspace.resolve()},target=/workspace,readonly",
                "--workdir", "/workspace", "--env", "PYTHONPATH=" + pythonpath,
                "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "--env", "PYTHONDONTWRITEBYTECODE=1",
                *evidence_args, image, "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *pytest_args, *tests]

    def run_tests(self, workspace, tests, timeout, *, cancel=None, image="repairlab-python:latest"):
        name = "repairlab-" + uuid.uuid4().hex[:14]
        try:
            with evidence_directory() as (harness, report, run_id):
                result = process(self.command(workspace, tests, image, name, harness=harness, run_id=run_id),
                                 cwd=workspace, timeout=timeout, cancel=cancel)
                result["test_evidence"] = read_evidence(report, run_id, result["exit_code"])
            result["isolation"] = "docker"
            return result
        finally:
            # Also kills descendants when the docker client was interrupted.
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)


def make_runner(name, allow_local=False):
    return LocalRunner(allowed=allow_local) if name == "local" else DockerRunner()
