"""会话持久化：包装 easycode.storage.Store（会话行 + 事件流）。

会话行 data 是无 schema JSON（workspace/model/tools/tokens/...）；事件流即消息与
工具执行的完整记录；steering 三原语（排队/原子弹出/清空）直接通用。
"""
from __future__ import annotations

from pathlib import Path

from easycode.storage import Store


class SessionStore:
    def __init__(self, path: Path):
        self.store = Store(path)

    def create(self, session_id, data):
        self.store.create(session_id, data)

    def update(self, session_id, **changes):
        self.store.update(session_id, **changes)

    def get(self, session_id):
        return self.store.get(session_id)

    def list(self):
        return self.store.list()

    def delete(self, session_id):
        return self.store.delete(session_id)

    def events(self, session_id, after=0):
        return self.store.events(session_id, after)

    def event(self, session_id, kind, data):
        return self.store.event(session_id, kind, data)

    # steering 三原语：排队 / 原子弹出 / 清空（取消即丢弃，插话不得复活）。
    def queue(self, session_id, text):
        return self.store.queue_instruction(session_id, text)

    def take_queued(self, session_id):
        return self.store.take_queued(session_id)

    def clear_queued(self, session_id):
        self.store.clear_queued(session_id)
