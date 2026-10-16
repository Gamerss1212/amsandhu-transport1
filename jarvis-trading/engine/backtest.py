"""backtest.py - honest backtester for one strategy on one symbol.

Rules enforced (from the Day-trade rules sheet and the master prompt):
  - signals on CLOSED bars, orders at the NEXT bar's open; never a same-bar fill
  - long only, hard stop on every trade, never widened; stops/targets fill intrabar, gaps fill at the open
  - costs on every trade (fees + spread + slippage) and the cost rule: cost in R <= 0.20 ok, 0.20-0.33 half size,
    over 0.33 = no trade
  - crypto stops at least 3 ATR, meme 4 ATR, stocks 2 ATR; LOUD = wider stop and 0.6x size; QUIET = no entry
  - stocks: entries 9:35 am-3:30 pm ET (1h bars until 2:30 pm), flat by 3:55 pm ET, never overnight
  - crypto: no Sat/Sun entries, avoid 7 pm-12 am MT, flat within 12 hours (96 in approved swing mode)
"""
from __future__ import annotations
import math, random
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from .data_feed import BAR_SECONDS, ET, MT, NYSE_EARLY_CLOSE, Bars, market_kind
from .strategies import Ctx, DEAD_HOURS_OK, entry_signals, exit_signals, spec as make_spec
from . import volatility

# Per-side costs in % of price. Fees move: use YOUR account's real tier (see the Cost in R sheet).
VENUES = {
    "ndax":         {"label": "NDAX (0.20% each way)",        "maker": 0.20, "taker": 0.20, "slip": 0.0},
    "kraken_taker": {"label": "Kraken Pro taker (0.26%)",     "maker": 0.16, "taker": 0.26, "slip": 0.0},
    "kraken_maker": {"label": "Kraken Pro maker (0.16%)",     "maker": 0.16, "taker": 0.16, "slip": 0.0},
    "maker_10bp":   {"label": "~10 bp maker tier (best case)", "maker": 0.05, "taker": 0.05, "slip": 0.0},
    "stock":        {"label": "US stock (ASSUMPTION 0.03%/side)", "maker": 0.03, "taker": 0.03, "slip": 0.0},
}
MIN_STOP_ATR = {"stock": 2.0, "crypto": 3.0, "meme": 4.0}
COST_OK, COST_MAX = 0.20, 0.33


@dataclass
class Trade:
    symbol: str
    entry_t: int
    exit_t: int
    entry: float
    exit: float
    stop: float
    target: float | None
    r_gross: float
    cost_r: float
    r_net: float
    reason: str
    bars: int
    size_mult: float
    vol_state: str
    date: str

    def as_gate(self): return {"date": self.date, "r_net": self.r_net, "cost_r": self.cost_r}


def default_venue(market: str) -> str:
    return "stock" if market == "stock" else "ndax"


def _entry_allowed(ctx: Ctx, j: int, sp: dict, market: str, swing: bool):
    """Can an order placed at the close of bar j (filled at the open of bar j+1) be an entry? (ok, reason)"""
    b = ctx.b; sec = BAR_SECONDS[b.bar]
    t_fill = b.t[j] + sec
    if market == "stock":
        et = datetime.fromtimestamp(t_fill, ET)
        if et.date() in NYSE_EARLY_CLOSE: return False, "early close day"
        last = (14, 30) if b.bar == "1h" else (15, 30)
        if sp["generator"] == "first_hour_momentum": last = (15, 30)
        if not ((9, 35) <= (et.hour, et.minute) <= last): return False, "outside entry hours"
        if j + 1 >= ctx.n or ctx.sess[j + 1] != ctx.sess[j]: return False, "next bar is a new session"
    else:
        mt = datetime.fromtimestamp(t_fill, MT)
        if mt.weekday() >= 5: return False, "weekend (no crypto entries Sat/Sun)"
        if mt.hour >= 19 and sp["generator"] not in DEAD_HOURS_OK: return False, "dead hours 7 pm-12 am MT"
    return True, ""


