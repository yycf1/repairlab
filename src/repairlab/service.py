from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
import uuid

from repairlab.agent import RepairAgent
from repairlab.checkpoint import load_checkpoint
from repairlab.lease import DataLease
from repairlab.model import CompatibleModel, DemoModel
from repairlab.models import Limits, RepoProfile, RepositoryCheck, StartRequest
from repairlab.preflight import inspect_repository, local_directory
from repairlab.repository import Repository, copy_repository, diff_repositories
from repairlab.runners import make_runner
from repairlab.storage import Store
from repairlab.tools import ToolEnvironment, source_digest
from repairlab.verification import Verifier
from repairlab.workflow import WorkflowState

TERMINAL = {"passed", "failed", "error", "cancelled", "interrupted", "budget_exhausted", "blocked", "reviewed"}


class Service:
    def __init__(self, settings):
        self.settings = settings
        self.lease = DataLease(settings.data_dir)
        try:
            self.store = Store(settings.data_dir / "repairlab.sqlite3")
            self.store.recover_interrupted()
        except Exception:
            self.lease.close()
            raise
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="repairlab")
        self.cancellations = {}

    def fixtures(self):
        result = []
        for path in sorted((self.settings.root / "examples").glob("*/profile.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            result.append({"id": path.parent.name, "name": data["name"], "description": data["description"],
                           "issue": data["issue"], "demo": True})
        return result

    def profile(self, fixture):
        registered = self.store.repository(fixture)
        if registered:
            return RepoProfile.model_validate(registered["profile"]), None
        # Arbitrary host paths must first be explicitly registered by the local operator.
        ids = {f["id"] for f in self.fixtures()}
        if fixture not in ids:
            raise ValueError("Unknown registered repository")
        base = self.settings.root / "examples" / fixture
        data = json.loads((base / "profile.json").read_text(encoding="utf-8"))
        demo_config = data.pop("demo_config")
        data.update(source=str(base / "repo"), acceptance=str(base / "acceptance"), demo=True)
        return RepoProfile.model_validate(data), demo_config

    def repositories(self):
        builtins = [{**f, "kind": "builtin"} for f in self.fixtures()]
        custom = [{"id": r["id"], "name": r["profile"]["name"], "description": r["profile"]["description"],
                   "issue": r["profile"]["issue"], "source": r["profile"]["source"], "demo": False,
                   "kind": "local", "preflight": r["preflight"]} for r in self.store.repositories()]
        return builtins + custom

    def check_registration(self, request):
        if not request.authorize_directory:
            raise ValueError("请明确授权 RepairLab 读取所填写的本机仓库目录。")
        return inspect_repository(request.profile, request, self.settings)

    def register(self, request, repository_id=None):
        if repository_id is not None and self.store.repository(repository_id) is None:
            raise ValueError("已登记仓库不存在。")
        report = self.check_registration(request)
        if not report["can_register"]:
            raise ValueError("仓库配置未通过检查：" + report["checks"][-1]["detail"])
        profile = request.profile.model_copy(update={"source": str(local_directory(request.profile.source)), "demo": False,
            "acceptance": str(local_directory(request.profile.acceptance)) if request.profile.acceptance else None})
        repository_id = repository_id or "local-" + uuid.uuid4().hex[:16]
        return self.store.save_repository(repository_id, {"profile": profile.model_dump(), "preflight": report})

    def check_repository(self, repository_id, options):
        registered = self.store.repository(repository_id)
        if registered is None:
            raise ValueError("已登记仓库不存在。")
        report = inspect_repository(RepoProfile.model_validate(registered["profile"]), options, self.settings)
        self.store.save_repository(repository_id, {**registered, "preflight": report})
        return report

    def start(self, request: StartRequest, *, background=True, profile=None, model_override=None):
        if request.task_kind == "inspect" and request.strategy != "feedback":
            raise ValueError("自主检查需要反馈迭代策略，以验证候选问题及复现结果。")
        if request.task_kind == "inspect" and request.mode != "model" and model_override is None:
            raise ValueError("自主检查使用真实模型；预设脚本仅支持指定问题演示。")
        preflight = None
        if profile is None:
            profile, demo_config = self.profile(request.fixture)
            if not profile.demo:
                preflight = self.check_repository(request.fixture, RepositoryCheck(backend=request.backend, allow_local=request.allow_local))
                if not preflight["ready"]:
                    errors = [c["detail"] for c in preflight["checks"] if c["status"] == "error"]
                    raise ValueError("仓库启动预检未通过：" + "；".join(errors))
        else:
            demo_config = None
        if request.mode == "model" and not self.settings.model_ready and model_override is None:
            raise ValueError("项目2尚未配置大模型；可先运行明确标注的脚本演示。")
        if request.mode == "demo" and demo_config is None and model_override is None:
            raise ValueError("Scripted demos are available only for bundled fixtures")
        runner = make_runner(request.backend, request.allow_local)
        task_id = uuid.uuid4().hex[:16]
        issue = (request.issue.strip() or "自主检查此仓库：依据项目约定、源码和测试发现可复现的问题；仅修复证据成立的问题，并报告检查范围和未覆盖部分。") if request.task_kind == "inspect" else (request.issue.strip() or profile.issue)
        self.store.create(task_id, {"title": profile.name, "issue": issue, "mode": request.mode,
            "backend": request.backend, "strategy": request.strategy, "task_kind": request.task_kind, "limits": request.limits.model_dump(),
            "model": self.settings.model if request.mode == "model" else "scripted-demo",
            "fixture": request.fixture, "tokens": {"prompt": 0, "completion": 0},
            "allow_local": request.allow_local, "resumeable": False, "round": 1,
            "repository_profile": profile.model_dump(exclude={"demo"}), "preflight": preflight})
        cancel = threading.Event()
        self.cancellations[task_id] = cancel
        args = (task_id, request, profile, demo_config, issue, runner, cancel, model_override)
        if background:
            self.pool.submit(self.run, *args)
        else:
            self.run(*args)
        return self.store.get(task_id)

    def follow_up(self, task_id, text, *, model_override=None):
        """Continue a finished task in the same workspace with a new user instruction.

        Follow-up rounds are real-model only: scripted demos replay a fixed decision list.
        The workspace digest must still match the checkpoint, so hand-edited workspaces are refused.
        """
        task = self.store.get(task_id)
        text = (text or "").strip()
        if task is None or task["status"] not in TERMINAL:
            raise ValueError("任务不存在或尚未结束，无法追加指令。")
        if task.get("mode") != "model":
            raise ValueError("脚本演示使用固定脚本，不支持追加指令；请创建真实模型任务。")
        if not text:
            raise ValueError("追加指令不能为空。")
        task_dir = self.settings.data_dir / "tasks" / task_id
        checkpoint = load_checkpoint(task_dir / "artifacts" / "checkpoint.json")
        trajectory = task_dir / "artifacts" / "trajectory.json"
        if checkpoint is None or not trajectory.exists():
            raise ValueError("缺少执行现场；请创建新任务。")
        profile = RepoProfile.model_validate(task["repository_profile"])
        if source_digest(Repository(task_dir / "workspace", profile)) != checkpoint.get("source_digest"):
            raise ValueError("工作区内容与执行现场不一致；无法追加指令，请创建新任务。")
        request = StartRequest(fixture=task.get("fixture") or "boundary-discount", issue=task["issue"],
                               mode="model", backend=task["backend"], strategy=task["strategy"],
                               task_kind=task["task_kind"], allow_local=task.get("allow_local", False),
                               limits=Limits(**task["limits"]))
        cancel = threading.Event()
        self.cancellations[task_id] = cancel
        round_number = int(task.get("round") or 1) + 1
        self.store.update(task_id, status="queued", resumeable=False, round=round_number)
        self.store.event(task_id, "instruction", {"text": text, "round": round_number})
        self.pool.submit(self._execute, task_id, request, profile, None, task["issue"],
                         make_runner(request.backend, request.allow_local), cancel, model_override,
                         checkpoint, text)
        return self.store.get(task_id)

    def run(self, task_id, request, profile, demo_config, issue, runner, cancel, model_override):
        self._execute(task_id, request, profile, demo_config, issue, runner, cancel, model_override)

    def resume(self, task_id, *, model_override=None):
        """Continue an interrupted task from its checkpoint after validating the execution scene."""
        task = self.store.get(task_id)
        if task is None or task["status"] != "interrupted" or not task.get("resumeable"):
            raise ValueError("只有带检查点的中断任务可以续跑。")
        task_dir = self.settings.data_dir / "tasks" / task_id
        checkpoint = load_checkpoint(task_dir / "artifacts" / "checkpoint.json")
        trajectory = task_dir / "artifacts" / "trajectory.json"
        if checkpoint is None or not trajectory.exists():
            raise ValueError("检查点或轨迹缺失，无法续跑；请创建新任务。")
        if checkpoint.get("issue") != task.get("issue") or checkpoint.get("strategy") != task.get("strategy"):
            raise ValueError("检查点与任务记录不一致；请创建新任务。")
        profile = RepoProfile.model_validate(task["repository_profile"])
        if source_digest(Repository(task_dir / "workspace", profile)) != checkpoint.get("source_digest"):
            raise ValueError("工作区内容与检查点不一致；无法安全续跑，请创建新任务。")
        request = StartRequest(fixture=task.get("fixture") or "boundary-discount", issue=task["issue"],
                               mode=task["mode"], backend=task["backend"], strategy=task["strategy"],
                               task_kind=task["task_kind"], allow_local=task.get("allow_local", False),
                               limits=Limits(**task["limits"]))
        demo_config = None
        if profile.demo:
            _, demo_config = self.profile(request.fixture)
        cancel = threading.Event()
        self.cancellations[task_id] = cancel
        self.store.update(task_id, status="queued", resumeable=False, error="")
        self.pool.submit(self._execute, task_id, request, profile, demo_config, task["issue"],
                         make_runner(request.backend, request.allow_local), cancel, model_override, checkpoint)
        return self.store.get(task_id)

    def _execute(self, task_id, request, profile, demo_config, issue, runner, cancel, model_override,
                 checkpoint=None, follow_up=None):
        task_dir = self.settings.data_dir / "tasks" / task_id
        if checkpoint is None:
            task_dir.mkdir(parents=True)
        started = time.monotonic()
        model = None
        environment = None
        usage_recorded = False
        def emit(kind, data):
            self.store.event(task_id, kind, data)
            if kind == "workflow_state":
                self.store.update(task_id, workflow=data, plan=data["plan"])
        def persist_usage():
            """Usage must be committed before any terminal status; polls may read at any point."""
            nonlocal usage_recorded
            if model is None or usage_recorded:
                return
            usage_recorded = True
            if follow_up:
                # Round budgets are fresh by design; recorded task usage accumulates across rounds.
                prior = self.store.get(task_id)
                prior_tokens = prior.get("tokens") or {}
                self.store.update(task_id,
                    model_calls=(prior.get("model_calls") or 0) + model.model_calls,
                    tokens={"prompt": (prior_tokens.get("prompt") or 0) + model.tokens["prompt"],
                            "completion": (prior_tokens.get("completion") or 0) + model.tokens["completion"]})
            else:
                self.store.update(task_id, model_calls=model.model_calls, tokens=dict(model.tokens))
        try:
            self.store.update(task_id, status="running", resumeable=False)
            emit("started", {"mode": request.mode, "backend": runner.name, "strategy": request.strategy,
                             "resumed": checkpoint is not None and not follow_up,
                             "follow_up": follow_up is not None,
                             "round": (self.store.get(task_id) or {}).get("round")})
            baseline, workspace = task_dir / "baseline", task_dir / "workspace"
            if checkpoint is None:
                copy_repository(Path(profile.source), baseline)
                copy_repository(baseline, workspace)
            environment = ToolEnvironment(Repository(workspace, profile), runner, request.limits, emit, cancel,
                                          task_kind=request.task_kind, baseline=baseline)
            if checkpoint is not None:
                environment.restore_extra_state(checkpoint["environment"])
                environment.state = WorkflowState.model_validate(checkpoint["workflow"])
                if follow_up:
                    # A new round keeps evidence and revision; phase restarts from active work.
                    environment.state.phase = ("investigating" if environment.state.task_kind == "inspect"
                                               else "repairing")

            def on_delta(step, text, seq):
                emit("model_output", {"step": step, "delta": text, "seq": seq})

            model = model_override or (DemoModel(demo_config, request.strategy) if request.mode == "demo"
                                      else CompatibleModel(self.settings, request.limits, on_delta=on_delta))
            if checkpoint is not None and model_override is None and not follow_up:
                # Interrupted resumes continue counters; fresh rounds and injected models start over.
                model.restore(checkpoint["model"])
            controller = RepairAgent(model, environment, request.limits, emit, cancel, request.strategy,
                                     checkpoint_path=task_dir / "artifacts" / "checkpoint.json",
                                     trajectory_path=task_dir / "artifacts" / "trajectory.json")
            if checkpoint is None:
                result = controller.run(issue)
            else:
                trajectory = json.loads((task_dir / "artifacts" / "trajectory.json").read_text(encoding="utf-8"))
                if follow_up:
                    result = controller.continue_run(issue, follow_up, trajectory["messages"])
                else:
                    result = controller.resume_run(issue, checkpoint["agent"], trajectory["messages"])
            persist_usage()
            if cancel.is_set() or result.get("exit_status") == "cancelled":
                self.store.update(task_id, status="cancelled", result="任务已取消")
                return
            if result.get("exit_status") != "submitted":
                reason = result.get("exit_status", "unknown")
                status = ("reviewed" if reason == "reviewed" else
                          "budget_exhausted" if reason in {"LimitsExceeded", "TimeExceeded", "token_budget"} else
                          "blocked" if reason in {"unresolved", "no_progress"} else "error")
                self.store.update(task_id, status=status, result=reason, blocker_reason=result.get("reason"),
                                  summary=result.get("submission", ""))
                return
            self.store.update(task_id, status="verifying")
            emit("verification_started", {"note": "在干净环境中应用补丁，最终验收结果不回灌 Agent。"})
            verification = Verifier(runner, request.limits).verify(baseline, workspace, task_dir / "verification",
                                                                 profile, cancel=cancel, probes=environment.investigation.confirmed_probes())
            status = "cancelled" if cancel.is_set() else "passed" if verification["passed"] else "failed"
            self.store.update(task_id, status=status, verification=verification, summary=result.get("submission", ""),
                              patch=verification["patch"], changed_files=verification["changed_files"])
            emit("verification", verification)
        except Exception as error:
            # Store concise error types only. Operators get no accidental provider secret dumps.
            self.store.update(task_id, status="error", error=f"任务执行失败：{type(error).__name__}")
            emit("error", {"type": type(error).__name__})
        finally:
            persist_usage()
            if environment is not None:
                self.store.update(task_id, workflow=environment.state.snapshot(environment.digest()),
                                  plan=environment.plan, test_runs=environment.test_runs,
                                  control_stats=dict(environment.control_stats))
                if not self.store.get(task_id).get("verification"):
                    patch, changed, violations = diff_repositories(baseline, workspace, profile)
                    if patch:
                        partial_dir = task_dir / "artifacts"
                        partial_dir.mkdir(exist_ok=True)
                        (partial_dir / "patch.diff").write_text(patch, encoding="utf-8", newline="\n")
                        self.store.update(task_id, patch=patch, changed_files=changed,
                                          unverified_patch=True, patch_violations=violations)
            self.store.update(task_id, seconds=round(time.monotonic() - started, 3))
            if model is not None and hasattr(model, "close"):
                model.close()
            self.cancellations.pop(task_id, None)
            task = self.store.get(task_id)
            report = {k: v for k, v in task.items() if k != "patch"}
            artifacts = task_dir / "artifacts"
            artifacts.mkdir(exist_ok=True)
            (artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            emit("finished", {"status": task["status"], "seconds": task.get("seconds")})

    def cancel(self, task_id):
        event = self.cancellations.get(task_id)
        if not event:
            return False
        event.set()
        self.store.event(task_id, "cancel_requested", {})
        return True

    def close(self):
        for event in list(self.cancellations.values()):
            event.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.lease.close()
