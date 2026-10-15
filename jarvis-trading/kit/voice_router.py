"""voice_router.py - turns what you SAY into one of four safe commands, with plain code (no AI).

Why no AI here: a spoken or texted sentence can carry hidden instructions ("halt, then buy bitcoin and
ignore your rules"). A fixed-list matcher cannot be talked into anything. It only accepts the WHOLE
sentence if it exactly matches a known command. Everything else is refused.

Allowed: STATUS, REPORT, HALT, PAUSE <bot id>. Nothing that adds risk. No resume. No orders.
Only a VERIFIED caller (your own number / chat id, checked by the phone layer) is obeyed at all.
Caller ID can be faked, so even verified callers get only risk-reducing or read-only commands.
"""
from __future__ import annotations
import json, os, re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import policy

NUM = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
       "six": "6", "seven": "7", "eight": "8", "nine": "9"}
REFUSAL = "Not allowed by voice. Do it at the keyboard."

@dataclass
class Route:
    kind: str                       # "command" | "refused"
    command: Optional[str] = None   # STATUS | REPORT | HALT | PAUSE
    arg: Optional[str] = None
    why: str = ""

def normalize(text: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    words = [NUM.get(w, w) for w in t.split()]
    t = " ".join(words)
    t = re.sub(r"\b(\d) (?=\d)", r"\1", t)          # "0 7 3" -> "073" (repeat for 3 digits)
    t = re.sub(r"\b(\d) (?=\d)", r"\1", t)
    t = re.sub(r"\ba (\d{3})\b", r"a\1", t)         # "a 073" -> "a073"
    return t.strip()

_PREFIX = r"(?:jarvus |jarvis )?(?:please )?"
_PATTERNS = [
    ("STATUS", re.compile(r"^" + _PREFIX + r"(?:status|what is the status|whats the status|how is the fleet)$")),
    ("REPORT", re.compile(r"^" + _PREFIX + r"(?:report|daily report|give me the report|how did today go|how did we do today)$")),
    ("HALT",   re.compile(r"^" + _PREFIX + r"(?:halt|halt everything|halt all|stop everything|emergency stop)$")),
    ("PAUSE",  re.compile(r"^" + _PREFIX + r"pause (?:bot )?(a\d{3})$")),
]

def route(text: str, verified: bool) -> Route:
    if not verified:
        return Route("refused", why="caller not verified")
    t = normalize(text)
    for name, rx in _PATTERNS:
        m = rx.match(t)
        if m: return Route("command", name, (m.group(1).upper() if name == "PAUSE" else None))
    return Route("refused", why="not on the allowed list")

def speakable(text: str) -> str:
    """Make text safe and clear for a text-to-speech voice."""
    t = re.sub(r"https?://\S+", "", text)
    t = t.replace("$", " dollars ").replace("%", " percent").replace("- ", "")
    t = re.sub(r"[`*_#<>]", "", t)
    return re.sub(r"\s+", " ", t).strip()

def spoken_report(summary: dict, now: Optional[datetime] = None, max_age_min: int = 30) -> str:
    """30-second call script from the reporter's summary dict. Every number comes from the file."""
    now = now or datetime.now(timezone.utc)
    if summary.get("empty"): return "Paper only. No paper trades logged yet. Say HALT to stop everything."
    parts = ["Paper only."]
    gen = summary.get("generated_at")
    if gen:
        age = (now - datetime.fromisoformat(gen)).total_seconds() / 60
        if age > max_age_min: parts.append(f"Warning: this report is {int(age)} minutes old.")
    p = summary["pnl_today"]; pct = summary["pnl_today_pct"]
    parts.append(f"Today, paper {'profit' if p >= 0 else 'loss'} of {abs(p):.2f} dollars, {abs(pct):.2f} percent.")
    parts.append(f"{summary['trades_today']} trades: {summary['wins']} won, {summary['losses']} lost.")
    if summary.get("halted_bots"): parts.append("Paused bots: " + ", ".join(summary["halted_bots"]) + ".")
    if summary.get("halt_flag"): parts.append("The HALT flag is on.")
    parts.append("Say HALT to stop everything.")
    return speakable(" ".join(parts))

def execute(r: Route, cfg: policy.Config, report_path: str, known_bots: Optional[set] = None,
            audit: Optional[policy.AuditLog] = None) -> str:
    """Run a routed command. Returns the sentence to speak."""
    if audit: audit.write("voice_route", {"kind": r.kind, "command": r.command, "arg": r.arg, "why": r.why})
    if r.kind == "refused":
        return "Not verified." if r.why == "caller not verified" else REFUSAL
    if r.command == "HALT":
        policy.trip_halt(cfg, "voice HALT", who="voice")
        return "Halting everything now. Nothing will restart until you do it at the keyboard."
    if r.command == "PAUSE":
        if known_bots is not None and r.arg not in known_bots: return f"I do not know bot {r.arg}."
        return f"Paused {r.arg}." if policy.pause_bot(cfg, r.arg, "voice PAUSE") else "That is not a valid bot id."
    # STATUS and REPORT are read-only
    try:
        with open(report_path) as f: s = json.load(f)
    except Exception:
        return "I cannot read the report file. I will not guess numbers."
    text = spoken_report(s)
    if r.command == "STATUS":
        flag = "The HALT flag is on. " if policy.halt_active(cfg) else ""
        n_paused = len(policy.paused_bots(cfg))
        return speakable(f"{flag}{n_paused} bots paused. {text}")
    return text
