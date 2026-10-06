"""上传 EPUB 的内存会话存储（带 TTL）。

会话只保存原始字节；每次预览/下载都基于原始字节重新解析，避免拆分过程中
对 XML 树的就地修改在多次请求之间串状态。
"""

from __future__ import annotations

import threading
import time
import uuid


class Session:
    def __init__(self, data: bytes, filename: str):
        self.id = uuid.uuid4().hex
        self.data = data
        self.filename = filename
        self.created = time.time()
        self.source_href: str | None = None
        # 最近一次预览结果（仅用于前端展示的轻量信息）
        self.last_plan_meta: dict | None = None


class SessionStore:
    def __init__(self, ttl_seconds: int = 3600):
        self.ttl = ttl_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, data: bytes, filename: str) -> Session:
        self._purge()
        s = Session(data, filename)
        with self._lock:
            self._sessions[s.id] = s
        return s

    def get(self, session_id: str) -> Session:
        self._purge()
        with self._lock:
            s = self._sessions.get(session_id)
        if s is None:
            raise KeyError(session_id)
        return s

    def drop(self, session_id: str):
        with self._lock:
            self._sessions.pop(session_id, None)

    def _purge(self):
        now = time.time()
        with self._lock:
            stale = [sid for sid, s in self._sessions.items()
                     if now - s.created > self.ttl]
            for sid in stale:
                self._sessions.pop(sid, None)
