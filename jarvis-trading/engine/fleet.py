"""fleet.py - the bot runner for the 99 bots (build-order steps 4, 7, 8, 9).

Every cycle, for every bot that is not PARKED:
  1. get CLOSED bars (fresh or cached; stale = WAIT)
  2. compute its strategy signal on the last closed bar (role's gate-chosen candidate, else candidate 1)
  3. ask policy.decide (kit) for BUY/WAIT - the code decides, never the AI
  4. BUY + gate passed -> paper book (fills next bar). Signal but not passed -> shadow book (no money).
  5. write the Fleet feed fields (heartbeat time + correlation id) to state/fleet.json
Then: mark positions, apply auto-pause actions (HALT / PAUSE / DEMOTE / HALF_SIZE / PAUSE_NEW_ENTRIES), write report.json.
"""
from __future__ import annotations
import json, os, threading, time, traceback, uuid
from datetime import date, datetime, timedelta, timezone
import policy, jarvus_reporter                                     # kit/
from . import library, volatility
from .backtest import MIN_STOP_ATR, VENUES, default_venue, COST_MAX
from .data_feed import BAR_SECONDS, ET, MT, DataService, nyse_session, market_kind, NYSE_HOLIDAYS, NYSE_EARLY_CLOSE
from .paper_broker import Book
from .strategies import Ctx, entry_signals, spec as make_spec
from .validation import gate_results

MAJORS = {"BTC-USD", "ETH-USD", "SOL-USD"}
# FOMC decision days 2026 (12:00 pm MT statement, 12:30 presser). VERIFY at federalreserve.gov.
FOMC_2026 = {date.fromisoformat(d) for d in ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
                                              "2026-09-16", "2026-10-28", "2026-12-09"]}


def event_blackout(now: datetime) -> bool:
    mt = now.astimezone(MT)
    if mt.date() in FOMC_2026 and 11 <= mt.hour < 13: return True
    return False


