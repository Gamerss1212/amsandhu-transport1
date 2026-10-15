"""Jarvis Trading Portal - run this file.

    pip install -r requirements.txt
    set ANTHROPIC_API_KEY=sk-ant-...      (Mac/Linux: export ANTHROPIC_API_KEY=...)   optional: without it Jarvis runs offline mode
    python server.py                       -> opens http://127.0.0.1:8765 (use Chrome or Edge for voice)
    python server.py --demo                -> simulated prices (labelled DEMO everywhere), no internet needed for data

Security: listens on 127.0.0.1 only. Every API call needs the per-run session token that is embedded in the page,
and the Host/Origin must be this machine. PAPER TRADING ONLY - there is no endpoint that places an order and no
endpoint that clears HALT or resumes a bot (delete the files in state/ by hand to do that).
"""
from __future__ import annotations
import json, os, secrets, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if "--demo" in sys.argv: os.environ["JARVIS_DATA_MODE"] = "demo"

from engine import library, indicators as ind                              # noqa: E402
from engine.data_feed import BAR_SECONDS, MT, ET, nyse_session, market_kind  # noqa: E402
from engine.strategies import Ctx, entry_signals, spec as make_spec         # noqa: E402
from engine.backtest import VENUES                                         # noqa: E402
import policy                                                              # noqa: E402
from jarvis.app import App, load_config                                     # noqa: E402

WEB = os.path.join(HERE, "web")
TOKEN = secrets.token_urlsafe(24)
APP: App | None = None
CFG = load_config()
PORT = int(CFG["port"])
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


def state_payload(since_ui: int = 0, since_note: int = 0) -> dict:
    a = APP; f = a.fleet; now = datetime.now(timezone.utc)
    quotes = [dict(q, vol=(a.vol_cache[q["symbol"]][1].get("state") if q["symbol"] in a.vol_cache else None))
              for q in a.quotes.values()]
    vols = {s: a.vol_cache[s][1] for s in ("BTC-USD", "ETH-USD", "SOL-USD") if s in a.vol_cache}
    feed = list(f.feed.values())
    compact = [{k: r.get(k) for k in ("bot_id", "symbol", "role", "group", "status", "verdict", "strategy", "last_signal",
                                      "decision", "reason", "vol_state", "open_position", "shadow_position", "error",
                                      "heartbeat_age_s", "last_price", "would_pause", "cost_r", "bar")} for r in feed]
    paused = sorted(policy.paused_bots(f.cfg))
    tasks = a.tasks.list(8)
    return {
        "now": now.isoformat(timespec="seconds"), "mt": now.astimezone(MT).strftime("%a %b %d · %I:%M:%S %p MT"),
        "et": now.astimezone(ET).strftime("%I:%M %p ET"), "nyse": nyse_session(now), "weekend": now.astimezone(MT).weekday() >= 5,
        "data_mode": a.data.mode, "brain": a.brain_status(), "phone": a.calls.status(), "paper_only": True,
        "uptime_s": int(time.time() - a.started), "halt": policy.halt_active(f.cfg), "paused": paused,
        "portfolio": f.paper.summary(), "positions": f.paper.positions(), "equity_curve": f.paper.s["equity"][-300:],
        "shadow": f.shadow.summary(), "shadow_positions": f.shadow.positions()[:20], "shadow_closed": f.shadow.s["closed"][-30:],
        "paper_closed": f.paper.s["closed"][-30:],
        "totals": f.totals, "fleet": compact, "fleet_progress": f.progress, "fleet_running": f.running,
        "last_cycle": f.last_cycle, "cycles": f.cycle_n, "events": f.events[:40], "quotes": quotes, "vol": vols,
        "limits": {"max_open_stock": f.cfg.max_open_stock, "max_open_crypto": f.cfg.max_open_crypto,
                   "max_trades_per_day": f.cfg.max_trades_per_day, "day_loss_pct": f.cfg.fleet_day_loss_pct,
                   "week_loss_pct": f.cfg.fleet_week_loss_pct, "cost_ok_r": f.cfg.cost_ok_r, "cost_max_r": f.cfg.cost_max_r},
        "trades_today": f.paper.trades_today(), "tasks": tasks,
        "notifications": a.store.notifications(since_note, 20), "ui_events": [e for e in a.ui_events if e["id"] > since_ui],
        "gate_results": __import__("engine.validation", fromlist=["x"]).gate_results(a.state_dir),
        "tries": __import__("engine.validation", fromlist=["x"]).tries(a.state_dir),
        "audit_ok": True,
    }