def run(bars: Bars, strategy_id: str, settings=None, market: str | None = None, venue: str | None = None,
        vol_gate: bool = True, rules: bool = True, swing: bool | None = None, benchmark: Bars | None = None,
        execution: str = "market", risk_pct: float = 1.0, capital: float = 10000.0, signals: list | None = None,
        vol_states: list | None = None, start: int = 0, end: int | None = None, keep_equity: bool = True) -> dict:
    sp = make_spec(strategy_id, settings) if isinstance(strategy_id, str) else strategy_id
    p = sp["params"]
    market = market or market_kind(bars.symbol)
    venue = venue or default_venue(market)
    fees = VENUES[venue]
    swing = bool(p.get("swing")) if swing is None else swing
    if vol_states is None and (vol_gate or sp["generator"] == "vol_gate_swing"):
        vol_states = volatility.gate(bars)["states"] if len(bars) > 60 else ["UNKNOWN"] * len(bars)
    ctx = Ctx(bars, "crypto" if market == "meme" else market, benchmark, vol_states)
    n = ctx.n; end = n if end is None else min(end, n)
    sig = signals if signals is not None else entry_signals(sp, ctx)
    xsig = exit_signals(sp, ctx)
    a14 = ctx.atr(14)
    sec = BAR_SECONDS[bars.bar]
    stop_atr = p.get("stop_atr", 2.0)
    if rules: stop_atr = max(stop_atr, MIN_STOP_ATR[market])
    exit_kind = sp["exit"]
    max_bars = p.get("max_bars", 20) if exit_kind in ("time",) or swing else None
    if rules and market == "stock" and bars.bar == "1h": max_bars = min(max_bars or 5, 5)
    flat_hours = 96 if swing else 12
    entry_fee = fees["maker"] if execution == "maker" else fees["taker"]
    risk_frac = risk_pct / 100 * (0.5 if market == "meme" else 1.0)

    trades, skipped = [], {}
    eq, peak, max_dd = capital, capital, 0.0
    curve = []
    pos = None
    pending = None             # (signal bar j, size_mult, stop_atr_used, vol_state, cost_r_est)
    exit_next = None

    def skip(why): skipped[why] = skipped.get(why, 0) + 1

    def close(k, price, why):
        nonlocal pos, eq, peak, max_dd
        risk_u = pos["entry"] - pos["stop0"]
        exit_fee = fees["maker"] if (why == "target" and execution == "maker") else fees["taker"]
        cost_pct = entry_fee + exit_fee + 2 * fees["slip"]
        cost_r = cost_pct / 100 * pos["entry"] / risk_u
        r_g = (price - pos["entry"]) / risk_u
        r_n = r_g - cost_r
        tz = ET if market == "stock" else MT
        tr = Trade(bars.symbol, bars.t[pos["i"]], bars.t[k], round(pos["entry"], 8), round(price, 8), round(pos["stop0"], 8),
                   None if pos["target"] is None else round(pos["target"], 8), round(r_g, 4), round(cost_r, 4), round(r_n, 4),
                   why, k - pos["i"] + 1, pos["mult"], pos["vs"], datetime.fromtimestamp(bars.t[pos["i"]], tz).date().isoformat())
        trades.append(tr)
        eq += r_n * pos["risk_amt"]; peak = max(peak, eq); max_dd = min(max_dd, eq / peak - 1)
        pos = None

    for j in range(start, end):
        o, h, l, c = bars.o[j], bars.h[j], bars.l[j], bars.c[j]
        # 1. exits decided at the previous close execute at this open
        if pos and exit_next:
            close(j, o, exit_next); exit_next = None
        # 2. pending entry fills at this open
        if pending is not None and pos is None:
            jj, mult, s_atr, vs, _ = pending; pending = None
            fill = o
            if execution == "maker":
                limit = bars.c[jj]
                if l > limit: skip("limit order not filled"); fill = None
                else: fill = min(o, limit)
            if fill is not None:
                stop = fill - s_atr * a14[jj]
                if stop > 0 and stop < fill:
                    target = fill + p.get("target_r", 2.0) * (fill - stop) if exit_kind == "atr_bracket" else None
                    pos = {"i": j, "entry": fill, "stop0": stop, "stop": stop, "target": target, "mult": mult, "vs": vs,
                           "hi": fill, "risk_amt": eq * risk_frac * mult, "trail": s_atr if exit_kind == "trail" else None}
        # 3. intrabar stop / target (pessimistic: stop first)
        if pos:
            if o <= pos["stop"]: close(j, o, "stop (gap)")
            elif l <= pos["stop"]: close(j, pos["stop"], "stop" if pos["stop"] <= pos["stop0"] else "trail stop")
            elif pos["target"] is not None and h >= pos["target"]: close(j, max(o, pos["target"]), "target")
        # 4. end of bar: trail, time, signal exits, flat rules
        if pos:
            held = j - pos["i"] + 1
            pos["hi"] = max(pos["hi"], c)
            if pos["trail"] and a14[j]:
                pos["stop"] = max(pos["stop"], pos["hi"] - pos["trail"] * a14[j])     # only ever tightens
            t_close = bars.t[j] + sec
            if rules and market == "stock":
                et = datetime.fromtimestamp(t_close, ET)
                last_bar = j + 1 >= n or ctx.sess[j + 1] != ctx.sess[j]
                nx = datetime.fromtimestamp(t_close + sec, ET)            # the NEXT bar would end after 3:55 pm ET
                if last_bar or (et.hour, et.minute) >= (15, 55) or (nx.hour, nx.minute) > (15, 55):
                    close(j, c, "flat by close"); continue
            elif rules and (t_close - bars.t[pos["i"]]) >= flat_hours * 3600:
                close(j, c, f"flat after {flat_hours}h"); continue
            if max_bars and held >= max_bars: exit_next = "time"
            elif xsig and xsig[j]: exit_next = "signal exit"
        # 5. new entry signal at this close
        if pos is None and pending is None and j + 1 < end and sig[j]:
            if a14[j] is None: skip("warm-up"); continue
            ok, why = _entry_allowed(ctx, j, sp, market, swing) if rules else (True, "")
            if not ok: skip(why); continue
            vs = vol_states[j] if vol_states else "NORMAL"
            mult, s_atr = 1.0, stop_atr
            if vol_gate:
                if vs == "QUIET": skip("volatility QUIET"); continue
                if vs not in ("LOUD", "NORMAL"): skip("volatility unknown"); continue
                if vs == "LOUD": mult *= 0.6; s_atr *= 1.25
            stop_pct = s_atr * a14[j] / c * 100
            cost_r = (entry_fee + fees["taker"] + 2 * fees["slip"]) / stop_pct
            if rules and cost_r > COST_MAX: skip("cost over 0.33R"); continue
            if rules and cost_r > COST_OK: mult *= 0.5
            pending = (j, mult, s_atr, vs, cost_r)
        if keep_equity and (j % 6 == 0 or j == end - 1): curve.append([bars.t[j], round(eq, 2)])
    if pos: close(end - 1, bars.c[end - 1], "end of data")

    res = {"strategy": sp["id"], "symbol": bars.symbol, "bar": bars.bar, "market": market, "venue": venue,
           "venue_label": fees["label"], "execution": execution, "vol_gate": vol_gate, "rules": rules, "swing": swing,
           "stop_atr_used": stop_atr, "params": p, "source": bars.source,
           "period": [bars.t[start] if n else None, bars.t[end - 1] if n else None], "bars_tested": end - start,
           "trades": [asdict(t) for t in trades], "skipped": skipped, "stats": stats(trades, capital, eq, max_dd),
           "equity": curve, "signals": sum(sig[start:end])}
    return res