class Fleet:
    def __init__(self, data: DataService, state_dir: str, capital: float = 10000.0, venue_crypto: str = "ndax"):
        self.data, self.dir = data, state_dir
        self.cfg = policy.Config(state_dir=state_dir)
        self.cfg.holidays = set(NYSE_HOLIDAYS); self.cfg.early_close_dates = set(NYSE_EARLY_CLOSE)
        self.audit = policy.AuditLog(self.cfg.audit_file)
        self.paper = Book(state_dir, "paper", capital)
        self.shadow = Book(state_dir, "shadow", capital)
        self.venue_crypto = venue_crypto
        self.feed: dict = {}
        self.events: list = []
        self.totals: dict = {}
        self.last_cycle = None
        self.cycle_n = 0
        self.lock = threading.Lock()
        self.running = False
        self.progress = {"done": 0, "total": 0, "phase": "idle"}
        try: self.feed = {b["bot_id"]: b for b in json.load(open(os.path.join(state_dir, "fleet.json")))}
        except Exception: pass

    # ---------- helpers ----------
    def flags(self) -> dict:
        try: return json.load(open(os.path.join(self.dir, "flags.json")))
        except Exception: return {}

    def _write_flags(self, f: dict):
        json.dump(f, open(os.path.join(self.dir, "flags.json"), "w"), indent=1)

    def strategy_for(self, b: dict) -> str:
        g = gate_results(self.dir).get(b["role"])
        return (g or {}).get("chosen") or b["candidate_1"]

    def role_passed(self, role: str) -> tuple:
        g = gate_results(self.dir).get(role)
        return (g and g["verdict"] == "PAPER TEST"), (g or {}).get("verdict")

    def _event(self, e: dict):
        e["t"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.events.insert(0, e); del self.events[300:]

    # ---------- one bot ----------
    def run_bot(self, b: dict, now: datetime, halt: bool, flags: dict, marks: dict, refresh: bool) -> dict:
        cid = uuid.uuid4().hex[:12]
        bid, sym, bar = b["bot_id"], b["symbol"], b["bar"]
        mk = "meme" if library.is_meme(b) else market_kind(sym)
        row = {"bot_id": bid, "symbol": sym, "role": b["role"], "group": b["group"], "status": b["status"], "bar": bar,
               "correlation_id": cid, "error": "", "market": mk}
        prev = self.feed.get(bid, {})
        sid = self.strategy_for(b)
        passed, role_verdict = self.role_passed(b["role"])
        row["strategy"] = sid
        row["verdict"] = role_verdict or b["verdict"]
        if passed: row["status"] = "ACTIVE-PAPER"
        got = self.data.get(sym, bar, 400, refresh=refresh)
        if not got["ok"]:
            row.update(error=f"no data: {got['error']}", last_signal="NONE", decision="WAIT", reason="Need fresh prices",
                       heartbeat_time=prev.get("heartbeat_time"), data_age_s=None)
            return row
        bars = got["bars"]
        marks[sym] = bars.c[-1]
        sess = nyse_session(now)
        closed_market = mk == "stock" and not sess["open"]
        age = bars.age_s()
        # Bots sheet rule: stale if the last CLOSED bar is older than bar size + 15 min. So the number the risk
        # rules compare with 15 min is the LAG past the moment the next bar should have closed, not the raw age.
        lag = None if age is None else max(0.0, age - BAR_SECONDS[bar])
        row["bar_age_s"] = None if age is None else round(age)
        row["data_age_s"] = None if closed_market else round(lag if lag is not None else 1e9)
        row["last_bar_time"] = datetime.fromtimestamp(bars.last_close_time(), timezone.utc).isoformat(timespec="minutes")
        row["last_price"] = bars.c[-1]; row["source"] = bars.source
        sp = make_spec(sid, library.settings_for(sid))
        if sym in MAJORS and len(bars) >= 200:
            big = self.data.get(sym, bar, 3000)          # same full series as the portal panel
            vstates = volatility.gate(big["bars"])["states"][-len(bars):] if big["ok"] and len(big["bars"]) >= len(bars) else None
        else:
            vstates = None
        if vstates is None:
            vstates = [volatility.rule_state(bars, i) for i in range(len(bars))] if len(bars) > 60 else ["UNKNOWN"] * len(bars)
        bm = None
        if mk != "stock" and sym != "BTC-USD":
            g = self.data.get("BTC-USD", "1h", 400, refresh=False); bm = g["bars"] if g["ok"] else None
        ctx = Ctx(bars, "crypto" if mk == "meme" else mk, bm, vstates)
        sig = entry_signals(sp, ctx)
        a = ctx.atr(14)[-1]
        new_long = bool(sig[-1])
        row["last_signal"] = "LONG" if new_long else "NONE"
        row["vol_state"] = vstates[-1]
        stop_atr = max(sp["params"].get("stop_atr", 2.0), MIN_STOP_ATR[mk])
        if vstates[-1] == "LOUD": stop_atr *= 1.25
        stop_pct = (stop_atr * a / bars.c[-1] * 100) if a else 0.0
        fees = VENUES[default_venue(mk) if mk == "stock" else self.venue_crypto]
        cost_pct = fees["taker"] * 2
        rv = ctx.relvol(20)[-1]
        intent = policy.Intent(bot=bid, symbol=sym, market=mk, now=now, bar=bar, gate_verdict=row["verdict"] or "REJECT",
                               role_passed=bool(passed), signal_long_new=new_long, vol_state=vstates[-1], stop_atr=stop_atr,
                               stop_pct=stop_pct, cost_roundtrip_pct=cost_pct,
                               data_age_s=(lag if (not closed_market and lag is not None) else 1e9), market_ok=bool(rv and rv > 0.3),
                               event_blackout=event_blackout(now), earnings_calendar_supplied=False,
                               built_for_dead_hours=False, swing_mode=bool(sp["params"].get("swing")), swing_approved=False)
        st = policy.State(open_stock=sum(1 for p in self.paper.s["positions"].values() if p["market"] == "stock"),
                          open_crypto=min(1, sum(1 for p in self.paper.s["positions"].values() if p["symbol"] in MAJORS)) +
                                      sum(1 for p in self.paper.s["positions"].values() if p["market"] != "stock" and p["symbol"] not in MAJORS),
                          trades_today=self.paper.trades_today(),
                          day_pnl_pct=100 * self.paper.summary()["pnl_today"] / self.paper.s["capital"])
        # audit only when something happened (a signal) to keep the log readable; decisions are recomputed every cycle
        d = policy.decide(intent, st, self.cfg, self.audit if new_long else None)
        row.update(decision=d.verdict, reason=d.reasons[0] if d.reasons else "", cost_r=round(d.cost_r, 3) if d.cost_r else
                   (round(cost_pct / stop_pct, 3) if stop_pct else None), size_mult=d.size_mult,
                   bullets=d.bullets())
        if new_long and bars.t[-1] != prev.get("last_signal_bar_t"):
            row["last_signal_bar_t"] = bars.t[-1]
            flat_by = (d.flat_by.timestamp() if d.flat_by else
                       (bars.t[-1] + BAR_SECONDS[bar] + (12 * 3600 if mk != "stock" else 0)))
            if mk == "stock" and not d.flat_by:
                et = datetime.fromtimestamp(bars.t[-1], ET); flat_by = et.replace(hour=15, minute=55).timestamp()
            target_r = sp["params"].get("target_r") if sp["exit"] == "atr_bracket" else None
            if d.verdict == "BUY" and passed:
                r = self.paper.place(bid, sym, mk, bars.t[-1], bar, stop_atr, a, target_r, flat_by, d.size_mult, cost_pct,
                                     d.correlation_id, sid, flags, halt)
                self._event({"bot": bid, "event": f"PAPER order {sym}: {r['reason']}", "cid": d.correlation_id})
            else:
                r = self.shadow.place(bid, sym, mk, bars.t[-1], bar, stop_atr, a or 0, target_r, flat_by, 1.0, cost_pct,
                                      d.correlation_id, sid, {}, False) if a else {"ok": False}
                if r.get("ok"):
                    self._event({"bot": bid, "event": f"SHADOW signal {sym} ({sid}); live answer WAIT: {row['reason']}",
                                 "cid": d.correlation_id})
        else:
            row["last_signal_bar_t"] = prev.get("last_signal_bar_t")
        for e in self.paper.on_bars(bid, bars, halt=halt) + self.shadow.on_bars(bid, bars):
            self._event(e)
        # Fleet feed: per-bot paper stats
        closed = [c for c in self.paper.s["closed"] if c["bot"] == bid]
        today = datetime.now(MT).date().isoformat()
        losses = 0
        for c in reversed(closed):
            if c["r"] > 0: break
            losses += 1
        last_trade = max((c["date"] for c in closed), default=None)
        row.update(open_position=bid in self.paper.s["positions"], shadow_position=bid in self.shadow.s["positions"],
                   pnl_today_usd=round(sum(c["pnl"] for c in closed if c["date"] == today), 2),
                   pnl_week_usd=round(sum(c["pnl"] for c in closed if c["date"] >= (datetime.now(MT).date() - timedelta(days=7)).isoformat()), 2),
                   trades_count=len(closed), trades_today=sum(1 for c in closed if c["date"] == today),
                   win_rate=round(sum(1 for c in closed if c["r"] > 0) / len(closed), 3) if closed else None,
                   avg_r=round(sum(c["r"] for c in closed) / len(closed), 3) if closed else None,
                   cost_r_avg=round(sum(c["cost_r"] for c in closed) / len(closed), 3) if closed else 0,
                   consecutive_losses=losses, slippage_vs_model=1.0, max_drawdown_pct=0.0, backtest_max_dd_pct=0.0,
                   days_since_last_trade=(datetime.now(MT).date() - date.fromisoformat(last_trade)).days if last_trade else None,
                   heartbeat_time=datetime.now(timezone.utc).isoformat(timespec="seconds"), heartbeat_age_s=0,
                   data_stale=bool(got["stale"]) and not closed_market)
        return row

    # ---------- one cycle ----------
    def cycle(self, refresh_budget: int = 25) -> dict:
        if not self.lock.acquire(blocking=False): return {"ok": False, "error": "cycle already running"}
        try:
            self.running = True
            now = datetime.now(timezone.utc)
            halt = policy.halt_active(self.cfg)
            flags = self.flags()
            marks: dict = {}
            bots = [b for b in library.bots() if b["status"] != "PARKED"]
            self.progress = {"done": 0, "total": len(bots), "phase": "bots"}
            # refresh BTC first (benchmark for alts), then spend the refresh budget on the stalest symbols
            self.data.get("BTC-USD", "1h", 400)
            order = sorted(bots, key=lambda b: self.feed.get(b["bot_id"], {}).get("heartbeat_time") or "")
            budget = refresh_budget
            for b in order:
                prev = self.feed.get(b["bot_id"], {})
                lb = prev.get("last_bar_time")
                due = (not lb) or (time.time() - datetime.fromisoformat(lb).timestamp() > BAR_SECONDS[b["bar"]] + 60)
                if market_kind(b["symbol"]) == "stock" and not nyse_session(now)["open"] and lb: due = False
                refresh = due and budget > 0
                if refresh: budget -= 1
                try:
                    row = self.run_bot(b, now, halt, flags, marks, refresh)
                except Exception as e:
                    row = {**prev, "bot_id": b["bot_id"], "symbol": b["symbol"], "role": b["role"], "status": b["status"],
                           "error": f"{type(e).__name__}: {e}", "decision": "WAIT", "reason": "Check failed; no trade",
                           "trace": traceback.format_exc(limit=2)}
                self.feed[b["bot_id"]] = row
                self.progress["done"] += 1
            for bid, row in self.feed.items():                    # heartbeat ages
                hb = row.get("heartbeat_time")
                row["heartbeat_age_s"] = round(time.time() - datetime.fromisoformat(hb).timestamp()) if hb else None
            if halt and self.paper.s["positions"]:
                for e in self.paper.close_all("HALT: kill switch", marks): self._event(e)
            self.paper.snapshot_equity()
            self._totals(halt)
            self._auto_pause()
            self._write()
            self.cycle_n += 1
            self.last_cycle = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.progress["phase"] = "idle"
            return {"ok": True, "bots": len(bots), "cycle": self.cycle_n}
        finally:
            self.running = False
            self.lock.release()

    def _totals(self, halt: bool):
        ps = self.paper.summary(); cap = self.paper.s["capital"]
        rows = list(self.feed.values())
        week = sum(c["pnl"] for c in self.paper.s["closed"] if c["date"] >= (datetime.now(MT).date() - timedelta(days=7)).isoformat())
        pos = self.paper.s["positions"].values()
        risk_all = self.paper.open_risk() or 0.0
        risk_cm = sum(max(0.0, (p["entry"] - p["stop"]) * p["qty"]) for p in pos if p["market"] != "stock")
        meme_cap = sum(p["last"] * p["qty"] for p in pos if p["market"] == "meme")
        counts = {}
        for r in rows: counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
        self.totals = {
            "paper_only": True, "equity": ps["equity"], "pnl_today": ps["pnl_today"],
            "day_pnl_pct": round(100 * ps["pnl_today"] / cap, 3), "week_pnl_pct": round(100 * week / cap, 3),
            "open_risk": round(risk_all, 2), "gross_exposure": round(sum(p["last"] * p["qty"] for p in pos), 2),
            "crypto_meme_risk_share": round(risk_cm / risk_all, 3) if risk_all else 0.0,
            "meme_capital_pct": round(100 * meme_cap / cap, 3), "active": sum(1 for r in rows if r.get("status") != "PARKED"),
            "paused": len(policy.paused_bots(self.cfg)), "halt": halt, "status_counts": counts,
            "signals_now": sum(1 for r in rows if r.get("last_signal") == "LONG"),
            "buy_answers": sum(1 for r in rows if r.get("decision") == "BUY"),
            "stale_bots": sum(1 for r in rows if r.get("data_stale")), "errors": sum(1 for r in rows if r.get("error")),
            "shadow_open": len(self.shadow.s["positions"]), "shadow_closed": len(self.shadow.s["closed"]),
            "paper_open": len(self.paper.s["positions"]), "last_cycle": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "gate_passed_roles": [r for r, g in gate_results(self.dir).items() if g["verdict"] == "PAPER TEST"],
        }

    def _auto_pause(self):
        """Apply the Auto-pause rules sheet. Fleet-level HALTs always apply. Bot PAUSE files are created only for bots
        that can actually trade (role passed the gate); for the others the would-be action is shown as a flag."""
        bots = []
        for r in self.feed.values():
            d = dict(r)
            if d.get("data_age_s") is None: d["data_age_s"] = 0          # stock market closed: not stale
            if not d.get("heartbeat_age_s"): d["heartbeat_age_s"] = 0
            bots.append(d)
        actions = policy.auto_pause_actions(bots, self.totals, self.cfg)
        flags = {"half_size": {}, "pause_new_crypto": False, "pause_new_meme": False, "updated": self.last_cycle}
        apply, info = [], []
        for a in actions:
            if a.target == "FLEET":
                if a.action == "PAUSE_NEW_ENTRIES":
                    if "meme sleeve" in a.reason: flags["pause_new_meme"] = True
                    else: flags["pause_new_crypto"] = True
                    info.append(a)
                else: apply.append(a)
                continue
            row = self.feed.get(a.target, {})
            if a.action == "HALF_SIZE": flags["half_size"][a.target] = True; info.append(a); continue
            if row.get("status") == "ACTIVE-PAPER" or row.get("open_position"): apply.append(a)
            else:
                row["would_pause"] = f"{a.action}: {a.reason}"; info.append(a)
        policy.apply_actions(apply, self.cfg, self.audit)
        for a in apply: self._event({"bot": a.target, "event": f"AUTO {a.action}: {a.reason}"})
        self._write_flags(flags)
        self.totals["auto_actions"] = [a.__dict__ for a in apply]
        self.totals["flags"] = flags

    def _write(self):
        rows = sorted(self.feed.values(), key=lambda r: r["bot_id"])
        tmp = os.path.join(self.dir, "fleet.json.tmp")
        json.dump(rows, open(tmp, "w"), default=str); os.replace(tmp, os.path.join(self.dir, "fleet.json"))
        json.dump(self.totals, open(os.path.join(self.dir, "fleet_totals.json"), "w"), default=str)
        self.write_report()

    def write_report(self) -> dict:
        path = os.path.join(self.dir, "paper_log.csv")
        try: rows = jarvus_reporter.load(path)
        except Exception: rows = []
        s = jarvus_reporter.summarize(rows, self.paper.s["capital"]) if rows else {"empty": True, "paper_only": True}
        s["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        s["halt_flag"] = policy.halt_active(self.cfg)
        s["halted_bots"] = sorted(policy.paused_bots(self.cfg))
        ps = self.paper.summary()
        s.update(equity=ps["equity"], open_positions=ps["open_positions"], unrealized=ps["unrealized"],
                 gate_counts=self.totals.get("status_counts", {}), shadow_open=self.totals.get("shadow_open", 0),
                 shadow_closed=self.totals.get("shadow_closed", 0), signals_now=self.totals.get("signals_now", 0))
        tmp = os.path.join(self.dir, "report.json.tmp")
        json.dump(s, open(tmp, "w"), indent=1); os.replace(tmp, os.path.join(self.dir, "report.json"))
        return s
