from __future__ import annotations

import json
from contextlib import contextmanager
import sqlite3
import threading
import time
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, created REAL, status TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT, created REAL, kind TEXT, data TEXT);
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id,id);
                CREATE TABLE IF NOT EXISTS repositories(id TEXT PRIMARY KEY, updated REAL, data TEXT);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, task_id, data):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO tasks VALUES(?,?,?,?)", (task_id, time.time(), "queued", json.dumps(data)))

    def update(self, task_id, **changes):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT data,status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            data = json.loads(row["data"])
            data.update(changes)
            db.execute("UPDATE tasks SET status=?,data=? WHERE id=?",
                       (changes.get("status", row["status"]), json.dumps(data), task_id))

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        return {**json.loads(row["data"]), "id": row["id"], "created": row["created"], "status": row["status"]}

    def list(self):
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM tasks ORDER BY created DESC LIMIT 60")]
        return [self.get(task_id) for task_id in ids]

    def event(self, task_id, kind, data):
        with self.lock, self.connect() as db:
            cursor = db.execute("INSERT INTO events(task_id,created,kind,data) VALUES(?,?,?,?)",
                                (task_id, time.time(), kind, json.dumps(data)))
            return cursor.lastrowid

    def events(self, task_id, after=0):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT 500",
                              (task_id, after)).fetchall()
        return [{"id": r["id"], "kind": r["kind"], "created": r["created"],
                 "data": json.loads(r["data"])} for r in rows]

    def recover_interrupted(self):
        """History survives restart; running tasks are marked interrupted, never silently replayed.

        A task with an on-disk checkpoint (agent loop stage only) is additionally marked resumable.
        """
        with self.connect() as db:
            rows = db.execute("SELECT id,status FROM tasks WHERE status IN ('running','queued','verifying')").fetchall()
        for row in rows:
            task_id, status = row["id"], row["status"]
            checkpoint = self.path.parent / "tasks" / task_id / "artifacts" / "checkpoint.json"
            resumeable = status in {"running", "queued"} and checkpoint.exists()
            self.update(task_id, status="interrupted", resumeable=resumeable,
                        error="服务中断；可从检查点继续执行。" if resumeable else "服务已重启；历史可查看，请创建新任务重跑。")
            self.event(task_id, "interrupted", {"reason": "server_restart", "resumeable": resumeable})

    def save_repository(self, repository_id, data):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO repositories VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,data=excluded.data",
                       (repository_id, time.time(), json.dumps(data)))
        return self.repository(repository_id)

    def repository(self, repository_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM repositories WHERE id=?", (repository_id,)).fetchone()
        return {**json.loads(row["data"]), "id": row["id"], "updated": row["updated"]} if row else None

    def repositories(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM repositories ORDER BY updated DESC").fetchall()
        return [{**json.loads(row["data"]), "id": row["id"], "updated": row["updated"]} for row in rows]
