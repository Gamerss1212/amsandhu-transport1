"""Jarvis HUD server - your own voice "Jarvis" in the browser (like the viral video), for trading and more.

  pip install anthropic tzdata
  set ANTHROPIC_API_KEY=sk-ant-...        (Mac/Linux: export ANTHROPIC_API_KEY=...)
  python server.py                         -> opens http://127.0.0.1:8765 in your browser (use Chrome or Edge for voice)

How it works
  - The page (hud.html) listens with the browser's built-in speech recognition and talks with the browser's voice.
  - Every sentence comes here first. HALT / PAUSE / STATUS / REPORT are handled by fixed code (voice_router), no AI.
  - Anything else goes to Claude with a small set of tools: read the paper-trading status, and build a website.
  - There are NO trading tools. Paper only. The server only listens on your own computer (127.0.0.1).
Without an API key it still runs: the four fixed commands work, chat is off.
"""
from __future__ import annotations
import json, os, re, sys, threading, webbrowser
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "kit"))
import policy, tools, voice_router  # noqa: E402

STATE = os.environ.get("JARVUS_DIR", os.path.join(HERE, "state"))
REPORT = os.environ.get("JARVUS_REPORT", os.path.join(STATE, "report.json"))
FLEET = os.environ.get("JARVUS_FLEET", os.path.join(STATE, "fleet.json"))
SITES = os.path.join(HERE, "sites")
MODEL = os.environ.get("JARVIS_MODEL", "claude-sonnet-5-5")
PORT = int(os.environ.get("JARVIS_PORT", "8765"))
CFG = policy.Config(state_dir=STATE)
AUDIT = policy.AuditLog(CFG.audit_file)
import time as _time
STARTED = _time.time()
LOG: deque = deque(maxlen=60)
HISTORY: deque = deque(maxlen=8)          # last few plain-text turns, for follow-up questions

def _prompt() -> str:
    p = os.path.join(HERE, "..", "docs", "JARVUS_1_MASTER_PROMPT.md")
    base = open(p, encoding="utf-8").read() if os.path.exists(p) else "You are Jarvus. Paper trading only."
    return base + """

## HUD MODE (this overrides formatting only, never the hard rules)
You are speaking through a voice dashboard. Your reply is read out loud.
- Trading questions: follow the master prompt exactly (bullets, BUY/WAIT, paper only). Use the tools for numbers; never guess.
- Other tasks (questions, ideas, writing, websites for Abhi's web-design business ElevatedSteps): help normally, but keep the spoken reply to 1-3 short sentences.
- To build a website: call build_website with ONE complete self-contained HTML file (inline CSS/JS, modern, mobile-friendly, real-looking copy, no external images unless from a stable CDN). Then say one sentence about it.
- You cannot place trades, change limits, or resume after a HALT. If asked, say it must be done at the keyboard.
"""

TOOLS = [
    {"name": "get_status", "description": "Paper-trading status: today's paper P&L, trades, paused bots, HALT flag, report age.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_daily_report", "description": "The daily paper-trading report as short bullet lines.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_bot", "description": "Details for one bot, for example A073.",
     "input_schema": {"type": "object", "properties": {"bot_id": {"type": "string"}}, "required": ["bot_id"]}},
    {"name": "build_website", "description": "Save a complete single-file website and open it for Abhi.",
     "input_schema": {"type": "object", "properties": {"title": {"type": "string"}, "html": {"type": "string"}},
                      "required": ["title", "html"]}},
]

def log(kind: str, text: str) -> None:
    LOG.appendleft({"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, "text": text[:300]})

def build_website(title: str, html: str) -> dict:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "site").lower()).strip("-")[:40] or "site"
    os.makedirs(SITES, exist_ok=True)
    with open(os.path.join(SITES, slug + ".html"), "w", encoding="utf-8") as f:
        f.write(html)
    return {"ok": True, "url": f"/sites/{slug}.html"}