def bars_payload(q: dict) -> dict:
    sym = (q.get("symbol") or ["BTC-USD"])[0].upper()
    bar = (q.get("bar") or ["1h"])[0]
    if bar not in BAR_SECONDS: return {"ok": False, "error": "bad bar"}
    n = min(int((q.get("n") or ["240"])[0]), 1500)
    g = APP.data.get(sym, bar, max(n, 400))
    if not g["ok"]: return {"ok": False, "error": g["error"]}
    b = g["bars"]
    ctx = Ctx(b)
    e20, e50 = ctx.ema(20), ctx.ema(50)
    vw, _ = ctx.vwap()
    sid = (q.get("strategy") or [""])[0]
    marks = []
    if sid and library.strategy(sid):
        try:
            sig = entry_signals(make_spec(sid, library.settings_for(sid)), ctx)
            marks = [b.t[i] for i in range(len(b)) if sig[i]]
        except Exception: marks = []
    k = slice(-n, None)
    return {"ok": True, "symbol": sym, "bar": bar, "source": b.source, "stale": g["stale"], "note": g.get("note"),
            "t": b.t[k], "o": b.o[k], "h": b.h[k], "l": b.l[k], "c": b.c[k], "v": b.v[k], "ema20": e20[k], "ema50": e50[k],
            "vwap": vw[k], "signals": [t for t in marks if t >= b.t[k][0]], "atr": ctx.atr(14)[-1]}


