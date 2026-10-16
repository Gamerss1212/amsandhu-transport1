"""paper_broker.py - a PAPER broker simulator and the shadow book. No real orders exist anywhere in this file.

Book(kind="paper"): simulated money. Only bots whose role PASSED the quality gate may use it, and only after
  policy.decide said BUY. Orders fill at the NEXT bar's open (never the signal bar). Every fill is checked for:
  HALT flag, duplicate position, price collar, daily trade limit, PAUSE_NEW_ENTRIES flag; HALF_SIZE halves size.
Book(kind="shadow"): the same simulation with no money, for bots that have NOT passed. Compare it to the
  backtest: a big gap means the backtest lied.
"""
from __future__ import annotations
import csv, json, os, threading, time
from datetime import datetime, timezone
from .data_feed import BAR_SECONDS, ET, MT, Bars

COLLAR_PCT = 2.0          # a fill more than 2% away from the last close is refused (bad price protection)
LOG_FIELDS = ["date", "bot", "symbol", "pnl", "r", "cost_r", "status"]


def _now(): return time.time()


class Book:
    def __init__(self, state_dir: str, kind: str = "paper", capital: float = 10000.0):
        self.kind, self.dir = kind, state_dir
        self.path = os.path.join(state_dir, f"{kind}_book.json")
        self.log_path = os.path.join(state_dir, "paper_log.csv" if kind == "paper" else "shadow_log.csv")
        self.lock = threading.RLock()
        os.makedirs(state_dir, exist_ok=True)
        try: self.s = json.load(open(self.path))
        except Exception:
            self.s = {"capital": capital, "realized": 0.0, "positions": {}, "pending": {}, "closed": [], "equity": [],
                      "trades_by_day": {}}

    # ---------- persistence ----------
    def save(self):
        with self.lock:
            self.s["closed"] = self.s["closed"][-500:]; self.s["equity"] = self.s["equity"][-2000:]
            tmp = self.path + ".tmp"
            json.dump(self.s, open(tmp, "w")); os.replace(tmp, self.path)

    def _log(self, row: dict):
        new = not os.path.exists(self.log_path)
        with open(self.log_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if new: w.writeheader()
            w.writerow({k: row.get(k) for k in LOG_FIELDS})

    # ---------- views ----------
    def equity(self, marks: dict | None = None) -> float:
        e = self.s["capital"] + self.s["realized"]
        for p in self.s["positions"].values():
            px = (marks or {}).get(p["symbol"], p.get("last", p["entry"]))
            e += (px - p["entry"]) * p["qty"]
        return e

    def unrealized(self) -> float:
        return sum((p.get("last", p["entry"]) - p["entry"]) * p["qty"] for p in self.s["positions"].values())

    def open_risk(self) -> float:
        return sum(max(0.0, (p["entry"] - p["stop"]) * p["qty"]) for p in self.s["positions"].values())

    def trades_today(self) -> int:
        return self.s["trades_by_day"].get(datetime.now(MT).date().isoformat(), 0)

    # ---------- orders ----------
    def place(self, bot: str, symbol: str, market: str, signal_bar_t: int, bar: str, stop_atr: float, atr: float,
              target_r: float | None, flat_by: float, size_mult: float, cost_pct: float, cid: str, strategy: str,
              flags: dict, halt: bool) -> dict:
        """Queue a buy for the NEXT bar's open. Returns {ok, reason}."""
        with self.lock:
            if halt: return {"ok": False, "reason": "HALT flag is on"}
            if bot in self.s["positions"] or bot in self.s["pending"]: return {"ok": False, "reason": "duplicate: bot already has an order or position"}
            fam = "stock" if market == "stock" else "crypto"
            if flags.get(f"pause_new_{fam}") or flags.get("pause_new_all") or (market == "meme" and flags.get("pause_new_meme")):
                return {"ok": False, "reason": "new entries paused by the risk engine"}
            if self.kind == "paper" and self.trades_today() >= 5: return {"ok": False, "reason": "daily trade limit"}
            if flags.get("half_size", {}).get(bot): size_mult *= 0.5
            self.s["pending"][bot] = {"bot": bot, "symbol": symbol, "market": market, "signal_t": signal_bar_t, "bar": bar,
                                      "stop_atr": stop_atr, "atr": atr, "target_r": target_r, "flat_by": flat_by,
                                      "size_mult": size_mult, "cost_pct": cost_pct, "cid": cid, "strategy": strategy,
                                      "placed_at": _now()}
            self.save()
            return {"ok": True, "reason": "queued for next bar open"}

    def on_bars(self, bot: str, bars: Bars, halt: bool = False) -> list:
        """Advance one bot: fill a pending order at the first bar after the signal; manage the open position."""
        events = []
        with self.lock:
            pend = self.s["pending"].get(bot)
            if pend and bars.symbol == pend["symbol"]:
                nxt = [k for k, t in enumerate(bars.t) if t > pend["signal_t"]]
                if halt:
                    self.s["pending"].pop(bot); events.append({"bot": bot, "event": "order cancelled (HALT)"})
                elif nxt:
                    k = nxt[0]; fill = bars.o[k]; last = bars.c[k - 1] if k > 0 else None
                    self.s["pending"].pop(bot)
                    if last is None:
                        events.append({"bot": bot, "event": "fill refused: no previous close to check the price collar"})
                    elif abs(fill / last - 1) * 100 > COLLAR_PCT:
                        events.append({"bot": bot, "event": f"fill refused by price collar ({fill:.6g} vs {last:.6g})"})
                    else:
                        stop = fill - pend["stop_atr"] * pend["atr"]
                        risk_amt = self.equity() * 0.01 * pend["size_mult"] * (0.5 if pend["market"] == "meme" else 1)
                        qty = risk_amt / (fill - stop) if fill > stop > 0 else 0
                        qty = min(qty, 0.5 * self.equity() / fill)                       # notional cap 50% of equity
                        if qty > 0:
                            self.s["positions"][bot] = {**pend, "entry": fill, "stop0": stop, "stop": stop, "qty": qty,
                                                        "target": None if not pend["target_r"] else fill + pend["target_r"] * (fill - stop),
                                                        "entry_t": bars.t[k], "last": fill, "last_t": bars.t[k], "bars_seen": k}
                            d = datetime.now(MT).date().isoformat()
                            self.s["trades_by_day"][d] = self.s["trades_by_day"].get(d, 0) + 1
                            events.append({"bot": bot, "event": f"{self.kind} BUY {bars.symbol} filled at {fill:.6g}", "cid": pend["cid"]})
            pos = self.s["positions"].get(bot)
            if pos and bars.symbol == pos["symbol"]:
                for k, t in enumerate(bars.t):
                    if t < pos["entry_t"] or t <= pos.get("last_t_done", -1): continue
                    o, h, l, c = bars.o[k], bars.h[k], bars.l[k], bars.c[k]
                    why, px = None, None
                    if halt: why, px = "HALT: kill switch closed the position", c
                    elif o <= pos["stop"]: why, px = "stop (gap)", o
                    elif l <= pos["stop"]: why, px = "stop", pos["stop"]
                    elif pos["target"] and h >= pos["target"]: why, px = "target", max(o, pos["target"])
                    elif t + 2 * BAR_SECONDS[bars.bar] > pos["flat_by"]: why, px = "flat time", c
                    pos["last"], pos["last_t"], pos["last_t_done"] = c, t, t
                    if why:
                        events.append(self._close(bot, px, why, t)); break
                if bot in self.s["positions"]: pos["last"] = bars.c[-1]
            if events: self.save()
        return events

    def _close(self, bot: str, price: float, why: str, t: int) -> dict:
        pos = self.s["positions"].pop(bot)
        risk_u = pos["entry"] - pos["stop0"]
        cost_r = pos["cost_pct"] / 100 * pos["entry"] / risk_u if risk_u > 0 else 0
        r_g = (price - pos["entry"]) / risk_u if risk_u > 0 else 0
        r_n = r_g - cost_r
        pnl = r_n * risk_u * pos["qty"]
        self.s["realized"] += pnl
        tz = ET if pos["market"] == "stock" else MT
        row = {"date": datetime.fromtimestamp(t, tz).date().isoformat(), "bot": bot, "symbol": pos["symbol"],
               "pnl": round(pnl, 2), "r": round(r_n, 3), "cost_r": round(cost_r, 3), "status": self.kind.upper()}
        self._log(row)
        rec = {**row, "entry": pos["entry"], "exit": price, "reason": why, "strategy": pos["strategy"], "cid": pos["cid"],
               "entry_t": pos["entry_t"], "exit_t": t}
        self.s["closed"].append(rec)
        return {"bot": bot, "event": f"{self.kind} SELL {pos['symbol']} at {price:.6g} ({why}), {r_n:+.2f}R", "cid": pos["cid"]}

    def close_all(self, why: str, marks: dict) -> list:
        with self.lock:
            out = [self._close(b, marks.get(p["symbol"], p["last"]), why, int(_now())) for b, p in list(self.s["positions"].items())]
            self.s["pending"].clear(); self.save()
            return out

    def snapshot_equity(self):
        with self.lock:
            self.s["equity"].append([int(_now()), round(self.equity(), 2)])
            self.save()

    def summary(self) -> dict:
        with self.lock:
            closed = self.s["closed"]
            today = datetime.now(MT).date().isoformat()
            td = [c for c in closed if c["date"] == today]
            eq = self.equity()
            return {"kind": self.kind, "paper_only": True, "capital": self.s["capital"], "equity": round(eq, 2),
                    "realized": round(self.s["realized"], 2), "unrealized": round(self.unrealized(), 2),
                    "open_risk": round(self.open_risk(), 2), "open_positions": len(self.s["positions"]),
                    "pending_orders": len(self.s["pending"]), "pnl_today": round(sum(c["pnl"] for c in td), 2),
                    "trades_today": len(td), "wins_today": sum(1 for c in td if c["pnl"] > 0),
                    "closed_trades": len(closed),
                    "win_rate": round(sum(1 for c in closed if c["r"] > 0) / len(closed), 3) if closed else None,
                    "avg_r": round(sum(c["r"] for c in closed) / len(closed), 3) if closed else None}

    def positions(self) -> list:
        with self.lock:
            return [{"bot": b, "symbol": p["symbol"], "entry": p["entry"], "stop": p["stop"], "target": p["target"],
                     "qty": round(p["qty"], 6), "last": p["last"], "unrealized": round((p["last"] - p["entry"]) * p["qty"], 2),
                     "r_now": round((p["last"] - p["entry"]) / (p["entry"] - p["stop0"]), 2) if p["entry"] > p["stop0"] else None,
                     "strategy": p["strategy"], "flat_by": datetime.fromtimestamp(p["flat_by"], MT).isoformat(timespec="minutes"),
                     "entry_time": datetime.fromtimestamp(p["entry_t"], MT).isoformat(timespec="minutes")}
                    for b, p in self.s["positions"].items()]
