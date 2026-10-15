"""app.py - wires everything together: data service, fleet, paper/shadow books, memory, tasks, alerts, phone,
tool gateway and the Jarvis brain. Background loops: fleet cycle, alert evaluation, daily report, retention."""
from __future__ import annotations
import json, os, threading, time, uuid
from collections import deque
from datetime import datetime, timezone
from engine import ROOT, library, volatility, validation
from engine.data_feed import DataService, MT, market_kind
from engine.fleet import Fleet, MAJORS
import policy                                                         # kit/
from .memory import Store
from .tasks import TaskManager
from .alerts import AlertEngine
from .phone import OutboundCallManager, PinAuth, InboundCalls
from .gateway import Gateway
from .brain import AnthropicBrain
from .core import JarvisCore

DEFAULTS = {"owner_name": "Abhi", "timezone": "America/Edmonton", "paper_capital_usd": 10000, "data_mode": "live",
            "crypto_venue": "ndax", "watchlist": ["BTC-USD", "ETH-USD", "SOL-USD", "AAPL", "NVDA", "JPM"],
            "fleet_cycle_seconds": 120, "alerts_every_seconds": 30, "daily_report_time_mt": "14:15",
            "models": {"strong": "claude-opus-5-5", "fast": "claude-sonnet-5-5"}, "effort": {"strong": "medium", "fast": "low"},
            "internet_research": True, "quiet_hours_mt": ["22:00", "07:00"], "retention_days": 30,
            "phone": {"max_calls_per_hour": 2, "max_calls_per_day": 6, "calls_off": False}, "port": 8765}


def load_config(path: str | None = None) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try: cfg.update(json.load(open(path or os.path.join(ROOT, "config", "jarvis_config.json"))))
    except Exception: pass
    if os.environ.get("JARVIS_DATA_MODE") in ("live", "demo"): cfg["data_mode"] = os.environ["JARVIS_DATA_MODE"]
    if os.environ.get("JARVIS_MODEL_STRONG"): cfg["models"]["strong"] = os.environ["JARVIS_MODEL_STRONG"]
    if os.environ.get("JARVIS_MODEL_FAST"): cfg["models"]["fast"] = os.environ["JARVIS_MODEL_FAST"]
    if os.environ.get("JARVIS_PORT"): cfg["port"] = int(os.environ["JARVIS_PORT"])
    return cfg


