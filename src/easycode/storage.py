"""SQLite 事件流存储：会话行 + 事件流 + steering 原语。

会话行（tasks 表）是无 schema JSON，事件流（events 表）即完整执行记录；
SSE 断线续传依赖事件游标，steering 三原语（排队/原子弹出/清空）在这里实现。
所有读写共用一把 RLock：外部轮询与工作线程的密集写入串行化，否则 sqlite
会稳定抛 READONLY（本机实测 22～38 次写入即触发）。
"""
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

    def create(self, session_id, data):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO tasks VALUES(?,?,?,?)", (session_id, time.time(), "queued", json.dumps(data)))

    def update(self, session_id, **changes):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT data,status FROM tasks WHERE id=?", (session_id,)).fetchone()
            if not row:
                raise KeyError(session_id)
            data = json.loads(row["data"])
            data.update(changes)
            db.execute("UPDATE tasks SET status=?,data=? WHERE id=?",
                       (changes.get("status", row["status"]), json.dumps(data), session_id))

    def get(self, session_id):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        return {**json.loads(row["data"]), "id": row["id"], "created": row["created"], "status": row["status"]}

    def list(self):
        with self.lock, self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM tasks ORDER BY created DESC LIMIT 60")]
        return [self.get(session_id) for session_id in ids]

    def event(self, session_id, kind, data):
        with self.lock, self.connect() as db:
            cursor = db.execute("INSERT INTO events(task_id,created,kind,data) VALUES(?,?,?,?)",
                                (session_id, time.time(), kind, json.dumps(data)))
            return cursor.lastrowid

    def delete(self, session_id):
        """Remove a session and all of its events; returns whether a row was deleted."""
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM events WHERE task_id=?", (session_id,))
            cursor = db.execute("DELETE FROM tasks WHERE id=?", (session_id,))
            return cursor.rowcount > 0

    def events(self, session_id, after=0):
        with self.lock, self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT 500",
                              (session_id, after)).fetchall()
        return [{"id": r["id"], "kind": r["kind"], "created": r["created"],
                 "data": json.loads(r["data"])} for r in rows]

    def queue_instruction(self, session_id, text):
        """Steering: append a user instruction to the running session's queue; returns queue depth."""
        with self.lock, self.connect() as db:
            row = db.execute("SELECT data FROM tasks WHERE id=?", (session_id,)).fetchone()
            if not row:
                raise KeyError(session_id)
            data = json.loads(row["data"])
            queue = data.get("queued") or []
            queue.append({"text": text, "at": time.time()})
            data["queued"] = queue
            db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(data), session_id))
            return len(queue)

    def take_queued(self, session_id):
        """Steering: atomically pop the whole queue as one combined instruction; None when empty."""
        with self.lock, self.connect() as db:
            row = db.execute("SELECT data FROM tasks WHERE id=?", (session_id,)).fetchone()
            if not row:
                return None
            data = json.loads(row["data"])
            queue = data.pop("queued", None)
            if not queue:
                return None
            db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(data), session_id))
            return "\n\n".join(item["text"] for item in queue)

    def clear_queued(self, session_id):
        """Steering: drop pending instructions (cancel means stop, queued words must not survive)."""
        with self.lock, self.connect() as db:
            row = db.execute("SELECT data FROM tasks WHERE id=?", (session_id,)).fetchone()
            if not row:
                return
            data = json.loads(row["data"])
            if data.pop("queued", None) is not None:
                db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(data), session_id))

    def recover_interrupted(self):
        """History survives restart; running sessions are marked interrupted, never silently replayed.

        会话本身可继续（continue 语义），终态由用户决定是否追加指令。
        """
        with self.connect() as db:
            rows = db.execute("SELECT id FROM tasks WHERE status IN ('running','queued')").fetchall()
        for row in rows:
            session_id = row["id"]
            self.update(session_id, status="interrupted",
                        error="服务已重启，会话中断；历史完整保留，可继续发送指令续跑。")
            self.event(session_id, "interrupted", {"reason": "server_restart"})
