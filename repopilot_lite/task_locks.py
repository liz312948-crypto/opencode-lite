from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock, RLock


class _TaskLockEntry:
    def __init__(self) -> None:
        self.lock = RLock()
        self.references = 0


class TaskLockManager:
    """Serializes one task's workflow inside a single Python process.

    Entries are reference counted and removed after the last holder or waiter
    leaves, so completed task IDs do not accumulate in the lock table.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _TaskLockEntry] = {}
        self._entries_guard = Lock()

    @contextmanager
    def lock(self, task_id: str) -> Iterator[None]:
        if not task_id:
            raise ValueError("task_id must not be empty")

        with self._entries_guard:
            entry = self._entries.get(task_id)
            if entry is None:
                entry = _TaskLockEntry()
                self._entries[task_id] = entry
            entry.references += 1

        acquired = False
        try:
            entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            with self._entries_guard:
                entry.references -= 1
                if entry.references == 0 and self._entries.get(task_id) is entry:
                    del self._entries[task_id]

    @property
    def active_task_count(self) -> int:
        """Return the number of task IDs currently holding or waiting on a lock."""

        with self._entries_guard:
            return len(self._entries)


default_task_lock_manager = TaskLockManager()