class H(BaseHTTPRequestHandler):
    server_version = "Jarvis"

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(data)

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _auth(self):
        if not self._host_ok(): return False
        o = self.headers.get("Origin")
        if o and o not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"): return False
        return secrets.compare_digest(self.headers.get("X-Jarvis-Token", ""), TOKEN)

    def _body(self):
        n = min(int(self.headers.get("Content-Length", 0) or 0), 200_000)
        raw = self.rfile.read(n) if n else b""
        ctype = self.headers.get("Content-Type", "")
        if "application/x-www-form-urlencoded" in ctype:
            return {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        try: return json.loads(raw or b"{}")
        except Exception: return None

    # ---------------- GET ----------------
    def do_GET(self):
        u = urlparse(self.path); p = u.path; q = parse_qs(u.query)
        if p in ("/", "/index.html"):
            if not self._host_ok(): return self._send(403, {"error": "wrong host"})
            html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read().replace("{{JARVIS_TOKEN}}", TOKEN)
            return self._send(200, html.encode(), TYPES[".html"], {"Content-Security-Policy":
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'"})
        if p.startswith("/static/"):
            name = os.path.normpath(p[len("/static/"):]).replace("\\", "/")
            f = os.path.join(WEB, name)
            if ".." in name or not os.path.isfile(f): return self._send(404, {"error": "not found"})
            return self._send(200, open(f, "rb").read(), TYPES.get(os.path.splitext(f)[1], "application/octet-stream"))
        if p == "/api/health": return self._send(200, {"ok": True, "paper_only": True})
        if not p.startswith("/api/"): return self._send(404, {"error": "not found"})
        if not self._auth(): return self._send(403, {"error": "forbidden"})
        a = APP
        try:
            if p == "/api/state":
                return self._send(200, state_payload(int((q.get("ui") or ["0"])[0]), int((q.get("note") or ["0"])[0])))
            if p == "/api/bars": return self._send(200, bars_payload(q))
            if p == "/api/fleet": return self._send(200, {"rows": list(a.fleet.feed.values()), "totals": a.fleet.totals})
            if p == "/api/strategies":
                lib = library.load()
                return self._send(200, {k: lib[k] for k in ("keep", "new", "do_not_build", "measured", "day_trade_rules", "cost_in_r", "roles",
                                                           "auto_pause_rules", "next_steps")} |
                                  {"venues": VENUES, "bots": [{k: b.get(k) for k in ("bot_id", "symbol", "role", "group", "status", "verdict", "bar",
                                                                                    "candidate_1")} for b in lib["bots"]]})
            if p == "/api/task":
                t = a.tasks.get((q.get("id") or [""])[0])
                return self._send(200 if t else 404, t or {"error": "no task"})
            if p == "/api/tasks": return self._send(200, {"tasks": a.tasks.list(30)})
            if p == "/api/alerts": return self._send(200, {"rules": a.alerts.list(), "phone": a.calls.status()})
            if p == "/api/memory": return self._send(200, {"memories": a.store.memories(None, 200)})
            if p == "/api/memory/export":
                return self._send(200, json.dumps({"memories": a.store.memories(None, 10000)}, indent=1).encode(), "application/json",
                                  {"Content-Disposition": "attachment; filename=jarvis_memory.json"})
            if p == "/api/activity":
                return self._send(200, {"tool_audit": a.store.tool_audit(80), "events": a.fleet.events[:120],
                                        "messages": a.store.recent_messages(60), "audit_chain_ok": a.fleet.audit.verify()})
            if p == "/api/vol":
                s = (q.get("symbol") or ["BTC-USD"])[0].upper()
                return self._send(200, a.vol_summary(s) or {"error": "no data"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    # ---------------- POST ----------------
    def do_POST(self):
        p = urlparse(self.path).path
        if p.startswith("/phone/"): return self._phone(p)
        if not self._auth(): return self._send(403, {"error": "forbidden"})
        body = self._body()
        if body is None: return self._send(400, {"error": "bad json"})
        a = APP
        try:
            if p == "/api/ask":
                ch = body.get("channel") if body.get("channel") in ("gui", "desktop_voice") else "gui"
                return self._send(200, a.core.handle(str(body.get("text", "")), channel=ch, session_id=body.get("session_id"),
                                                     auth_level="owner_operate"))
            sess = {"id": "ui", "channel": "gui", "auth_level": "owner_operate"}
            if p == "/api/backtest":
                return self._send(200, a.gateway.call("run_backtest", {k: body[k] for k in ("strategy_id", "symbol", "bar", "venue", "vol_gate") if k in body}, sess))
            if p == "/api/role_test": return self._send(200, a.gateway.call("run_role_test", {"role": str(body.get("role", ""))}, sess))
            if p == "/api/alerts":
                return self._send(200, a.gateway.call("create_alert_rule", {k: body[k] for k in ("condition", "channels", "label", "severity") if k in body}, sess))
            if p == "/api/alerts/disable": return self._send(200, a.gateway.call("disable_alert_rule", {"rule_id": str(body.get("id", ""))}, sess))
            if p == "/api/memory": return self._send(200, a.gateway.call("remember", {"text": str(body.get("text", "")), "kind": body.get("kind", "preference")}, sess))
            if p == "/api/memory/delete": return self._send(200, a.gateway.call("forget_memory", {"memory_id": str(body.get("id", ""))}, sess))
            if p == "/api/memory/correct":
                a.store.correct_memory(str(body.get("id", "")), str(body.get("text", ""))); return self._send(200, {"ok": True})
            if p == "/api/halt": return self._send(200, a.gateway.call("halt_fleet", {"reason": str(body.get("reason", "HALT button"))[:200]}, sess))
            if p == "/api/pause": return self._send(200, a.gateway.call("pause_agent", {"bot_id": str(body.get("bot_id", ""))}, sess))
            if p == "/api/task/cancel": return self._send(200, {"ok": a.tasks.cancel(str(body.get("id", "")))})
            if p == "/api/cycle":
                if a.fleet.running: return self._send(200, {"ok": False, "error": "cycle already running"})
                threading.Thread(target=a.cycle_once, daemon=True).start(); return self._send(200, {"ok": True})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    def _phone(self, p):
        """Twilio webhooks. Disabled unless JARVIS_PHONE_INBOUND=1, a PIN is set, and signatures verify."""
        a = APP
        if not a.inbound.enabled(): return self._send(404, {"error": "phone not enabled"})
        params = self._body() or {}
        url = os.environ.get("JARVIS_PUBLIC_URL", "").rstrip("/") + p
        if not a.inbound.verify(url, params, self.headers.get("X-Twilio-Signature", "")): return self._send(403, {"error": "bad signature"})
        xml = {"/phone/voice": a.inbound.voice, "/phone/pin": a.inbound.pin_entered, "/phone/speech": a.inbound.speech}.get(p)
        if not xml: return self._send(404, {"error": "not found"})
        self._send(200, xml(params).encode(), "text/xml")

    def log_message(self, *a): pass


def main():
    global APP
    if "--set-pin" in sys.argv:
        from jarvis.phone import PinAuth
        import getpass
        pin = getpass.getpass("New phone PIN (4-10 digits): ")
        PinAuth(os.environ.get("JARVUS_DIR") or os.path.join(HERE, "state")).set_pin(pin)
        print("PIN saved (salted hash only)."); return
    APP = App()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}"
    b = APP.brain_status()
    print(f"\n  JARVIS TRADING PORTAL  ->  {url}")
    print(f"  data: {APP.data.mode.upper()}   brain: {'ONLINE ' + str(b['models']) if b['ready'] else 'OFFLINE (' + b['reason'] + ')'}")
    print("  PAPER TRADING ONLY. Ctrl+C to stop.\n")
    if "--no-browser" not in sys.argv: threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally: APP.stop()


if __name__ == "__main__":
    main()
