"""Generic single-writer-thread FIFO queue — the shared machinery behind
core/sessions.py's whole-file atomic JSON writes and core/eventlog.py's
append-only JSONL writes. Both persistence layers need the exact same
shape (never block the caller on disk I/O, process writes strictly in
enqueued order via one background daemon thread, swallow any per-item
failure so one bad write never stops later ones, offer a flush() the
caller can block on before the process exits) and previously each
reimplemented it from scratch with its own module-level `queue.Queue` +
`threading.Thread` + `threading.Lock`.

Not part of core's public API (see core/__init__.py) — an internal detail
private to the two persistence modules that use it, not something outside
code should depend on directly.
"""

import queue
import threading
from pathlib import Path
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class QueuedWriter(Generic[T]):
    """Wraps a `write_fn(path, item)` with a background daemon thread and
    FIFO queue so `enqueue()` never blocks its caller on disk I/O. The
    write function decides what "a write" means (atomic whole-file
    replace, append a JSONL line, ...) — this class only owns the
    threading/ordering/error-swallowing contract around it.

    The thread is started lazily, on first enqueue() — not at
    construction — so importing a module that creates one of these at
    module scope never spins up a thread until it's actually needed."""

    def __init__(self, write_fn: Callable[[Path, T], None], *, thread_name: str):
        self._write_fn = write_fn
        self._thread_name = thread_name
        self._queue: "queue.Queue[tuple[Path, T]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def enqueue(self, path: Path, item: T) -> None:
        """Enqueue `item` to be written to `path` by the background
        thread, in the order enqueue() was called (FIFO across all
        paths) — never blocks on I/O."""
        self._ensure_thread()
        self._queue.put((path, item))

    def flush(self) -> None:
        """Block until every write enqueued so far has actually been
        processed. Returns immediately if nothing has ever been
        enqueued (queue.Queue.join() on an untouched queue returns right
        away)."""
        self._queue.join()

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._loop, daemon=True, name=self._thread_name)
                self._thread.start()

    def _loop(self) -> None:
        while True:
            path, item = self._queue.get()
            try:
                self._write_fn(path, item)
            except Exception:
                # Swallow per-item — a single bad write (disk full,
                # permission denied) must not kill this thread, or every
                # write queued after it would silently never be
                # processed again.
                pass
            finally:
                self._queue.task_done()
