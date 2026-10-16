"""memory.py - SQLite store for sessions, messages, long-term memories, tasks, alert rules, the notification
outbox and the tool audit. One local file (state/jarvis.db), WAL mode, per-call connections (thread safe).

What is NOT stored: secrets, PINs (only a salted hash lives in phone.py's file), API keys, raw audio,
or financial values as 'current truth' (those are always read live from the engine).
"""
from __future__ import annotations
import json, os, sqlite3, threading, time, uuid
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, channel TEXT, auth_level TEXT, scopes TEXT,
  started_at REAL, last_activity REAL, state TEXT, version INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
  channel TEXT, created_at REAL, meta TEXT);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, kind TEXT, text TEXT, source TEXT, confidence REAL,
  sensitivity TEXT, created_at REAL, updated_at REAL, expires_at REAL, corrected INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, kind TEXT, title TEXT, status TEXT, progress REAL, detail TEXT,
  created_at REAL, updated_at REAL, result TEXT, error TEXT, session_id TEXT);
CREATE TABLE IF NOT EXISTS alert_rules (id TEXT PRIMARY KEY, version INTEGER, rule TEXT, enabled INTEGER,
  created_at REAL, eval_state TEXT);
CREATE TABLE IF NOT EXISTS outbox (key TEXT PRIMARY KEY, rule_id TEXT, channel TEXT, state TEXT, message TEXT,
  created_at REAL, updated_at REAL, provider_id TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS tool_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, session_id TEXT, channel TEXT,
  tool TEXT, tier TEXT, args TEXT, ok INTEGER, error TEXT, ms INTEGER);
CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, severity TEXT, title TEXT,
  body TEXT, spoken INTEGER DEFAULT 0, seen INTEGER DEFAULT 0);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c; c.commit()
        finally:
            c.close()

    def q(self, sql, args=()):
        with self.conn() as c: return [dict(r) for r in c.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self._lock, self.conn() as c:
            cur = c.execute(sql, args); return cur.lastrowid

    # ---------- sessions ----------
    def session(self, sid: str | None, channel: str, auth_level: str, scopes: list) -> dict:
        now = time.time()
        if sid:
            r = self.q("SELECT * FROM sessions WHERE id=?", (sid,))
            if r:
                self.x("UPDATE sessions SET last_activity=? WHERE id=?", (now, sid))
                s = r[0]; s["state"] = json.loads(s["state"] or "{}"); s["scopes"] = json.loads(s["scopes"] or "[]")
                return s
        sid = sid or uuid.uuid4().hex
        self.x("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,0)", (sid, channel, auth_level, json.dumps(scopes), now, now, "{}"))
        return {"id": sid, "channel": channel, "auth_level": auth_level, "scopes": scopes, "state": {}, "version": 0}

    def save_state(self, sid: str, state: dict, version: int) -> bool:
        """Optimistic concurrency: only write if nobody else wrote since we read."""
        with self._lock, self.conn() as c:
            cur = c.execute("UPDATE sessions SET state=?, version=version+1 WHERE id=? AND version=?",
                            (json.dumps(state, default=str), sid, version))
            return cur.rowcount == 1

    # ---------- messages ----------
    def add_message(self, sid, role, content, channel, meta=None):
        self.x("INSERT INTO messages (session_id, role, content, channel, created_at, meta) VALUES (?,?,?,?,?,?)",
               (sid, role, content, channel, time.time(), json.dumps(meta or {})))

    def history(self, sid, limit=16) -> list:
        rows = self.q("SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?", (sid, limit))
        return list(reversed(rows))

    def recent_messages(self, limit=40) -> list:
        return self.q("SELECT session_id, role, content, channel, created_at FROM messages ORDER BY id DESC LIMIT ?", (limit,))

    def purge(self, retention_days: int = 30):
        cut = time.time() - retention_days * 86400
        self.x("DELETE FROM messages WHERE created_at < ?", (cut,))
        self.x("DELETE FROM tool_audit WHERE ts < ?", (cut,))
        self.x("DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?", (time.time(),))

    # ---------- long-term memory ----------
    def remember(self, text: str, kind: str = "preference", source: str = "owner", confidence: float = 1.0,
                 expires_days: float | None = None) -> str:
        mid = uuid.uuid4().hex[:10]; now = time.time()
        self.x("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,0)", (mid, kind, text[:500], source, confidence, "normal",
                                                                      now, now, now + expires_days * 86400 if expires_days else None))
        return mid

    def memories(self, query: str | None = None, limit: int = 30) -> list:
        if query:
            words = [w for w in query.lower().split() if len(w) > 2][:6]
            if words:
                cond = " OR ".join(["lower(text) LIKE ?"] * len(words))
                return self.q(f"SELECT * FROM memories WHERE {cond} ORDER BY updated_at DESC LIMIT ?",
                              [f"%{w}%" for w in words] + [limit])
        return self.q("SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,))

    def correct_memory(self, mid: str, text: str) -> bool:
        with self._lock, self.conn() as c:
            return c.execute("UPDATE memories SET text=?, updated_at=?, corrected=1 WHERE id=?", (text[:500], time.time(), mid)).rowcount == 1

    def forget(self, mid: str):
        self.x("DELETE FROM memories WHERE id=?", (mid,))

    # ---------- notifications ----------
    def notify(self, title: str, body: str, severity: str = "info") -> int:
        return self.x("INSERT INTO notifications (ts, severity, title, body) VALUES (?,?,?,?)", (time.time(), severity, title, body))

    def notifications(self, since_id: int = 0, limit: int = 30) -> list:
        return self.q("SELECT * FROM notifications WHERE id>? ORDER BY id DESC LIMIT ?", (since_id, limit))

    # ---------- audit ----------
    def audit_tool(self, sid, channel, tool, tier, args, ok, error, ms):
        self.x("INSERT INTO tool_audit (ts, session_id, channel, tool, tier, args, ok, error, ms) VALUES (?,?,?,?,?,?,?,?,?)",
               (time.time(), sid, channel, tool, tier, json.dumps(args, default=str)[:2000], int(bool(ok)), error, ms))

    def tool_audit(self, limit=50) -> list:
        return self.q("SELECT * FROM tool_audit ORDER BY id DESC LIMIT ?", (limit,))