def run_tool(name: str, args: dict) -> dict:
    if name == "get_status": return tools.fleet_status(CFG, REPORT)
    if name == "get_daily_report": return {"text": tools.daily_report_text(REPORT)}
    if name == "get_bot": return tools.get_bot(FLEET, str(args.get("bot_id", "")))
    if name == "build_website": return build_website(str(args.get("title", "")), str(args.get("html", "")))
    return {"ok": False, "error": "unknown tool"}

def make_client():
    import anthropic                                   # imported here so the server runs without it
    return anthropic.Anthropic()

def ask_claude(text: str, client=None) -> tuple[str, str | None]:
    client = client or make_client()
    msgs = [m for turn in HISTORY for m in turn] + [{"role": "user", "content": text}]
    open_url = None
    for _ in range(6):
        r = client.messages.create(model=MODEL, max_tokens=16000, system=_prompt(), tools=TOOLS, messages=msgs)
        blocks = [b.model_dump() if hasattr(b, "model_dump") else b for b in r.content]
        msgs.append({"role": "assistant", "content": blocks})
        if r.stop_reason != "tool_use":
            reply = " ".join(b["text"] for b in blocks if b.get("type") == "text").strip() or "Done."
            HISTORY.append(({"role": "user", "content": text}, {"role": "assistant", "content": reply}))
            return reply, open_url
        results = []
        for b in blocks:
            if b.get("type") != "tool_use": continue
            out = run_tool(b["name"], b.get("input") or {})
            log("tool", f"{b['name']} -> {'ok' if out.get('ok', True) else out.get('error')}")
            if b["name"] == "build_website" and out.get("ok"): open_url = out["url"]
            results.append({"type": "tool_result", "tool_use_id": b["id"], "content": json.dumps(out, default=str)[:20000]})
        msgs.append({"role": "user", "content": results})
    return "I stopped after too many steps.", open_url

def handle_ask(text: str, client=None) -> dict:
    text = (text or "").strip()[:2000]
    if not text: return {"reply": "I did not catch that."}
    log("you", text)
    r = voice_router.route(text, verified=True)        # local keyboard/mic on 127.0.0.1 = you
    if r.kind == "command":
        reply = voice_router.execute(r, CFG, REPORT, audit=AUDIT)
        log("jarvis", reply); return {"reply": reply, "command": r.command}
    if client is None and not os.environ.get("ANTHROPIC_API_KEY"):
        reply = "Chat is off until you add an Anthropic API key. Status, report, halt and pause still work."
        log("jarvis", reply); return {"reply": reply}
    try:
        reply, url = ask_claude(text, client)
    except Exception as e:
        reply, url = f"I could not reach Claude ({type(e).__name__}).", None
    AUDIT.write("hud_chat", {"q": text, "a": reply[:500], "url": url})
    log("jarvis", reply)
    return {"reply": reply, "open_url": url}

def fleet() -> dict:
    """The 99 bots for the matrix: from the bots spreadsheet if openpyxl is installed, else from state/fleet.json."""
    bots, allowed = [], 0
    xl = os.path.join(HERE, "..", "data", "JARVUS_3_BOTS.xlsx")
    try:
        import openpyxl
        ws = openpyxl.load_workbook(xl, read_only=True, data_only=True)["Bots"]
        rows = list(ws.iter_rows(values_only=True)); h = {n: i for i, n in enumerate(rows[0])}
        for r in rows[1:]:
            if not r[0]: continue
            bots.append({"id": r[h["Agent"]], "status": r[h["Status"]], "symbol": r[h["Instrument"]], "group": r[h["Group"]]})
            allowed += str(r[h["Allowed to trade"]]).startswith("Yes")
    except Exception:
        try:
            for b in json.load(open(FLEET)):
                bots.append({"id": b.get("bot_id"), "status": b.get("status", "?"), "symbol": b.get("symbol", ""), "group": ""})
        except Exception: pass
    counts: dict = {}
    for b in bots: counts[b["status"]] = counts.get(b["status"], 0) + 1
    paused = policy.paused_bots(CFG)
    for b in bots: b["paused"] = b["id"] in paused
    return {"bots": bots, "counts": counts, "allowed": allowed, "total": len(bots)}

