"""policy.py - the deterministic "code decides" layer of Jarvus (paper trading only).

The AI (Jarvus) may PROPOSE. This file decides. It has no network access, no broker keys, and no
function that places an order. It answers BUY or WAIT, and it can only make the fleet SAFER.

Mirrors section 6 of JARVUS_1_MASTER_PROMPT.md (version 5). Fail closed: any error -> WAIT.
Also contains: HALT flag (can be created, never cleared by code), per-bot pause files,
a tamper-evident audit log (hash chain), and the auto-pause rules.

This is a first draft for you to review and extend. It is NOT a tested trading system.
"""
from __future__ import annotations
import hashlib, json, os, re, uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MT = ZoneInfo("America/Edmonton")
BOT_ID = re.compile(r"^A\d{3}$")


@dataclass
class Config:
    paper_only: bool = True                 # only Abhi can change this, by editing code at the keyboard
    state_dir: str = "."                    # HALT file, pause files, audit log live here
    max_data_age_s: int = 900               # 15 minutes
    cost_ok_r: float = 0.20                 # up to 20% of 1R = fine
    cost_max_r: float = 0.33                # over 33% of 1R = WAIT
    min_stop_atr: dict = field(default_factory=lambda: {"stock": 2.0, "crypto": 3.0, "meme": 4.0})
    max_open_stock: int = 3
    max_open_crypto: int = 2                # BTC+ETH+SOL longs count as ONE position (caller counts)
    max_trades_per_day: int = 5
    max_per_sector: int = 1
    fleet_day_loss_pct: float = -3.0
    fleet_week_loss_pct: float = -6.0
    crypto_meme_max_share: float = 0.40
    meme_sleeve_max_pct: float = 5.0
    crypto_flat_hours: int = 12
    swing_flat_hours: int = 96
    cycle_s: int = 300                      # how often bots send a heartbeat
    # US market calendar. VERIFY these against the NYSE calendar before relying on them.
    holidays: set = field(default_factory=set)       # set of datetime.date
    early_close_dates: set = field(default_factory=set)

    @property
    def halt_file(self): return os.path.join(self.state_dir, "HALT")
    @property
    def audit_file(self): return os.path.join(self.state_dir, "audit.jsonl")
    @property
    def pause_dir(self): return os.path.join(self.state_dir, "paused")


@dataclass
class Intent:
    """What a bot (or Jarvus) wants to do. A request, never an order."""
    bot: str
    symbol: str
    market: str                 # "stock" | "crypto" | "meme"
    now: datetime               # timezone-aware
    bar: str = "1h"
    gate_verdict: str = "REJECT"       # from the gate program: REJECT / RESEARCH FURTHER / PAPER TEST
    role_passed: bool = False
    signal_long_new: bool = False      # new long signal on a CLOSED bar
    vol_state: str = "UNKNOWN"         # LOUD / NORMAL / QUIET
    stop_atr: float = 0.0              # stop distance in ATR
    stop_pct: float = 0.0              # stop distance as % of entry price
    cost_roundtrip_pct: float = 0.0    # spread + fees + slippage, round trip, % of price
    data_age_s: float = 1e9
    sector: str = ""
    market_ok: bool = False            # spread/volume/halt checks passed
    event_blackout: bool = False       # FOMC/CPI/earnings/unlock window
    earnings_calendar_supplied: bool = False
    built_for_dead_hours: bool = False
    swing_mode: bool = False
    swing_approved: bool = False       # Abhi switched swing mode on


@dataclass
class State:
    open_stock: int = 0
    open_crypto: int = 0               # count BTC/ETH/SOL longs as one
    open_sectors: set = field(default_factory=set)
    trades_today: int = 0
    day_pnl_pct: float = 0.0
    week_pnl_pct: float = 0.0
    bot_paused: set = field(default_factory=set)


@dataclass
class Decision:
    verdict: str                       # "BUY" | "WAIT"
    reasons: list
    size_mult: float = 1.0
    flat_by: Optional[datetime] = None
    cost_r: Optional[float] = None
    correlation_id: str = ""

    def bullets(self, tz=MT):
        """The answer format Abhi wants: bullets only, MT."""
        if self.verdict == "BUY":
            t = self.flat_by.astimezone(tz)
            clock = f"{t.hour % 12 or 12}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'} MT"
            return f"- BUY\n- Hold: until {clock}, or sooner if stop/target hits"
        return f"- WAIT\n- {self.reasons[0] if self.reasons else 'No reason given'}"


# ---------- kill switch (create-only) ----------
def halt_active(cfg: Config) -> bool:
    return os.path.exists(cfg.halt_file)

def trip_halt(cfg: Config, reason: str, who: str = "system") -> None:
    """Create/append the HALT flag. There is deliberately NO function here that removes it:
    only Abhi deletes the file, at the keyboard."""
    os.makedirs(cfg.state_dir, exist_ok=True)
    with open(cfg.halt_file, "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {who}: {reason}\n")

