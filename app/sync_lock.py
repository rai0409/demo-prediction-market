from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
from typing import Iterator


def sync_lock_path(db_path: str) -> Path | None:
    if db_path == ":memory:":
        return None
    return Path(db_path).resolve().with_name(Path(db_path).resolve().name + ".sync.lock")


@contextmanager
def try_sync_lock(db_path: str) -> Iterator[bool]:
    path = sync_lock_path(db_path)
    if path is None:
        yield True
        return
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
