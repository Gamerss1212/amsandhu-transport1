"""gateway.py - the ONLY way Jarvis touches the app (JARVIS PRIME section 22-24).

An explicit registry of typed tools. For every call, code checks: known tool, schema (no extra properties, bounds),
channel, session auth level, and risk tier - immediately before running it. The model's own claims about who it
is or what it may do are never trusted. Every call is audited. There is no shell, no file access, no arbitrary
HTTP, no broker credentials, and no tool that places, changes or cancels an order.

Tiers:  T0 public (general info, public prices)   T1 owner read (portfolio, bots, logs)
        RR risk-reducing (HALT, PAUSE) - allowed on any authenticated channel, voice included
        T2 owner operate (sandbox backtests, alert rules, memory, UI navigation) - desktop only
"""
from __future__ import annotations
import json, os, re, time
from datetime import datetime, timezone
import policy, tools as kit_tools, voice_router                       # kit/
from engine import library, volatility
from engine.data_feed import ET, MT, nyse_session, market_kind
from engine.backtest import VENUES

LEVEL_TIERS = {"anonymous": {"T0"}, "owner_read": {"T0", "T1", "RR"}, "owner_operate": {"T0", "T1", "RR", "T2"}}
SITES = {   # fixed allowlist - Jarvis can never open an arbitrary URL
    "tradingview": "https://www.tradingview.com/chart/?symbol={tv}", "yahoo_finance": "https://finance.yahoo.com/quote/{sym}",
    "coinbase": "https://www.coinbase.com/price/{base_l}", "kraken": "https://pro.kraken.com/app/trade/{base_l}-usd",
    "ndax": "https://ndax.io/", "fomc_calendar": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "nyse_hours": "https://www.nyse.com/markets/hours-calendars", "anthropic_console": "https://console.anthropic.com/",
}
PANELS = ["core", "chart", "strategies", "backtest", "fleet", "alerts", "memory", "activity", "risk", "help"]
SYMBOL = r"^[A-Z0-9.\-]{1,20}$"
BOT = r"^A\d{3}$"


def ok(data, source="jarvis", as_of=None, stale=False):
    return {"ok": True, "data": data, "source": source, "as_of": as_of,
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "stale": stale, "error": None}


def err(code, message):
    return {"ok": False, "data": None, "error": {"code": code, "message": message}}