def stats(trades: list, capital: float = 10000.0, eq: float | None = None, max_dd: float = 0.0) -> dict:
    rs = [t.r_net if isinstance(t, Trade) else t["r_net"] for t in trades]
    gs = [t.r_gross if isinstance(t, Trade) else t["r_gross"] for t in trades]
    cs = [t.cost_r if isinstance(t, Trade) else t["cost_r"] for t in trades]
    n = len(rs)
    if n == 0:
        return {"trades": 0, "hit_rate": None, "avg_r_net": None, "avg_r_gross": None, "avg_cost_r": None,
                "profit_factor": None, "max_dd_r": 0.0, "max_dd_pct": round(100 * max_dd, 2), "total_r": 0.0,
                "return_pct": 0.0 if eq is None else round(100 * (eq / capital - 1), 2), "sharpe_per_trade": None,
                "longest_losing_streak": 0}
    wins = [r for r in rs if r > 0]; losses = [r for r in rs if r <= 0]
    cum = peak = dd = 0.0; streak = worst = 0
    for r in rs:
        cum += r; peak = max(peak, cum); dd = min(dd, cum - peak)
        streak = streak + 1 if r <= 0 else 0; worst = max(worst, streak)
    m = sum(rs) / n; sd = math.sqrt(sum((r - m) ** 2 for r in rs) / n) if n > 1 else 0
    return {"trades": n, "hit_rate": round(len(wins) / n, 3), "avg_r_net": round(m, 3), "avg_r_gross": round(sum(gs) / n, 3),
            "avg_cost_r": round(sum(cs) / n, 3),
            "profit_factor": None if not losses or sum(losses) == 0 else round(sum(wins) / abs(sum(losses)), 2),
            "max_dd_r": round(dd, 2), "max_dd_pct": round(100 * max_dd, 2), "total_r": round(sum(rs), 2),
            "return_pct": None if eq is None else round(100 * (eq / capital - 1), 2),
            "sharpe_per_trade": None if sd == 0 else round(m / sd, 3), "longest_losing_streak": worst}


