"""bash 命令执行后端：加固容器与本地直跑两种后端。

默认 Docker 一次性加固容器：断网、非 root、系统层只读、CPU/内存/进程数受限；
--read-only 只锁容器根文件系统，工作目录的 bind mount 保持可写（Windows/9p 上
已实测 uid 10001 的写入落在宿主机）。这是相对 pi 默认 YOLO 的刻意差异——pi 作者
的建议是"不放心就进容器"，EasyCode 直接把容器做成了默认项。
可信目录可切本机直跑（LocalRunner，必须显式 allow_local）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import uuid


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
            raise ExecutionUnavailable("本地执行需要明确启用；仅适用于可信目录，不提供容器隔离。")

    def run_command(self, workspace, command, timeout, *, cancel=None, image=None):
        """Arbitrary shell command on the trusted host; environment secrets are filtered out."""
        env = {k: v for k, v in os.environ.items()
               if not any(s in k.upper() for s in ["KEY", "TOKEN", "SECRET", "PASSWORD"])}
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})
        result = process(_shell_argv(command), cwd=workspace, timeout=timeout, env=env, cancel=cancel)
        result["isolation"] = "trusted_local_only"
        return result


class DockerRunner:
    name = "docker"

    def __init__(self, *, check=True):
        if check:
            if not shutil.which("docker"):
                raise ExecutionUnavailable("未安装 Docker；请安装并启动 Docker，或为可信目录明确选择本地执行。")
            ping = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=15)
            if ping.returncode:
                raise ExecutionUnavailable("Docker 服务不可用，请先启动 Docker Desktop。")

    @staticmethod
    def command_run(workspace, command, image, name):
        """One-shot hardened container; the workspace mount is writable, argv is arbitrary.

        --read-only only locks the container's root filesystem; the bind mount stays writable
        (verified on Windows/9p: uid 10001 writes land on the host, root-fs writes are refused).
        The workspace is the user's real directory — the agent may wreck it, history is in
        the session store, files are under the user's own version control.
        """
        return ["docker", "run", "--rm", "--pull", "never", "--name", name, "--network", "none",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", "10001:10001", "--cpus", "1", "--memory", "512m", "--pids-limit", "64",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                "--mount", f"type=bind,source={workspace.resolve()},target=/workspace",
                "--workdir", "/workspace", "--env", "PYTHONDONTWRITEBYTECODE=1",
                image, "sh", "-c", command]

    def run_command(self, workspace, command, timeout, *, cancel=None, image="easycode-python:latest"):
        """One arbitrary shell command in a disposable hardened container."""
        name = "easycode-cmd-" + uuid.uuid4().hex[:14]
        try:
            result = process(self.command_run(workspace, command, image, name),
                             cwd=workspace, timeout=timeout, cancel=cancel)
            result["isolation"] = "docker"
            return result
        finally:
            # The timeout path kills the docker client; rm -f also terminates the in-container command.
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)


def _shell_argv(command):
    return ["cmd", "/c", command] if os.name == "nt" else ["/bin/sh", "-c", command]


def make_runner(name, allow_local=False):
    return LocalRunner(allowed=allow_local) if name == "local" else DockerRunner()
