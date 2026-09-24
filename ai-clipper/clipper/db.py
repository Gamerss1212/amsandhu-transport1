"""SQLite state: trend history, seen uploads, processed videos."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS short_videos (
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    data TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (platform, video_id)
);
CREATE TABLE IF NOT EXISTS trend_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    n_videos INTEGER NOT NULL,
    profile TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    video_id TEXT PRIMARY KEY,
    channel_id TEXT,
    channel TEXT,
    title TEXT,
    published REAL,
    seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    scheduled_at REAL NOT NULL,
    status TEXT NOT NULL,
    caption TEXT,
    remote_id TEXT,
    error TEXT,
    attempts INTEGER DEFAULT 0,
    posted_at REAL,
    stats TEXT,
    stats_at REAL
);
CREATE TABLE IF NOT EXISTS processed (
    video_id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT NOT NULL,
    n_clips INTEGER DEFAULT 0,
    processed_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def _exec(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            self._conn.commit()
            return rows

    # -- trend history
    def save_short_videos(self, videos: list[dict]) -> None:
        now = time.time()
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO short_videos VALUES (?, ?, ?, ?)",
                [(v["platform"], v["video_id"], json.dumps(v), now) for v in videos],
            )
            self._conn.commit()

    def load_short_videos(self, since: float) -> list[dict]:
        rows = self._exec("SELECT data FROM short_videos WHERE fetched_at >= ?", (since,))
        return [json.loads(r["data"]) for r in rows]

    def save_profile(self, profile: dict) -> None:
        self._exec("INSERT INTO trend_profiles (created_at, n_videos, profile) VALUES (?, ?, ?)",
                   (time.time(), profile.get("n_videos", 0), json.dumps(profile)))

    def latest_profile(self) -> dict | None:
        rows = self._exec("SELECT profile, created_at FROM trend_profiles ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        return {**json.loads(rows[0]["profile"]), "created_at": rows[0]["created_at"]}

    # -- posting schedule (TikTok / Instagram)
    def add_post(self, folder: str, name: str, platform: str, at: float, caption: str) -> int:
        with self._lock:
            cur = self._conn.execute("INSERT INTO posts (folder, name, platform, scheduled_at, status, caption) "
                                     "VALUES (?, ?, ?, ?, 'scheduled', ?)", (folder, name, platform, at, caption))
            self._conn.commit()
            return int(cur.lastrowid)

    def posts(self, where: str = "1=1", params=()) -> list[dict]:
        rows = self._exec(f"SELECT * FROM posts WHERE {where} ORDER BY scheduled_at", params)
        return [{**dict(r), "stats": json.loads(r["stats"]) if r["stats"] else None} for r in rows]

    def update_post(self, post_id: int, **fields) -> None:
        if "stats" in fields and fields["stats"] is not None:
            fields["stats"] = json.dumps(fields["stats"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE posts SET {cols} WHERE id = ?", (*fields.values(), post_id))

    # -- real-time uploads
    def add_upload(self, video_id: str, channel_id: str, channel: str, title: str,
                   published: float) -> bool:
        """Returns True when the upload is new."""
        rows = self._exec("SELECT 1 FROM uploads WHERE video_id = ?", (video_id,))
        if rows:
            return False
        self._exec("INSERT INTO uploads VALUES (?, ?, ?, ?, ?, ?)",
                   (video_id, channel_id, channel, title, published, time.time()))
        return True

    def recent_uploads(self, limit: int = 50) -> list[dict]:
        rows = self._exec(
            "SELECT u.*, p.status FROM uploads u LEFT JOIN processed p USING (video_id) "
            "ORDER BY u.published DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # -- processing state
    def is_processed(self, video_id: str) -> bool:
        return bool(self._exec("SELECT 1 FROM processed WHERE video_id = ?", (video_id,)))

    def mark_processed(self, video_id: str, title: str, status: str, n_clips: int = 0) -> None:
        self._exec("INSERT OR REPLACE INTO processed VALUES (?, ?, ?, ?, ?)",
                   (video_id, title, status, n_clips, time.time()))
