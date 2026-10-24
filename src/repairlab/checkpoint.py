"""Task-local execution checkpoints: atomic writes and a versioned schema.

A checkpoint pairs with the vendor trajectory file (messages + model stats). Together
they allow an interrupted agent loop to be reconstructed; neither contains credentials
or provider response bodies.
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA_VERSION = 1


def save_checkpoint(path, payload):
    data = {"schema_version": SCHEMA_VERSION, **payload}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return data


def load_checkpoint(path):
    path = Path(path)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("检查点版本不兼容；请创建新任务重跑。")
    return data
