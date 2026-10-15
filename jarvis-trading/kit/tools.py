"""tools.py - the READ-ONLY (plus risk-reducing) tools that a voice agent or MCP client may call.
There is no tool here that places, changes or cancels an order, and none that resumes anything."""
from __future__ import annotations
import json, os
from datetime import datetime, timezone
import policy
import jarvus_reporter

def load_report(path: str) -> dict:
    with open(path) as f: return json.load(f)

def report_age_minutes(summary: dict, now=None):
    gen = summary.get("generated_at")
    if not gen: return None
    now = now or datetime.now(timezone.utc)
    return round((now - datetime.fromisoformat(gen)).total_seconds() / 60, 1)

def fleet_status(cfg: policy.Config, report_path: str) -> dict:
    try: s = load_report(report_path)
    except Exception as e: return {"ok": False, "error": f"cannot read report ({type(e).__name__}); do not guess numbers"}
    return {"ok": True, "paper_only": True, "summary": s, "report_age_minutes": report_age_minutes(s),
            "halt_flag": policy.halt_active(cfg), "paused_bots": sorted(policy.paused_bots(cfg))}

def daily_report_text(report_path: str) -> str:
    try: return jarvus_reporter.bullets(load_report(report_path))
    except Exception: return "- Cannot read the report file\n- Paper only."

def get_bot(fleet_path: str, bot_id: str) -> dict:
    if not policy.BOT_ID.match(bot_id): return {"ok": False, "error": "bad bot id"}
    try:
        with open(fleet_path) as f: fleet = json.load(f)
    except Exception: return {"ok": False, "error": "cannot read fleet file"}
    for b in fleet:
        if b.get("bot_id") == bot_id: return {"ok": True, "bot": b}
    return {"ok": False, "error": "unknown bot"}

def halt_all(cfg: policy.Config, reason: str = "requested by tool") -> dict:
    policy.trip_halt(cfg, reason[:200], who="tool")
    return {"ok": True, "halt_flag": True, "note": "Only Abhi can clear this, at the keyboard."}

def pause_one(cfg: policy.Config, bot_id: str) -> dict:
    return {"ok": policy.pause_bot(cfg, bot_id, "requested by tool")}
