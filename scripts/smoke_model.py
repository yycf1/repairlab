"""Temporary credential import for a real-model smoke test; never writes credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from dotenv import dotenv_values
from repairlab.models import Limits, StartRequest
from repairlab.service import Service
from repairlab.settings import Settings


def main():
    parser = argparse.ArgumentParser(description="临时读取指定 env 的一个密钥，执行真实模型验证；不会复制 env。")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--key-name", default="LLM_API_KEY")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--fixture", default="csv-tags")
    parser.add_argument("--backend", choices=["local", "docker"], default="docker")
    parser.add_argument("--allow-local", action="store_true")
    args = parser.parse_args()
    key = dotenv_values(args.env_file).get(args.key_name)
    if not key:
        raise SystemExit("指定配置中没有可用密钥。")
    os.environ.update(REPAIRLAB_API_KEY=key, REPAIRLAB_BASE_URL=args.base_url, REPAIRLAB_MODEL=args.model)
    service = Service(Settings())
    try:
        task = service.start(StartRequest(fixture=args.fixture, mode="model", backend=args.backend,
            allow_local=args.allow_local, limits=Limits(max_steps=20, max_tokens=60000, max_seconds=600)), background=False)
        safe = {k: task.get(k) for k in ["id", "fixture", "status", "model", "model_calls", "tokens", "seconds", "error", "result"]}
        safe["verification_reason"] = (task.get("verification") or {}).get("reason")
        print(json.dumps(safe, ensure_ascii=False, indent=2), flush=True)
        return 0 if task["status"] == "passed" else 1
    finally:
        service.close()
        os.environ.pop("REPAIRLAB_API_KEY", None)


if __name__ == "__main__":
    sys.exit(main())