def every_bar_benchmark(bars: Bars, strategy_id: str, settings=None, **kw) -> dict:
    """Same structure, but enter at every allowed bar: does the setup beat 'just be long'?"""
    sp = make_spec(strategy_id, settings)
    r = run(bars, sp, signals=[1] * len(bars), keep_equity=False, **kw)
    return r["stats"]


def buy_and_hold(bars: Bars, start: int = 0, end: int | None = None) -> dict:
    end = len(bars) if end is None else end
    if end - start < 2: return {"return_pct": None, "sharpe_daily": None}
    c = bars.c[start:end]; t = bars.t[start:end]
    daily = {}
    for ti, ci in zip(t, c): daily[datetime.fromtimestamp(ti, MT).date()] = ci
    closes = list(daily.values())
    rets = [closes[k] / closes[k - 1] - 1 for k in range(1, len(closes))]
    sh = None
    if len(rets) > 5:
        m = sum(rets) / len(rets); sd = math.sqrt(sum((r - m) ** 2 for r in rets) / len(rets))
        sh = None if sd == 0 else round(m / sd * math.sqrt(365 if market_kind(bars.symbol) != "stock" else 252), 3)
    return {"return_pct": round(100 * (c[-1] / c[0] - 1), 2), "sharpe_daily": sh}


def strategy_daily_sharpe(trades: list, market: str) -> float | None:
    """Annualised Sharpe of daily R (realised at entry date). Days without trades count as 0."""
    if not trades: return None
    by = {}
    for t in trades: by[t["date"]] = by.get(t["date"], 0.0) + t["r_net"]
    d0 = datetime.fromisoformat(min(by)).date(); d1 = datetime.fromisoformat(max(by)).date()
    days, d = [], d0
    while d <= d1:
        if market != "stock" or d.weekday() < 5: days.append(by.get(d.isoformat(), 0.0))
        d += timedelta(days=1)
    if len(days) < 5: return None
    m = sum(days) / len(days); sd = math.sqrt(sum((x - m) ** 2 for x in days) / len(days))
    return None if sd == 0 else round(m / sd * math.sqrt(252 if market == "stock" else 365), 3)