class App:
    def __init__(self, state_dir: str | None = None, cfg: dict | None = None, brain=None, start_threads: bool = True):
        self.cfg = cfg or load_config()
        self.state_dir = state_dir or os.environ.get("JARVUS_DIR") or os.path.join(ROOT, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.started = time.time()
        self.data = DataService(self.state_dir, self.cfg["data_mode"])
        self.fleet = Fleet(self.data, self.state_dir, float(self.cfg["paper_capital_usd"]), self.cfg["crypto_venue"])
        self.store = Store(os.path.join(self.state_dir, "jarvis.db"))
        self.store.purge(int(self.cfg["retention_days"]))
        self.calls = OutboundCallManager(self.store, self.cfg["phone"])
        self.pin = PinAuth(self.state_dir)
        self.alerts = AlertEngine(self.store, self.metric, self.calls, tuple(self.cfg["quiet_hours_mt"]))
        self.tasks = TaskManager(self.store, 2, on_done=self._task_done)
        self.gateway = Gateway(self)
        # Without an API key the AnthropicBrain reports SETUP_REQUIRED and the core runs the labelled offline mode.
        self.brain = brain or AnthropicBrain(self.cfg["models"], bool(self.cfg["internet_research"]), effort=self.cfg.get("effort"))
        self.core = JarvisCore(self, self.brain)
        self.inbound = InboundCalls(self.pin, self.core.handle)
        self.ui_events: deque = deque(maxlen=200)
        self._ui_id = 0
        self.vol_cache: dict = {}
        self.quotes: dict = {s: {"ok": False, "symbol": s, "error": "loading"} for s in self.cfg["watchlist"]}
        self._seen_event_t = ""
        self._stop = threading.Event()
        if start_threads: self.start()

    @property
    def report_path(self): return os.path.join(self.state_dir, "report.json")

    def brain_status(self) -> dict:
        ready, why = self.brain.ready()
        return {"provider": getattr(self.brain, "name", "?"), "ready": ready, "reason": why,
                "models": getattr(self.brain, "models", {}), "web_research": bool(getattr(self.brain, "web", False)),
                "last_error": getattr(self.brain, "last_error", None)}

    def ui_event(self, e: dict):
        self._ui_id += 1
        self.ui_events.append({**e, "id": self._ui_id, "t": time.time()})     # event id always wins

    # ---------- deterministic metrics for alert rules ----------
    def metric(self, name: str):
        now = time.time()
        if name.startswith("price:"):
            g = self.data.get(name[6:].upper(), "1h", 60, refresh=False)
            if not g["ok"]: g = self.data.get(name[6:].upper(), "1h", 60)
            return (g["bars"].c[-1], g["bars"].last_close_time()) if g["ok"] else None
        if name.startswith("vol:"):
            v = self.vol_summary(name[4:].upper()); return (v["state"], now) if v else None
        s = self.fleet.paper.summary()
        if name == "paper_pnl_today": return (s["pnl_today"], now)
        if name == "paper_equity": return (s["equity"], now)
        if name == "open_positions": return (s["open_positions"], now)
        if name == "paused_bots": return (len(policy.paused_bots(self.fleet.cfg)), now)
        return None

    def vol_summary(self, sym: str, refresh: bool = False):
        c = self.vol_cache.get(sym)
        if c and time.time() - c[0] < 600 and not refresh: return c[1]
        g = self.data.get(sym, "1h", 3000)
        if not g["ok"] or len(g["bars"]) < 100: return None
        v = volatility.summary(g["bars"]); v["stale"] = g["stale"]
        self.vol_cache[sym] = (time.time(), v)
        return v

    # ---------- background jobs ----------
    def start_backtest(self, sid, symbol, bar="1h", venue=None, vol_gate=True, session_id=None) -> str:
        def job(progress, cancel):
            progress(0.05, f"loading {symbol} {bar} bars")
            n = 6000 if bar == "1h" else 8000
            g = self.data.get(symbol, bar, n)
            if not g["ok"]: raise RuntimeError(f"no data for {symbol}: {g['error']}")
            bm = None
            if market_kind(symbol) != "stock" and symbol != "BTC-USD":
                b2 = self.data.get("BTC-USD", bar, n); bm = b2["bars"] if b2["ok"] else None
            progress(0.25, "backtesting, benchmark, placebo, PBO")
            meme = any(b["symbol"] == symbol and library.is_meme(b) for b in library.bots())
            rep = validation.full_report(g["bars"], sid, self.state_dir, settings=library.settings_for(sid), benchmark=bm,
                                         venue=venue, vol_gate=vol_gate, market="meme" if meme else None)
            rep["trades"] = rep["trades"][-400:]
            rep["data_note"] = g.get("note") or ""
            return rep
        return self.tasks.submit("backtest", f"Backtest {sid} on {symbol} {bar}", job, session_id)

    def start_role_test(self, role, session_id=None) -> str:
        def job(progress, cancel):
            return validation.role_test(role, self.data, self.state_dir, progress=progress)
        return self.tasks.submit("role_test", f"Role test: {role}", job, session_id)

    def _task_done(self, tid, kind, title, status):
        self.alerts.emit("task_completed", tid)
        self.store.notify(f"Task {status}", title, "info" if status == "done" else "warning")
        self.ui_event({"type": "task", "task_id": tid, "status": status})

    def cycle_once(self, budget: int = 25):
        r = self.fleet.cycle(refresh_budget=budget)
        for e in reversed(self.fleet.events):
            if e["t"] <= self._seen_event_t: continue
            if "signal" in e["event"].lower() or "order" in e["event"].lower(): self.alerts.emit("signal", f"{e['bot']}: {e['event']}")
            self._seen_event_t = e["t"]
        return r

    def start(self):
        def fleet_loop():
            first = True
            while not self._stop.is_set():
                try: self.cycle_once(80 if first else 25)
                except Exception as e: self.store.notify("Fleet cycle error", f"{type(e).__name__}: {e}", "warning")
                first = False
                self._stop.wait(int(self.cfg["fleet_cycle_seconds"]))

        def alert_loop():
            last_report = None
            while not self._stop.is_set():
                try: self.alerts.evaluate()
                except Exception: pass
                now = datetime.now(MT); hh, mm = map(int, self.cfg["daily_report_time_mt"].split(":"))
                if now.weekday() < 5 and (now.hour, now.minute) >= (hh, mm) and last_report != now.date():
                    last_report = now.date()
                    try:
                        s = self.fleet.write_report()
                        self.store.notify("Daily report ready", "Paper-trading report written. Ask: how did today go?", "info")
                    except Exception: pass
                self._stop.wait(int(self.cfg["alerts_every_seconds"]))

        def quote_loop():
            while not self._stop.is_set():
                for s in self.cfg["watchlist"]:
                    try:
                        q = self.data.quote(s, "1h")
                        if q.get("ok"):
                            lp = self.data.live_price(s)
                            q["live"] = lp
                            q["live_change_pct"] = None if lp is None or len(q["spark"]) < 25 else round(100 * (lp / q["spark"][-25] - 1), 2)
                        self.quotes[s] = q
                    except Exception as e:
                        self.quotes[s] = {"ok": False, "symbol": s, "error": type(e).__name__}
                self._stop.wait(20)

        def vol_loop():
            while not self._stop.is_set():
                for s in ("BTC-USD", "ETH-USD", "SOL-USD"):
                    try: self.vol_summary(s, refresh=True)
                    except Exception: pass
                self._stop.wait(900)

        for fn in (quote_loop, fleet_loop, alert_loop, vol_loop):
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()

    def stop(self): self._stop.set()
