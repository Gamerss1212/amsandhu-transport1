"""tasks.py - background jobs (backtests, role tests, research) with progress, cancel and durable status.
At most 2 run at once; the rest queue. A finished task emits a 'task_completed' event for alert rules."""
from __future__ import annotations
import json, threading, time, uuid, traceback
from concurrent.futures import ThreadPoolExecutor


class TaskManager:
    def __init__(self, store, workers: int = 2, on_done=None):
        self.store, self.on_done = store, on_done
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jarvis-task")
        self.cancel_flags: dict = {}
        self.store.x("UPDATE tasks SET status='interrupted', updated_at=? WHERE status IN ('queued','running')", (time.time(),))

    def submit(self, kind: str, title: str, fn, session_id: str | None = None) -> str:
        tid = uuid.uuid4().hex[:10]; now = time.time()
        self.store.x("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)", (tid, kind, title, "queued", 0.0, "", now, now, None, None, session_id))
        ev = threading.Event(); self.cancel_flags[tid] = ev

        def progress(p: float, detail: str = ""):
            if ev.is_set(): raise InterruptedError("cancelled")
            self.store.x("UPDATE tasks SET progress=?, detail=?, updated_at=? WHERE id=?", (round(p, 3), detail[:200], time.time(), tid))

        def run():
            if ev.is_set(): return
            self.store.x("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (time.time(), tid))
            try:
                res = fn(progress, ev)
                self.store.x("UPDATE tasks SET status='done', progress=1, result=?, updated_at=? WHERE id=?",
                             (json.dumps(res, default=str)[:3_000_000], time.time(), tid))
                status = "done"
            except InterruptedError:
                self.store.x("UPDATE tasks SET status='cancelled', updated_at=? WHERE id=?", (time.time(), tid)); status = "cancelled"
            except Exception as e:
                self.store.x("UPDATE tasks SET status='failed', error=?, updated_at=? WHERE id=?",
                             (f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"[:2000], time.time(), tid)); status = "failed"
            if self.on_done:
                try: self.on_done(tid, kind, title, status)
                except Exception: pass

        self.pool.submit(run)
        return tid

    def get(self, tid: str, with_result: bool = True) -> dict | None:
        r = self.store.q("SELECT * FROM tasks WHERE id=?", (tid,))
        if not r: return None
        t = r[0]
        t["result"] = json.loads(t["result"]) if (with_result and t["result"]) else None
        return t

    def list(self, limit: int = 20) -> list:
        return self.store.q("SELECT id, kind, title, status, progress, detail, created_at, updated_at, error FROM tasks "
                            "ORDER BY created_at DESC LIMIT ?", (limit,))

    def cancel(self, tid: str) -> bool:
        ev = self.cancel_flags.get(tid)
        if not ev: return False
        ev.set()
        self.store.x("UPDATE tasks SET status='cancelled', updated_at=? WHERE id=? AND status='queued'", (time.time(), tid))
        return True
