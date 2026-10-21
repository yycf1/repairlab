"""Real-model investigation of a self-written repository; no known issue is supplied."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

from dotenv import dotenv_values
from repairlab.models import Limits, RepoProfile, StartRequest
from repairlab.service import Service, TERMINAL
from repairlab.settings import ROOT, Settings


def main():
    parser = argparse.ArgumentParser(description="真实模型自主调查验证；样例为自建仓库，不是公开评测。")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--key-name", default="LLM_API_KEY")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--backend", choices=["docker", "local"], default="docker")
    parser.add_argument("--allow-local", action="store_true")
    args = parser.parse_args()
    key = dotenv_values(args.env_file).get(args.key_name)
    if not key:
        raise SystemExit("指定配置中没有可用密钥。")
    variables = {"REPAIRLAB_API_KEY": key, "REPAIRLAB_BASE_URL": args.base_url, "REPAIRLAB_MODEL": args.model}
    previous = {name: os.environ.get(name) for name in variables}
    os.environ.update(variables)
    service = None
    try:
        service = Service(Settings())
        base = ROOT / "examples" / "investigation"
        data = json.loads((base / "repository.json").read_text(encoding="utf-8"))
        data.update(source=str(base / "repo"), acceptance=str(base / "acceptance"))
        task = service.start(StartRequest(fixture="investigation-sample", task_kind="inspect", mode="model",
            backend=args.backend, allow_local=args.allow_local,
            limits=Limits(max_steps=36, max_tokens=90000, max_seconds=600)), profile=RepoProfile.model_validate(data))
        cursor = 0
        while True:
            for event in service.store.events(task["id"], cursor):
                cursor = event["id"]
                if event["kind"] == "action":
                    print(json.dumps({"step": event["data"]["step"], "tool": event["data"]["action"]["tool"]}), flush=True)
            task = service.store.get(task["id"])
            if task["status"] in TERMINAL and (service.settings.data_dir / "tasks" / task["id"] / "artifacts/report.json").is_file():
                break
            time.sleep(1)
        safe = {k: task.get(k) for k in ["id", "status", "model", "model_calls", "tokens", "seconds", "error", "result"]}
        safe.update(kind="real_model_self_written_sample", task_kind=task["task_kind"],
                    baseline_category=(task.get("workflow", {}).get("baseline_test") or {}).get("category"),
                    candidates=[{k: c.get(k) for k in ["id", "status", "summary", "baseline_probe", "latest_probe", "resolution"]}
                                for c in task.get("workflow", {}).get("candidates", [])],
                    verification_reason=(task.get("verification") or {}).get("reason"))
        target = service.settings.data_dir / "investigation-model-smoke.json"
        target.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(safe, ensure_ascii=False, indent=2), flush=True)
        return 0 if task["status"] in {"passed", "reviewed"} else 1
    finally:
        if service:
            service.close()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == "__main__":
    raise SystemExit(main())
