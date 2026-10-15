"""Jarvus reporter: READ-ONLY summary of the paper log + optional phone/Telegram delivery.
Dry-run by default: nothing is sent unless you pass --send and set env vars.
HALT flag: writing the file 'HALT' stops the fleet; this script can only CREATE it, never delete it.
Usage:  python jarvus_reporter.py paper_log.csv            (print report)
        python jarvus_reporter.py paper_log.csv --json out.json
        python jarvus_reporter.py paper_log.csv --send telegram|call
        python jarvus_reporter.py --halt                   (create HALT flag)
paper_log.csv columns: date,bot,symbol,pnl,r,cost_r,status   (date=YYYY-MM-DD, pnl in dollars)
"""
import csv, json, os, sys, urllib.parse, urllib.request, base64
from datetime import datetime, timezone
from collections import defaultdict
HALT = os.environ.get("JARVUS_HALT_FILE", "HALT")

def load(path):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["pnl"] = float(r["pnl"]); r["r"] = float(r.get("r") or 0); r["cost_r"] = float(r.get("cost_r") or 0)
    return rows

def summarize(rows, capital=10000.0):
    if not rows: return {"empty": True}
    today = max(r["date"] for r in rows)
    t = [r for r in rows if r["date"] == today]
    wins = [r for r in t if r["pnl"] > 0]; losses = [r for r in t if r["pnl"] <= 0]
    by_bot = defaultdict(float)
    for r in rows: by_bot[r["bot"]] += r["pnl"]
    total = sum(r["pnl"] for r in rows)
    return {
        "date": today, "paper_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pnl_today": round(sum(r["pnl"] for r in t), 2),
        "pnl_today_pct": round(100 * sum(r["pnl"] for r in t) / capital, 2),
        "pnl_all": round(total, 2),
        "trades_today": len(t), "wins": len(wins), "losses": len(losses),
        "best": max(t, key=lambda r: r["pnl"])["bot"] if t else None,
        "worst": min(t, key=lambda r: r["pnl"])["bot"] if t else None,
        "avg_r_all": round(sum(r["r"] for r in rows) / len(rows), 3),
        "avg_cost_r": round(sum(r["cost_r"] for r in rows) / len(rows), 3),
        "halted_bots": sorted({r["bot"] for r in rows if r.get("status", "").upper() in ("PAUSED", "HALTED")}),
        "halt_flag": os.path.exists(HALT),
    }

def money(x):
    return ("-" if x < 0 else "") + f"${abs(x):,.2f}"

def bullets(s):
    if s.get("empty"): return "- No paper trades logged yet\n- Paper only."
    out = [f"- Paper P&L today: {money(s['pnl_today'])} ({s['pnl_today_pct']}%)",
           f"- Trades: {s['trades_today']} ({s['wins']} won, {s['losses']} lost)",
           f"- All-time paper P&L: {money(s['pnl_all'])}; avg {s['avg_r_all']}R; avg cost {s['avg_cost_r']}R"]
    if s["halted_bots"]: out.append("- Paused/halted: " + ", ".join(s["halted_bots"]))
    if s["halt_flag"]: out.append("- HALT flag is ON")
    out.append("- Paper only.")
    return "\n".join(out)

def send_telegram(text):
    tok, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=20)

def send_call(text):
    sid, tok = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    to, frm = os.environ["JARVUS_MY_NUMBER"], os.environ["TWILIO_FROM_NUMBER"]   # only YOUR own number
    say = text.replace("- ", "").replace("\n", ". ") + " Say HALT to stop everything."
    twiml = "<Response><Say>" + say.replace("&", "and").replace("<", "").replace(">", "") + "</Say></Response>"
    data = urllib.parse.urlencode({"To": to, "From": frm, "Twiml": twiml}).encode()
    req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json", data)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode())
    urllib.request.urlopen(req, timeout=20)

def main(a):
    if "--halt" in a:
        open(HALT, "a").write("HALT requested\n"); print("- HALT flag created"); return
    s = summarize(load(a[0])); text = bullets(s); print(text)
    if "--json" in a: json.dump(s, open(a[a.index("--json") + 1], "w"), indent=2)
    if "--send" in a:
        how = a[a.index("--send") + 1]
        {"telegram": send_telegram, "call": send_call}[how](text)

if __name__ == "__main__":
    main(sys.argv[1:])
