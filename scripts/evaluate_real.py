"""Real-model evaluation on the bundled fixtures (paid API calls; gated behind --yes).

口径：内置夹具上的真实模型评测，记录每个任务的状态、控制拦截、用量与耗时。
夹具为自建样例仓库，结果不构成外部基准（如 SWE-bench）修复率证据。
不带 --yes 运行时只打印任务计划与最坏用量上限，不发送任何模型请求；
先明确样本与预算，再开始计费调用。
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile
import time
from datetime import datetime, timezone

from repairlab.models import Limits, RepoProfile, StartRequest
from repairlab.service import Service
from repairlab.settings import ROOT, Settings

REPAIR_FIXTURES = ("boundary-discount", "csv-tags", "pagination")


def fixture_digest(fixture):
    root = ROOT / "examples" / fixture
    manifest = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def build_plan(args):
    plan = []
    for fixture in REPAIR_FIXTURES:
        if args.fixture and fixture != args.fixture:
            continue
        strategies = ("single_shot", "feedback") if args.strategies == "both" else (args.strategies,)
        for strategy in strategies:
            plan.append({"fixture": fixture, "task_kind": "repair", "strategy": strategy, "repeat": 0})
    if not args.skip_inspect and (not args.fixture or args.fixture == "investigation"):
        plan.append({"fixture": "investigation", "task_kind": "inspect", "strategy": "feedback", "repeat": 0})
    repeated = []
    for item in plan:
        for index in range(args.repeat):
            repeated.append({**item, "repeat": index + 1})
    if args.limit:
        repeated = repeated[:args.limit]
    return repeated


def inspect_profile():
    base = ROOT / "examples" / "investigation"
    data = json.loads((base / "repository.json").read_text(encoding="utf-8"))
    data.update(source=str(base / "repo"), acceptance=str(base / "acceptance"))
    return RepoProfile.model_validate(data)


def run_task(service, item, limits, backend):
    request = StartRequest(fixture=item["fixture"], task_kind=item["task_kind"], strategy=item["strategy"],
                           mode="model", backend=backend, allow_local=backend == "local", limits=limits)
    started = time.monotonic()
    try:
        task = service.start(request, background=False,
                             profile=inspect_profile() if item["task_kind"] == "inspect" else None)
    except Exception as error:  # infrastructure failure: record and keep the remaining tasks going
        return {"status": "error", "error": f"任务执行失败：{type(error).__name__}", "seconds": round(time.monotonic() - started, 3)}
    verification = task.get("verification") or {}
    return {"status": task["status"], "result": verification.get("reason") or task.get("result"),
            "error": task.get("error", ""), "control_stats": task.get("control_stats") or {},
            "model_calls": task.get("model_calls"), "tokens": task.get("tokens"), "seconds": task.get("seconds"),
            "round": task.get("round"), "patch_sha256": hashlib.sha256((verification.get("patch") or "").encode()).hexdigest(),
            "suites": {name: verification[name]["counts"] for name in ("acceptance", "regression") if verification.get(name)}}


def aggregate(results):
    statuses, control, by_fixture = {}, {}, {}
    token_total = {"prompt": 0, "completion": 0}
    for record in results:
        statuses[record["status"]] = statuses.get(record["status"], 0) + 1
        bucket = by_fixture.setdefault(record["fixture"], {})
        bucket[record["status"]] = bucket.get(record["status"], 0) + 1
        for code, count in (record.get("control_stats") or {}).items():
            control[code] = control.get(code, 0) + count
        for kind in ("prompt", "completion"):
            token_total[kind] += (record.get("tokens") or {}).get(kind) or 0
    seconds = [r["seconds"] for r in results if isinstance(r.get("seconds"), (int, float))]
    calls = [r["model_calls"] for r in results if isinstance(r.get("model_calls"), int)]
    return {"total": len(results), "status_counts": statuses, "by_fixture": by_fixture,
            "control_stats_totals": control,
            "tokens_total": token_total,
            "model_calls": {"total": sum(calls), "mean": round(sum(calls) / len(calls), 1)} if calls else None,
            "seconds": {"mean": round(sum(seconds) / len(seconds), 1), "max": max(seconds)} if seconds else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("local", "docker"), default="local")
    parser.add_argument("--allow-local", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=1, help="每个配置的重复次数（1-10）")
    parser.add_argument("--strategies", choices=("both", "single_shot", "feedback"), default="both")
    parser.add_argument("--fixture", help="只跑指定夹具（repair 三件套或 investigation）")
    parser.add_argument("--skip-inspect", action="store_true")
    parser.add_argument("--limit", type=int, help="最多执行的任务数")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--max-tokens", type=int, default=60000)
    parser.add_argument("--max-seconds", type=int, default=600)
    parser.add_argument("--yes", action="store_true", help="确认预算后真正执行计费调用")
    args = parser.parse_args()
    if args.backend == "local" and not args.allow_local:
        parser.error("宿主机直接执行需要 --allow-local（本机未装 Docker 时评测在 local 后端进行）")
    if not 1 <= args.repeat <= 10:
        parser.error("--repeat 取值 1-10")
    plan = build_plan(args)
    limits = Limits(max_steps=args.max_steps, max_tokens=args.max_tokens,
                    max_seconds=args.max_seconds)
    if not args.yes:
        print(json.dumps({"planned_tasks": len(plan),
                          "worst_case_token_cap": len(plan) * args.max_tokens,
                          "limits": limits.model_dump(),
                          "tasks": plan}, ensure_ascii=False, indent=2))
        print("以上为任务计划；确认预算后加 --yes 执行真实调用。", flush=True)
        return 0
    settings = Settings()
    if not settings.model_ready:
        raise SystemExit("模型未配置：请在工作台设置页填写模型、Base URL 与 API Key，或设置 REPAIRLAB_* 环境变量。")
    records = []
    started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with tempfile.TemporaryDirectory(prefix="repairlab-real-eval-") as temporary:
        previous = os.environ.get("REPAIRLAB_DATA_DIR")
        os.environ["REPAIRLAB_DATA_DIR"] = str(Path(temporary) / "data")
        service = None
        try:
            service = Service(settings)
            for index, item in enumerate(plan, 1):
                label = f"[{index}/{len(plan)}] {item['fixture']} {item['task_kind']} {item['strategy']} #{item['repeat']}"
                print(f"{label} ... ", flush=True)
                record = run_task(service, item, limits, args.backend)
                record.update({k: item[k] for k in ("fixture", "task_kind", "strategy", "repeat")},
                              fixture_sha256=fixture_digest(item["fixture"]))
                records.append(record)
                print(f"{label} -> {record['status']} calls={record.get('model_calls')} "
                      f"tokens={record.get('tokens')} seconds={record.get('seconds')}", flush=True)
        finally:
            if service:
                service.close()
            if previous is None:
                os.environ.pop("REPAIRLAB_DATA_DIR", None)
            else:
                os.environ["REPAIRLAB_DATA_DIR"] = previous
    report = {"schema_version": 1, "kind": "real_model_bundled_fixtures", "real_model_evaluation": True,
              "scope": "真实模型在内置自建夹具上的评测：状态分布、控制拦截、用量与耗时；"
                       "不构成外部基准修复率证据，样本与预算见任务计划。",
              "model": settings.model, "base_url": settings.base_url, "backend": args.backend,
              "limits": limits.model_dump(), "python": platform.python_version(), "platform": platform.system(),
              "started_utc": started_utc, "results": records, "aggregates": aggregate(records)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = report["aggregates"]
    print(json.dumps({"total": summary["total"], "status_counts": summary["status_counts"],
                      "tokens_total": summary["tokens_total"], "output": str(args.output)},
                     ensure_ascii=False), flush=True)
    return 0 if "error" not in summary["status_counts"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