def history() -> dict:
    """Cumulative paper P&L by day from state/paper_log.csv (empty list if there is none yet)."""
    path = os.environ.get("JARVUS_LOG", os.path.join(STATE, "paper_log.csv"))
    try:
        import jarvus_reporter
        by: dict = {}
        for r in jarvus_reporter.load(path): by[r["date"]] = by.get(r["date"], 0.0) + r["pnl"]
        tot, pts = 0.0, []
        for d in sorted(by): tot += by[d]; pts.append({"date": d, "day": round(by[d], 2), "cum": round(tot, 2)})
        return {"points": pts}
    except Exception:
        return {"points": []}

def sites_list() -> list:
    if not os.path.isdir(SITES): return []
    fs = sorted((f for f in os.listdir(SITES) if f.endswith(".html")), key=lambda f: -os.path.getmtime(os.path.join(SITES, f)))
    return [{"name": f[:-5], "url": "/sites/" + f} for f in fs[:12]]

def sysinfo() -> dict:
    """Real CPU/RAM numbers. Needs `pip install psutil`; without it the panel says n/a."""
    try:
        import psutil
        return {"cpu": psutil.cpu_percent(percpu=True), "ram": psutil.virtual_memory().percent, "swap": psutil.swap_memory().percent}
    except Exception:
        return {"cpu": None, "ram": None, "swap": None}

def stats() -> dict:
    s = tools.fleet_status(CFG, REPORT)
    sm = s.get("summary", {}) if s.get("ok") else {}
    n_sites = len([f for f in os.listdir(SITES) if f.endswith(".html")]) if os.path.isdir(SITES) else 0
    return {"ok": s.get("ok", False), "paper_only": True, "pnl_today": sm.get("pnl_today"), "pnl_today_pct": sm.get("pnl_today_pct"),
            "trades_today": sm.get("trades_today"), "wins": sm.get("wins"), "losses": sm.get("losses"),
            "report_age_minutes": s.get("report_age_minutes"), "halt": policy.halt_active(CFG),
            "paused": sorted(policy.paused_bots(CFG)), "sites": n_sites, "site_list": sites_list(), "chat_on": bool(os.environ.get("ANTHROPIC_API_KEY")), "model": MODEL, "uptime_s": int(_time.time() - STARTED),
            "log": list(LOG), "sys": sysinfo(),
            "limits": {"day_loss_pct": CFG.fleet_day_loss_pct, "week_loss_pct": CFG.fleet_week_loss_pct, "max_trades_day": CFG.max_trades_per_day,
                       "max_open_stock": CFG.max_open_stock, "max_open_crypto": CFG.max_open_crypto},
            "avg_cost_r": sm.get("avg_cost_r"), "pnl_all": sm.get("pnl_all")}

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/hud.html"):
            return self._send(200, open(os.path.join(HERE, "hud.html"), "rb").read(), "text/html; charset=utf-8")
        if p == "/api/stats": return self._send(200, stats())
        if p == "/api/fleet": return self._send(200, fleet())
        if p == "/api/history": return self._send(200, history())
        if p.startswith("/sites/"):
            name = os.path.basename(p)
            f = os.path.join(SITES, name)
            if name.endswith(".html") and os.path.isfile(f):
                return self._send(200, open(f, "rb").read(), "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/ask": return self._send(404, {"error": "not found"})
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self._send(403, {"error": "wrong origin"})          # stops other websites from talking to Jarvis
        n = min(int(self.headers.get("Content-Length", 0)), 100_000)
        try: body = json.loads(self.rfile.read(n) or b"{}")
        except Exception: return self._send(400, {"error": "bad json"})
        self._send(200, handle_ask(body.get("text", "")))

    def log_message(self, *a): pass

def main():
    os.makedirs(STATE, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Jarvis HUD running at {url}  (Ctrl+C to stop). Paper only.")
    if "--no-browser" not in sys.argv: threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    srv.serve_forever()

if __name__ == "__main__":
    main()