def obj(props: dict, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


S = lambda **kw: {"type": "string", **kw}
I = lambda **kw: {"type": "integer", **kw}
B = {"type": "boolean"}


def validate(schema: dict, args) -> str | None:
    """Small strict JSON-schema check: types, required, enum, bounds, pattern, no extra keys."""
    if not isinstance(args, dict): return "arguments must be an object"
    props = schema.get("properties", {})
    extra = set(args) - set(props)
    if extra and schema.get("additionalProperties") is False: return f"unexpected argument(s): {sorted(extra)}"
    for k in schema.get("required", []):
        if k not in args: return f"missing argument: {k}"
    for k, v in args.items():
        p = props[k]; t = p.get("type")
        if t == "string":
            if not isinstance(v, str): return f"{k} must be a string"
            if len(v) > p.get("maxLength", 500): return f"{k} too long"
            if "pattern" in p and not re.match(p["pattern"], v): return f"{k} has a bad format"
        elif t == "integer":
            if not isinstance(v, int) or isinstance(v, bool): return f"{k} must be an integer"
            if v < p.get("minimum", -10 ** 12) or v > p.get("maximum", 10 ** 12): return f"{k} out of range"
        elif t == "number":
            if not isinstance(v, (int, float)) or isinstance(v, bool): return f"{k} must be a number"
        elif t == "boolean" and not isinstance(v, bool): return f"{k} must be true/false"
        elif t == "array" and not isinstance(v, list): return f"{k} must be a list"
        if "enum" in p and v not in p["enum"]: return f"{k} must be one of {p['enum']}"
    return None


class Gateway:
    def __init__(self, app):
        self.app = app
        self.tools: dict = {}
        self._register()

    def add(self, name, tier, desc, schema, fn, channels=("gui", "desktop_voice", "telephone")):
        self.tools[name] = {"name": name, "tier": tier, "description": desc, "input_schema": schema, "fn": fn, "channels": set(channels)}

    def for_session(self, level: str, channel: str) -> list:
        """The tool list the model is shown - already filtered by what this session may call."""
        allowed = LEVEL_TIERS.get(level, set())
        return [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
                for t in self.tools.values() if t["tier"] in allowed and channel in t["channels"]]

    def call(self, name: str, args: dict, session: dict) -> dict:
        t0 = time.time()
        t = self.tools.get(name)
        level, channel = session.get("auth_level", "anonymous"), session.get("channel", "gui")
        if t is None: res = err("UNKNOWN_TOOL", f"no tool named {name}")
        elif t["tier"] not in LEVEL_TIERS.get(level, set()): res = err("UNAUTHORIZED", "not allowed for this session")
        elif channel not in t["channels"]: res = err("CHANNEL_DENIED", f"{name} is not available on {channel}")
        else:
            problem = validate(t["input_schema"], args or {})
            if problem: res = err("BAD_ARGUMENTS", problem)
            else:
                try: res = t["fn"](args or {}, session)
                except Exception as e: res = err("UNAVAILABLE", f"{type(e).__name__}: {str(e)[:200]}")
        try:
            self.app.store.audit_tool(session.get("id"), channel, name, (t or {}).get("tier", "?"), args, res.get("ok"),
                                      None if res.get("ok") else json.dumps(res.get("error")), int(1000 * (time.time() - t0)))
        except Exception: pass
        return res

    # ======================= tools =======================
    def _register(self):
        a = self.app

        def get_time(args, s):
            now = datetime.now(timezone.utc); ses = nyse_session(now)
            mt = now.astimezone(MT)
            return ok({"mountain_time": mt.strftime("%A %Y-%m-%d %I:%M %p MT"), "eastern_time": now.astimezone(ET).strftime("%I:%M %p ET"),
                       "nyse_open": ses["open"], "next_nyse_open_mt": ses["next_open_mt"], "crypto_entries_allowed_today": mt.weekday() < 5,
                       "dead_hours_now": mt.hour >= 19}, "clock")
        self.add("get_time", "T0", "Current time in Mountain and Eastern time, whether the US stock market is open, and crypto entry rules for today.",
                 obj({}), get_time)

        def get_quote(args, s):
            sym = args["symbol"].upper()
            q = a.data.quote(sym, "1h")
            if not q["ok"]: return err("UNAVAILABLE", f"no price for {sym}: {q.get('error')}")
            q.pop("spark", None)
            return ok(q, q["source"], q["as_of"], q["stale"])
        self.add("get_quote", "T0", "Latest CLOSED 1-hour bar price and 24-bar change for a symbol, e.g. BTC-USD, ETH-USD, AAPL. "
                 "Crypto symbols end in -USD.", obj({"symbol": S(pattern=SYMBOL)}, ["symbol"]), get_quote)

        def market_overview(args, s):
            rows = []
            for sym in a.cfg["watchlist"]:
                q = a.data.quote(sym, "1h")
                if q["ok"]:
                    rows.append({"symbol": sym, "price": q["price"], "change_pct": q["change_pct"], "stale": q["stale"], "source": q["source"]})
                else: rows.append({"symbol": sym, "error": "unavailable"})
            return ok({"watchlist": rows, "data_mode": a.data.mode}, "data service")
        self.add("get_market_overview", "T0", "Prices and changes for the watchlist (crypto majors and key stocks).", obj({}), market_overview)

        def vol_gate(args, s):
            sym = args.get("symbol", "BTC-USD").upper()
            v = a.vol_summary(sym)
            if not v: return err("UNAVAILABLE", f"not enough bars for {sym}")
            return ok(v, v["source"], None, v.get("stale", False))
        self.add("get_volatility_gate", "T0", "Volatility gate for a symbol: LOUD / NORMAL / QUIET for the next 12 hours, with out-of-sample "
                 "AUC. It predicts the SIZE of moves, never direction.", obj({"symbol": S(pattern=SYMBOL)}), vol_gate)

        # ---------- T1 owner read ----------
        def system_status(args, s):
            f = a.fleet
            return ok({"paper_only": True, "data_mode": a.data.mode, "fleet_last_cycle": f.last_cycle, "fleet_cycles": f.cycle_n,
                       "halt_flag": policy.halt_active(f.cfg), "paused_bots": len(policy.paused_bots(f.cfg)),
                       "bots_active": f.totals.get("active"), "data_errors": f.totals.get("errors"), "stale_bots": f.totals.get("stale_bots"),
                       "ai_brain": a.brain_status(), "phone": a.calls.status(), "uptime_minutes": round((time.time() - a.started) / 60),
                       "audit_chain_ok": f.audit.verify()}, "system")
        self.add("get_system_status", "T1", "Health of the whole system: data feed, fleet cycle, HALT flag, paused bots, AI brain, phone setup, uptime.",
                 obj({}), system_status)

        def portfolio(args, s):
            p = a.fleet.paper.summary(); p["positions"] = a.fleet.paper.positions()
            p["currency"] = "USD (paper)"; p["note"] = "Paper money only. Equity = capital + realised + unrealised."
            return ok(p, "paper broker", a.fleet.last_cycle)
        self.add("get_portfolio", "T1", "The PAPER account: equity, realised and unrealised P&L, today's P&L, open positions with stops and targets.",
                 obj({}), portfolio)

        def daily_report(args, s):
            r = kit_tools.fleet_status(a.fleet.cfg, a.report_path)
            r["bullets"] = kit_tools.daily_report_text(a.report_path)
            return ok(r, "report.json") if r.get("ok") else err("UNAVAILABLE", r.get("error"))
        self.add("get_daily_report", "T1", "The daily paper-trading report (from report.json) as bullets, with its age.", obj({}), daily_report)

        def list_bots(args, s):
            rows = list(a.fleet.feed.values()) or [{"bot_id": b["bot_id"], "symbol": b["symbol"], "role": b["role"],
                                                    "status": b["status"], "verdict": b["verdict"]} for b in library.bots()]
            for k in ("status", "role", "group", "symbol"):
                if args.get(k): rows = [r for r in rows if args[k].lower() in str(r.get(k, "")).lower()]
            if args.get("signal_only"): rows = [r for r in rows if r.get("last_signal") == "LONG"]
            keep = ["bot_id", "symbol", "role", "status", "verdict", "strategy", "last_signal", "decision", "reason", "vol_state",
                    "open_position", "shadow_position", "error", "heartbeat_age_s"]
            return ok({"count": len(rows), "bots": [{k: r.get(k) for k in keep} for r in rows[: args.get("limit", 25)]]}, "fleet feed", a.fleet.last_cycle)
        self.add("list_agents", "T1", "List the trading bots (agents) with filters: status (WATCH, RETRAIN, NEEDS DATA, MEME SHADOW, PARKED, "
                 "ACTIVE-PAPER), role, group (US stocks, Crypto, Meme coins), symbol, signal_only.",
                 obj({"status": S(maxLength=30), "role": S(maxLength=60), "group": S(maxLength=30), "symbol": S(maxLength=20),
                      "signal_only": B, "limit": I(minimum=1, maximum=99)}), list_bots)

        def get_bot(args, s):
            bid = args["bot_id"].upper()
            sheet = library.bot(bid)
            if not sheet: return err("NOT_FOUND", f"no bot {bid}")
            live = a.fleet.feed.get(bid, {})
            return ok({"sheet": sheet, "live": live, "paused": bid in policy.paused_bots(a.fleet.cfg)}, "fleet feed + bots sheet", live.get("heartbeat_time"))
        self.add("get_agent", "T1", "Everything about one bot, e.g. A073: role, symbol, strategy, gate verdict, last signal, BUY/WAIT answer and why, heartbeat.",
                 obj({"bot_id": S(pattern=BOT)}, ["bot_id"]), get_bot)

        def fleet_summary(args, s):
            return ok({**a.fleet.totals, "roles": library.roles()}, "fleet", a.fleet.last_cycle)
        self.add("get_fleet_summary", "T1", "Fleet totals: bots by status, signals now, BUY answers, shadow trades, open paper risk, roles and their candidate strategies.",
                 obj({}), fleet_summary)

        def risk_status(args, s):
            c = a.fleet.cfg; t = a.fleet.totals
            return ok({"halt_flag": policy.halt_active(c), "paused_bots": sorted(policy.paused_bots(c)),
                       "limits": {"max_open_stock": c.max_open_stock, "max_open_crypto": c.max_open_crypto, "max_trades_per_day": c.max_trades_per_day,
                                  "fleet_day_loss_pct": c.fleet_day_loss_pct, "fleet_week_loss_pct": c.fleet_week_loss_pct,
                                  "cost_rule": "<=0.20R ok, 0.20-0.33R half size, >0.33R WAIT", "min_stop_atr": c.min_stop_atr},
                       "used": {"trades_today": a.fleet.paper.trades_today(), "day_pnl_pct": t.get("day_pnl_pct"),
                                "week_pnl_pct": t.get("week_pnl_pct"), "open_risk_usd": t.get("open_risk"),
                                "crypto_meme_risk_share": t.get("crypto_meme_risk_share"), "meme_capital_pct": t.get("meme_capital_pct")},
                       "flags": t.get("flags"), "auto_actions_last_cycle": t.get("auto_actions")}, "risk engine (code)")
        self.add("get_risk_status", "T1", "Risk engine: hard limits, how much is used, HALT flag, paused bots, auto-pause flags.", obj({}), risk_status)

        def decision(args, s):
            sym = args["symbol"].upper()
            rows = [r for r in a.fleet.feed.values() if r.get("symbol", "").upper() == sym]
            if not rows: return ok({"bullets": "- WAIT\n- No bot for this symbol"}, "policy (code)")
            r = rows[0]
            return ok({"bot": r["bot_id"], "bullets": r.get("bullets") or "- WAIT\n- Need fresh prices", "decision": r.get("decision"),
                       "reason": r.get("reason"), "strategy": r.get("strategy"), "verdict": r.get("verdict"), "vol_state": r.get("vol_state"),
                       "cost_r": r.get("cost_r")}, "policy.decide (code)", r.get("heartbeat_time"))
        self.add("get_trade_decision", "T1", "The code's BUY or WAIT answer for a symbol right now (from policy.decide via the fleet), with the reason. "
                 "Use this for 'should I buy X' questions. Answer in the master-prompt bullet format.",
                 obj({"symbol": S(pattern=SYMBOL)}, ["symbol"]), decision)

        def search_strategies(args, s):
            lib = library.load(); q = (args.get("query") or "").lower()
            rows = lib["keep"] + lib["new"]
            if q: rows = [r for r in rows if q in json.dumps(r, default=str).lower()]
            if args.get("market"): rows = [r for r in rows if args["market"] in str(r.get("markets", "crypto, stock"))]
            if args.get("tier"): rows = [r for r in rows if args["tier"] in str(r.get("tier", "new"))]
            keep = ["id", "tier", "starter", "job", "jarvus_grade", "bars", "markets", "settings", "caveat", "status", "rules"]
            return ok({"count": len(rows), "strategies": [{k: r.get(k) for k in keep if r.get(k) is not None} for r in rows[: args.get("limit", 12)]],
                       "totals": {"keep": len(lib["keep"]), "new": len(lib["new"]), "do_not_build": len(lib["do_not_build"])}}, "strategy library")
        self.add("search_strategies", "T1", "Search the strategy library (100 kept variants, 11 new strategies). Filter by text, market (stock/crypto), tier (1 CORE / 2 TEST).",
                 obj({"query": S(maxLength=80), "market": S(enum=["stock", "crypto"]), "tier": S(enum=["1 CORE", "2 TEST"]),
                      "limit": I(minimum=1, maximum=40)}), search_strategies)

        def recent_activity(args, s):
            n = args.get("limit", 15)
            return ok({"fleet_events": a.fleet.events[:n], "tool_calls": a.store.tool_audit(n), "notifications": a.store.notifications(0, n),
                       "tasks": a.tasks.list(n),
                       "metrics": {"fleet_cycles": a.fleet.cycle_n, "signals_now": a.fleet.totals.get("signals_now"),
                                   "shadow_trades_closed": a.fleet.totals.get("shadow_closed"), "paper_trades_open": a.fleet.totals.get("paper_open"),
                                   "backtests_run": sum(1 for t in a.tasks.list(200) if t["kind"] in ("backtest", "role_test"))}},
                      "activity log")
        self.add("get_recent_activity", "T1", "What the system and the AI actually did recently: fleet events, signals, tool calls, alerts, background tasks, with real counts.",
                 obj({"limit": I(minimum=1, maximum=50)}), recent_activity)

        def shadow_log(args, s):
            sh = a.fleet.shadow
            return ok({"summary": sh.summary(), "open": sh.positions(), "closed": sh.s["closed"][-args.get("limit", 20):]},
                      "shadow book (no money)")
        self.add("get_shadow_log", "T1", "Shadow trades: signals from bots that have NOT passed the gate, simulated with no money.",
                 obj({"limit": I(minimum=1, maximum=100)}), shadow_log)

        def task_status(args, s):
            if args.get("task_id"):
                t = a.tasks.get(args["task_id"])
                if not t: return err("NOT_FOUND", "no such task")
                res = t.get("result") or {}
                if isinstance(res, dict):                 # keep the model's view small
                    res = {k: v for k, v in res.items() if k not in ("trades", "equity")}
                    if "trades" in (t.get("result") or {}): res["trade_count"] = len(t["result"]["trades"])
                t["result"] = res
                return ok(t, "task registry")
            return ok({"tasks": a.tasks.list(args.get("limit", 10))}, "task registry")
        self.add("get_tasks", "T1", "Background jobs (backtests, role tests): status, progress, and results. Give task_id for one task.",
                 obj({"task_id": S(maxLength=20), "limit": I(minimum=1, maximum=30)}), task_status)

        def list_rules(args, s): return ok({"rules": a.alerts.list()}, "alert rules")
        self.add("list_alert_rules", "T1", "List alert and reminder rules.", obj({}), list_rules)

        def recall(args, s): return ok({"memories": a.store.memories(args.get("query"), 20)}, "long-term memory")
        self.add("recall_memories", "T1", "Look up what Abhi asked Jarvis to remember (preferences, projects, facts). Never holds live prices or balances.",
                 obj({"query": S(maxLength=120)}), recall)

        # ---------- RR risk-reducing ----------
        def halt(args, s):
            r = kit_tools.halt_all(a.fleet.cfg, f"Jarvis tool ({s.get('channel')}): {args.get('reason', 'requested')}"[:200])
            a.store.notify("HALT", "The HALT flag is ON. Nothing trades until Abhi deletes the HALT file at the keyboard.", "critical")
            return ok(r, "kill switch")
        self.add("halt_fleet", "RR", "EMERGENCY STOP for the whole fleet: creates the HALT flag (closes paper positions next cycle). "
                 "Only Abhi can clear it, by hand. Use when asked to halt / stop everything.",
                 obj({"reason": S(maxLength=200)}), halt)

        def pause(args, s):
            bid = args["bot_id"].upper()
            if not library.bot(bid): return err("NOT_FOUND", f"no bot {bid}")
            r = kit_tools.pause_one(a.fleet.cfg, bid)
            return ok({"paused": bid, **r, "note": "Resume only at the keyboard."}, "risk engine")
        self.add("pause_agent", "RR", "Pause one bot, e.g. A073. Cannot be undone by Jarvis.", obj({"bot_id": S(pattern=BOT)}, ["bot_id"]), pause)

        # ---------- T2 owner operate (desktop only) ----------
        desk = ("gui", "desktop_voice")

        def run_backtest(args, s):
            sid = args["strategy_id"]
            if not library.strategy(sid): return err("NOT_FOUND", f"unknown strategy {sid}")
            tid = a.start_backtest(sid, args["symbol"].upper(), args.get("bar", "1h"), args.get("venue"), args.get("vol_gate", True), s.get("id"))
            return ok({"task_id": tid, "status": "queued", "note": "Sandbox backtest on historical bars; costs on; next-bar fills."}, "task registry")
        self.add("run_backtest", "T2", "Start a SANDBOX backtest (background task) of one strategy on one symbol, with the full quality-gate report. "
                 "Returns a task id; results arrive later.",
                 obj({"strategy_id": S(maxLength=60), "symbol": S(pattern=SYMBOL), "bar": S(enum=["5m", "15m", "1h"]),
                      "venue": S(enum=list(VENUES)), "vol_gate": B}, ["strategy_id", "symbol"]), run_backtest, desk)

        def run_role(args, s):
            if args["role"] not in [r["role"] for r in library.roles()]: return err("NOT_FOUND", "unknown role")
            return ok({"task_id": a.start_role_test(args["role"], s.get("id")), "status": "queued"}, "task registry")
        self.add("run_role_test", "T2", "Start the pooled ROLE test (build step 5): pick one candidate on train data, test once on unseen data, "
                 "run the quality gate. Only a PAPER TEST verdict lets that role's bots paper-trade.",
                 obj({"role": S(maxLength=60)}, ["role"]), run_role, desk)

        def create_rule(args, s):
            from .alerts import describe
            rule = {"condition": args["condition"], "channels": args.get("channels", ["desktop"]), "label": args.get("label", ""),
                    "severity": args.get("severity", "info")}
            try: r = a.alerts.add(rule)
            except ValueError as e: return err("BAD_ARGUMENTS", str(e))
            return ok({"rule_id": r["id"], "readback": describe(r)}, "alert engine")
        cond = {"type": "object", "description": "One of: {kind:'metric_threshold', metric:'price:BTC-USD'|'paper_pnl_today'|'paper_equity'|"
                "'open_positions'|'paused_bots', operator:'>'|'>='|'<'|'<=', threshold:'decimal string', initial_true_policy:'trigger'|'wait_for_crossing'} | "
                "{kind:'vol_state', symbol, state:'LOUD'|'NORMAL'|'QUIET'} | {kind:'any_signal'} | {kind:'task_completed', task_id} | "
                "{kind:'scheduled', local_time:'HH:MM', local_date:'YYYY-MM-DD' (required for once), recurrence:'once'|'daily', message}. "
                "Keep the owner's operator exactly: 'above' is >, 'reaches' is >=."}
        self.add("create_alert_rule", "T2", "Create a typed alert or reminder. Read the returned readback to the owner. Phone channel only calls the owner's own number with a generic message.",
                 obj({"condition": cond, "channels": {"type": "array", "items": {"type": "string", "enum": ["desktop", "phone"]}},
                      "label": S(maxLength=120), "severity": S(enum=["info", "warning", "critical"])}, ["condition"]), create_rule, desk)

        def disable_rule(args, s): a.alerts.disable(args["rule_id"]); return ok({"disabled": args["rule_id"]}, "alert engine")
        self.add("disable_alert_rule", "T2", "Turn off an alert rule by id.", obj({"rule_id": S(maxLength=20)}, ["rule_id"]), disable_rule, desk)

        def remember(args, s):
            mid = a.store.remember(args["text"], args.get("kind", "preference"), source=f"owner via {s.get('channel')}")
            return ok({"memory_id": mid, "saved": args["text"]}, "long-term memory")
        self.add("remember", "T2", "Save something Abhi explicitly asked you to remember (a preference, project note, or fact). Never secrets, PINs, keys or balances.",
                 obj({"text": S(maxLength=500), "kind": S(enum=["preference", "project", "fact", "rule"])}, ["text"]), remember, desk)

        def forget(args, s): a.store.forget(args["memory_id"]); return ok({"forgotten": args["memory_id"]}, "long-term memory")
        self.add("forget_memory", "T2", "Delete one remembered item by id.", obj({"memory_id": S(maxLength=20)}, ["memory_id"]), forget, desk)

        def research(args, s):
            if not a.brain.ready()[0]: return err("SETUP_REQUIRED", "research needs the AI brain (ANTHROPIC_API_KEY)")
            return ok({"task_id": a.start_research(args["question"], s.get("id")), "status": "queued",
                       "note": "Runs in the background; Abhi gets a notification when the report is ready."}, "task registry")
        self.add("start_research", "T2", "Start a background research job on the public web (many sources, cited). Use for questions that need "
                 "more than a quick search. Returns a task id; read the report later with get_tasks.",
                 obj({"question": S(maxLength=600)}, ["question"]), research, desk)

        def build_site(args, s):
            slug = re.sub(r"[^a-z0-9]+", "-", args["title"].lower()).strip("-")[:40] or "site"
            os.makedirs(a.sites_dir, exist_ok=True)
            with open(os.path.join(a.sites_dir, slug + ".html"), "w", encoding="utf-8") as f: f.write(args["html"])
            url = f"/sites/{slug}.html"
            a.ui_event({"type": "open_url", "url": url, "label": f"Website: {args['title']}"})
            return ok({"saved": url, "note": "Saved locally and opened in a sandboxed tab."}, "website builder")
        self.add("build_website", "T2", "Build a website for Abhi's web-design business (ElevatedSteps) or anyone he asks: pass ONE complete "
                 "self-contained HTML file (inline CSS/JS, modern, mobile-friendly, realistic copy). It is saved and opened for him.",
                 obj({"title": S(maxLength=80), "html": S(maxLength=400_000)}, ["title", "html"]), build_site, desk)

        def open_site(args, s):
            sym = (args.get("symbol") or "BTC-USD").upper()
            base = sym[:-4] if sym.endswith("-USD") else sym
            tv = f"COINBASE:{base}USD" if sym.endswith("-USD") else sym
            url = SITES[args["site"]].format(sym=sym, base=base, base_l=base.lower(), tv=tv)
            a.ui_event({"type": "open_url", "url": url, "label": args["site"].replace("_", " ").title() + (f" · {sym}" if "{" in SITES[args["site"]] else "")})
            return ok({"opened": url}, "allowlisted link")
        self.add("open_website", "T2", "Open a website on Abhi's screen from a FIXED list only: " + ", ".join(SITES) + ". Optional symbol for chart/quote pages.",
                 obj({"site": S(enum=list(SITES)), "symbol": S(pattern=SYMBOL)}, ["site"]), open_site, desk)

        def open_panel(args, s):
            a.ui_event({"type": "open_panel", "panel": args["panel"], "symbol": args.get("symbol")})
            return ok({"opened": args["panel"]}, "ui")
        self.add("open_panel", "T2", "Show a page on Abhi's screen: " + ", ".join(PANELS) + ". Optionally a symbol for the chart.",
                 obj({"panel": S(enum=PANELS), "symbol": S(pattern=SYMBOL)}, ["panel"]), open_panel, desk)