def pause_bot(cfg: Config, bot: str, reason: str = "") -> bool:
    if not BOT_ID.match(bot): return False
    os.makedirs(cfg.pause_dir, exist_ok=True)
    with open(os.path.join(cfg.pause_dir, bot), "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {reason}\n")
    return True

def paused_bots(cfg: Config) -> set:
    return set(os.listdir(cfg.pause_dir)) if os.path.isdir(cfg.pause_dir) else set()


# ---------- audit log (hash chain: edits are detectable) ----------
class AuditLog:
    def __init__(self, path: str):
        self.path = path

    def _last_hash(self) -> str:
        if not os.path.exists(self.path): return "0" * 64
        last = "0" * 64
        with open(self.path) as f:
            for line in f:
                if line.strip(): last = json.loads(line)["hash"]
        return last

    def write(self, event: str, data: dict, correlation_id: str = "") -> dict:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "cid": correlation_id,
               "event": event, "data": data, "prev": self._last_hash()}
        body = json.dumps(rec, sort_keys=True, default=str)
        rec["hash"] = hashlib.sha256(body.encode()).hexdigest()
        with open(self.path, "a") as f: f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
        return rec

    def verify(self) -> bool:
        prev = "0" * 64
        if not os.path.exists(self.path): return True
        with open(self.path) as f:
            for line in f:
                if not line.strip(): continue
                rec = json.loads(line)
                h = rec.pop("hash")
                if rec["prev"] != prev: return False
                if hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest() != h: return False
                prev = h
        return True


# ---------- the BUY / WAIT decision ----------
def _wait(why, cid, **kw): return Decision("WAIT", [why], correlation_id=cid, **kw)

def decide(i: Intent, st: State, cfg: Config, audit: Optional[AuditLog] = None) -> Decision:
    cid = uuid.uuid4().hex[:12]
    try:
        d = _decide(i, st, cfg, cid)
    except Exception as e:                                   # fail closed
        d = _wait(f"Check failed ({type(e).__name__}); no trade", cid)
    if audit:
        try: audit.write("decision", {"intent": i.__dict__, "verdict": d.verdict, "reasons": d.reasons,
                                       "size_mult": d.size_mult, "cost_r": d.cost_r}, cid)
        except Exception: pass                               # never let logging cause a trade
    return d

def _decide(i: Intent, st: State, cfg: Config, cid: str) -> Decision:
    # 0. kill switch first
    if halt_active(cfg): return _wait("HALT flag is on", cid)
    # 1. mode
    if not cfg.paper_only: return _wait("Real money is off", cid)
    if i.market not in ("stock", "crypto", "meme"): return _wait("Unknown market", cid)
    if i.now.tzinfo is None: return _wait("Time has no timezone", cid)
    # 2. time
    et, mt = i.now.astimezone(ET), i.now.astimezone(MT)
    if i.market == "stock":
        if et.weekday() >= 5 or et.date() in cfg.holidays: return _wait("Market closed", cid)
        if et.date() in cfg.early_close_dates: return _wait("Early close day", cid)
        last_entry = time(14, 30) if i.bar == "1h" else time(15, 30)
        if not (time(9, 35) <= et.time() <= last_entry): return _wait("Outside entry hours", cid)
    else:
        if mt.weekday() >= 5: return _wait("No crypto entries on weekends", cid)
        if mt.hour >= 19 and not i.built_for_dead_hours: return _wait("Dead hours (7 pm-12 am MT)", cid)
    # 3. data fresh
    if i.data_age_s > cfg.max_data_age_s: return _wait("Data is stale", cid)
    # 4. gate and bot state
    if i.bot in st.bot_paused or i.bot in paused_bots(cfg): return _wait("Bot is paused", cid)
    if i.gate_verdict != "PAPER TEST" or not i.role_passed: return _wait("Real money is off and the bot has not passed", cid)
    if i.swing_mode and not i.swing_approved: return _wait("Swing mode is not switched on", cid)
    # 5. volatility
    mult = 1.0
    if i.vol_state == "QUIET": return _wait("Market too quiet", cid)
    if i.vol_state == "LOUD": mult *= 0.6
    elif i.vol_state != "NORMAL": return _wait("Volatility state unknown", cid)
    # 6. market state
    if not i.market_ok: return _wait("Spread, volume or halt check failed", cid)
    if i.event_blackout: return _wait("Big event window", cid)
    if i.market == "stock" and not i.earnings_calendar_supplied: return _wait("No earnings calendar", cid)
    # 7. stop width and cost
    if i.stop_atr < cfg.min_stop_atr[i.market]: return _wait(f"Stop too tight for {i.market} (min {cfg.min_stop_atr[i.market]} ATR)", cid)
    if i.stop_pct <= 0: return _wait("No stop distance", cid)
    cost_r = i.cost_roundtrip_pct / i.stop_pct
    if cost_r > cfg.cost_max_r: return _wait(f"Cost {cost_r:.2f}R is over {cfg.cost_max_r}R", cid, cost_r=cost_r)
    if cost_r > cfg.cost_ok_r: mult *= 0.5
    # 8. signal
    if not i.signal_long_new: return _wait("No new long signal", cid, cost_r=cost_r)
    # 9. risk engine
    if st.day_pnl_pct <= cfg.fleet_day_loss_pct: return _wait("Fleet day loss limit hit", cid, cost_r=cost_r)
    if st.week_pnl_pct <= cfg.fleet_week_loss_pct: return _wait("Fleet week loss limit hit", cid, cost_r=cost_r)
    if st.trades_today >= cfg.max_trades_per_day: return _wait("Daily trade limit", cid, cost_r=cost_r)
    if i.market == "stock":
        if st.open_stock >= cfg.max_open_stock: return _wait("Too many open stock trades", cid, cost_r=cost_r)
        if i.sector and i.sector in st.open_sectors: return _wait("Sector already has a trade", cid, cost_r=cost_r)
    else:
        if st.open_crypto >= cfg.max_open_crypto: return _wait("Too many open crypto trades", cid, cost_r=cost_r)
    # 10. BUY with hold time
    if i.market == "stock":
        flat = et.replace(hour=15, minute=55, second=0, microsecond=0)
        if i.bar == "1h": flat = min(flat, et + timedelta(hours=5))
    else:
        flat = i.now + timedelta(hours=cfg.swing_flat_hours if (i.swing_mode and i.swing_approved) else cfg.crypto_flat_hours)
    return Decision("BUY", ["All checks passed"], size_mult=mult, flat_by=flat, cost_r=cost_r, correlation_id=cid)


