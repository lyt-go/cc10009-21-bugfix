"""进程内锁与加锁顺序约定。

平台当前使用内存仓库，并发请求（FastAPI 线程池）下靠这里的可重入锁保证状态一致。
所有涉及「采集质量 + 内容提取」两侧数据的操作，统一按
**先 quality 后 content** 的顺序取锁，避免交叉加锁造成死锁。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class LockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def get(self, name: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(name)
            if lock is None:
                lock = threading.RLock()
                self._locks[name] = lock
            return lock

    @contextmanager
    def quality_then_content(self) -> Iterator[None]:
        """质量报告与字段提取两侧联动写操作的统一加锁入口。"""
        quality_lock = self.get("qualityreport")
        content_lock = self.get("contentextract")
        with quality_lock:
            with content_lock:
                yield


locks = LockRegistry()
