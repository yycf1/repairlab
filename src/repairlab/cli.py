from __future__ import annotations

import argparse
import json
from pathlib import Path

from repairlab.models import RepoProfile, StartRequest
from repairlab.service import Service
from repairlab.settings import Settings


def main():
    parser = argparse.ArgumentParser(description="RepairLab 代码修复工作台")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动本地工作台")
    serve.add_argument("--port", type=int, default=8766)
    run = commands.add_parser("run", help="同步执行一个修复任务")
    run.add_argument("--fixture", default="boundary-discount")
    run.add_argument("--profile", type=Path, help="自有仓库配置 JSON；该入口仅供本地操作员使用")
    run.add_argument("--mode", choices=["demo", "model"], default="demo")
    run.add_argument("--backend", choices=["docker", "local"], default="docker")
    run.add_argument("--allow-local", action="store_true", help="明确允许可信仓库在本机执行，无容器隔离")
    run.add_argument("--strategy", choices=["feedback", "single_shot"], default="feedback")
    run.add_argument("--issue", default="")
    run.add_argument("--task-kind", choices=["repair", "inspect"], default="repair")
    evaluate = commands.add_parser("evaluate", help="内置脚本任务的机制演示；不是模型 benchmark")
    evaluate.add_argument("--backend", choices=["docker", "local"], default="docker")
    evaluate.add_argument("--allow-local", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "serve":
        import uvicorn
        from repairlab.api import create_app
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)
        return
    service = Service(settings)
    try:
        if args.command == "evaluate":
            results = []
            for fixture in service.fixtures():
                for strategy in ["single_shot", "feedback"]:
                    request = StartRequest(fixture=fixture["id"], backend=args.backend, allow_local=args.allow_local,
                                           strategy=strategy, mode="demo")
                    task = service.start(request, background=False)
                    results.append({"fixture": fixture["id"], "strategy": strategy,
                        "status": task["status"], "steps": task.get("model_calls"), "seconds": task.get("seconds"),
                        "task_id": task["id"]})
            report = {"kind": "scripted_mechanism_demo", "real_model_evaluation": False,
                      "note": "预设脚本用于检查闭环和验收机制，不能证明模型修复率或策略收益。", "results": results}
            target = settings.data_dir / "demo-evaluation.json"
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            profile = None
            if args.profile:
                data = json.loads(args.profile.read_text(encoding="utf-8"))
                for name in ["source", "acceptance"]:
                    if data.get(name):
                        data[name] = str((args.profile.parent / data[name]).resolve())
                profile = RepoProfile.model_validate(data)
            request = StartRequest(fixture=args.fixture, mode=args.mode, backend=args.backend,
                allow_local=args.allow_local, strategy=args.strategy, issue=args.issue, task_kind=args.task_kind)
            task = service.start(request, background=False, profile=profile)
            print(json.dumps(task, ensure_ascii=False, indent=2))
            if task["status"] not in {"passed", "reviewed"}:
                raise SystemExit(1)
    finally:
        service.close()


if __name__ == "__main__":
    main()