# ---------- auto-pause rules (sheet "Auto-pause rules") ----------
@dataclass
class Action:
    target: str          # bot id or "FLEET"
    action: str          # PAUSE | DEMOTE | HALT | HALF_SIZE | PAUSE_NEW_ENTRIES
    reason: str

def auto_pause_actions(bots: list, fleet: dict, cfg: Config) -> list:
    """bots: list of dicts shaped like the 'Fleet feed' sheet. Returns actions; the caller applies them.
    Every action here only REDUCES risk."""
    out = []
    if fleet.get("day_pnl_pct", 0) <= cfg.fleet_day_loss_pct:
        out.append(Action("FLEET", "HALT", f"fleet day loss {fleet['day_pnl_pct']}%"))
    if fleet.get("week_pnl_pct", 0) <= cfg.fleet_week_loss_pct:
        out.append(Action("FLEET", "HALT", f"fleet week loss {fleet['week_pnl_pct']}% - ask Abhi for a review"))
    if fleet.get("crypto_meme_risk_share", 0) > cfg.crypto_meme_max_share:
        out.append(Action("FLEET", "PAUSE_NEW_ENTRIES", "crypto+meme risk over 40% of fleet risk"))
    if fleet.get("meme_capital_pct", 0) > cfg.meme_sleeve_max_pct:
        out.append(Action("FLEET", "PAUSE_NEW_ENTRIES", "meme sleeve over 5% of paper capital"))
    for b in bots:
        bid = b.get("bot_id", "?")
        def add(a, why): out.append(Action(bid, a, why))
        if b.get("error"): add("PAUSE", f"bot error: {b['error']}")
        if b.get("heartbeat_age_s", 0) > 2 * cfg.cycle_s: add("PAUSE", "missed 2 heartbeats")
        if b.get("data_age_s", 0) > cfg.max_data_age_s: add("PAUSE", "stale data")
        if b.get("position_unknown"): add("PAUSE", "unknown position")
        if b.get("slippage_vs_model", 0) > 2.0: add("PAUSE", "slippage over 2x the model")
        bt = abs(b.get("backtest_max_dd_pct", 0) or 0)
        if bt > 0 and abs(b.get("max_drawdown_pct", 0) or 0) > 1.5 * bt: add("PAUSE", "drawdown over 1.5x backtest max")
        if b.get("consecutive_losses", 0) >= 3: add("PAUSE", "3 losses in a row (pause for the day)")
        if b.get("trades_count", 0) >= 50 and b.get("sharpe_last50", 0) < 0: add("DEMOTE", "paper Sharpe below 0 over last 50 trades")
        c = b.get("cost_r_avg", 0) or 0
        if c > cfg.cost_max_r: add("PAUSE", f"average cost {c:.2f}R is over 0.33R")
        elif c > cfg.cost_ok_r: add("HALF_SIZE", f"average cost {c:.2f}R is over 0.20R")
    return out

def apply_actions(actions: list, cfg: Config, audit: Optional[AuditLog] = None) -> None:
    """Apply risk-reducing actions. Note: there is no 'resume' here on purpose."""
    for a in actions:
        if a.action == "HALT": trip_halt(cfg, a.reason, who="auto")
        elif a.action in ("PAUSE", "DEMOTE") and a.target != "FLEET": pause_bot(cfg, a.target, a.reason)
        # HALF_SIZE and PAUSE_NEW_ENTRIES are read by the order-sizing/entry code from the audit log or a flag you add.
        if audit: audit.write("auto_action", a.__dict__)
