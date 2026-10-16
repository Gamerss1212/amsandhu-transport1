"""strategies.py - every strategy in JARVUS_2_STRATEGIES.xlsx as code.

A strategy id has three parts:  generator . filter . exit      e.g. rsi2_reversion.trend200.time
  - generator: produces a NEW LONG signal on a CLOSED bar (1 = signal on bar i). Long only, always.
  - filter:    none / relvol / adx / trend200 / vol_band / session (plus the special filters of the New sheet)
  - exit:      atr_bracket (stop + 2R target) / time (max bars + stop) / trail (ATR trailing stop)
Orders always go in at the NEXT bar's open (the backtester and the fleet runner do that, not this file).

Nothing here has shown profit after costs. Signals are research inputs for the gate, never orders.
"""
from __future__ import annotations
import math
from datetime import datetime
from . import indicators as ind
from .data_feed import ET, MT, Bars, market_kind, session_key


# ---------------- context: lazily computed indicators for one Bars object ----------------
class Ctx:
    def __init__(self, bars: Bars, market: str | None = None, benchmark: Bars | None = None, vol_states: list | None = None):
        self.b = bars
        self.market = market or market_kind(bars.symbol)
        self.benchmark = benchmark
        self.vol_states = vol_states            # per-bar LOUD/NORMAL/QUIET from the volatility gate (optional)
        self._c: dict = {}
        self.n = len(bars)

    def get(self, key, fn):
        if key not in self._c: self._c[key] = fn()
        return self._c[key]

    # common series
    @property
    def sess(self): return self.get("sess", lambda: [session_key(t, self.market) for t in self.b.t])
    def atr(self, n=14): return self.get(("atr", n), lambda: ind.atr(self.b.h, self.b.l, self.b.c, n))
    def rsi(self, n): return self.get(("rsi", n), lambda: ind.rsi(self.b.c, n))
    def sma(self, n): return self.get(("sma", n), lambda: ind.sma(self.b.c, n))
    def ema(self, n): return self.get(("ema", n), lambda: ind.ema(self.b.c, n))
    def adx(self, n=14): return self.get(("adx", n), lambda: ind.adx(self.b.h, self.b.l, self.b.c, n))
    def relvol(self, n=20): return self.get(("rv", n), lambda: ind.relvol(self.b.v, n))
    def vwap(self): return self.get("vwap", lambda: ind.session_vwap(self.b.h, self.b.l, self.b.c, self.b.v, self.sess))

    def local(self, i):
        tz = ET if self.market == "stock" else MT
        return datetime.fromtimestamp(self.b.t[i], tz)

    def session_starts(self):
        """Index of the first bar of each session, per bar."""
        def f():
            out, first, key = [0] * self.n, 0, None
            for i, s in enumerate(self.sess):
                if s != key: key, first = s, i
                out[i] = first
            return out
        return self.get("sstart", f)

    def prev_session(self):
        """(high, low, close) of the previous session, per bar."""
        def f():
            out, cur, prev, key = [None] * self.n, None, None, None
            for i, s in enumerate(self.sess):
                if s != key:
                    prev, key = cur, s
                    cur = [self.b.h[i], self.b.l[i], self.b.c[i]]
                else:
                    cur[0] = max(cur[0], self.b.h[i]); cur[1] = min(cur[1], self.b.l[i]); cur[2] = self.b.c[i]
                out[i] = tuple(prev) if prev else None
            return out
        return self.get("psess", f)


def _once_per_session(ctx: Ctx, sig: list) -> list:
    out, seen = [0] * len(sig), set()
    for i, s in enumerate(sig):
        if s and ctx.sess[i] not in seen:
            out[i] = 1; seen.add(ctx.sess[i])
    return out


def _bar_minutes(ctx: Ctx) -> int:
    from .data_feed import BAR_SECONDS
    return BAR_SECONDS[ctx.b.bar] // 60


def _opening_range(ctx: Ctx, bars_in_range: int, start_hm=None):
    """Signal when a bar closes above the high of the first `bars_in_range` bars of the session
    (or of the bars from start_hm local time). Returns (signal, or_high, or_low)."""
    b, n = ctx.b, ctx.n
    sig, orh, orl = [0] * n, [None] * n, [None] * n
    ss = ctx.session_starts()
    anchor = {}
    if start_hm is not None:                    # first bar of each session that starts exactly at start_hm
        for j in range(n):
            lt = ctx.local(j)
            if (lt.hour, lt.minute) == start_hm: anchor.setdefault(ctx.sess[j], j)
    for i in range(n):
        s0 = ss[i]
        if start_hm is not None:
            s0 = anchor.get(ctx.sess[i])
            if s0 is None or s0 > i: continue
        k = i - s0
        if k < bars_in_range: continue
        hi = max(b.h[s0:s0 + bars_in_range]); lo = min(b.l[s0:s0 + bars_in_range])
        orh[i], orl[i] = hi, lo
        if b.c[i] > hi and (k == bars_in_range or b.c[i - 1] <= hi): sig[i] = 1
    return _once_per_session(ctx, sig), orh, orl


# ---------------- generators (Keep sheet) ----------------
def g_active_orb(ctx, p):
    sig, _, _ = _opening_range(ctx, int(p.get("bars", 2)))
    b, ss = ctx.b, ctx.session_starts()
    # "active": the opening candle itself closed up (Zarattini et al. stocks-in-play idea, one-stock version)
    return [1 if s and b.c[ss[i]] > b.o[ss[i]] else 0 for i, s in enumerate(sig)]

def g_orb(ctx, p):
    return _opening_range(ctx, int(p.get("bars", 3)))[0]

def g_connors_rsi_rev(ctx, p):
    cr = ctx.get("crsi", lambda: ind.connors_rsi(ctx.b.c))
    lo = p.get("lo", 10)
    return [1 if v is not None and v < lo else 0 for v in cr]

def g_donchian_trend(ctx, p):
    hh = ind.rolling_max(ctx.b.h, int(p.get("entry", 55)), shift=1)
    return [1 if hh[i] is not None and ctx.b.c[i] > hh[i] else 0 for i in range(ctx.n)]

def x_donchian_trend(ctx, p):
    """Generator-specific exit: close below the lowest low of the previous `exit` bars."""
    ll = ind.rolling_min(ctx.b.l, int(p.get("exit", 20)), shift=1)
    return [ll[i] is not None and ctx.b.c[i] < ll[i] for i in range(ctx.n)]

def g_first_hour_momentum(ctx, p):
    """Gao, Han, Li, Zhou (JFE): the first 30 minutes' return (from the prior close) predicts the last
    30 minutes. Long at the bar that closes at 3:30 pm ET when the first-half-hour return was positive."""
    if ctx.market != "stock": return [0] * ctx.n
    b, out, ss, ps = ctx.b, [0] * ctx.n, ctx.session_starts(), ctx.prev_session()
    bm = _bar_minutes(ctx)
    for i in range(ctx.n):
        lt = datetime.fromtimestamp(b.t[i] + bm * 60, ET)            # bar close time
        if (lt.hour, lt.minute) != (15, 30) or ps[i] is None: continue
        s0 = ss[i]
        j = next((k for k in range(s0, i) if datetime.fromtimestamp(b.t[k] + bm * 60, ET) >= lt.replace(hour=10, minute=0)), None)
        if j is not None and b.c[j] > ps[i][2]: out[i] = 1
    return out

def g_ibs_reversion(ctx, p):
    v = ctx.get("ibs", lambda: ind.ibs(ctx.b.h, ctx.b.l, ctx.b.c))
    return [1 if x is not None and x < p.get("lo", 0.2) else 0 for x in v]

def g_noise_band_momentum(ctx, p):
    """Zarattini/Aziz/Barbon noise area: upper band = session open x (1 + m x average absolute move from the
    open at the same bar-of-day over the last n sessions). Long on a close above the band."""
    b, n_days, m = ctx.b, int(p.get("n", 14)), p.get("m", 1.0)
    ss, out, hist = ctx.session_starts(), [0] * ctx.n, {}
    for i in range(ctx.n):
        k = i - ss[i]; op = b.o[ss[i]]
        moves = hist.get(k, [])
        if len(moves) >= n_days:
            band = op * (1 + m * sum(moves[-n_days:]) / n_days)
            if b.c[i] > band and (k == 0 or b.c[i - 1] <= band): out[i] = 1
        hist.setdefault(k, []).append(abs(b.c[i] / op - 1))
    return _once_per_session(ctx, out)

def g_nr7_breakout(ctx, p):
    n, b = int(p.get("n", 7)), ctx.b
    rng = [h - l for h, l in zip(b.h, b.l)]
    out = [0] * ctx.n
    for i in range(n, ctx.n):
        if rng[i - 1] <= min(rng[i - n:i]) and b.c[i] > b.h[i - 1]: out[i] = 1
    return out

def g_rsi2_reversion(ctx, p):
    r = ctx.rsi(2)
    return [1 if v is not None and v < p.get("lo", 10) else 0 for v in r]

def x_rsi2_reversion(ctx, p):
    r = ctx.rsi(2); lvl = p.get("exit", 70)
    return [v is not None and v > lvl for v in r]

def g_vol_breakout(ctx, p):
    """Larry Williams volatility breakout: close above the session open + k x the previous session's range."""
    b, k, ss, ps = ctx.b, p.get("k", 0.6), ctx.session_starts(), ctx.prev_session()
    out = [1 if ps[i] and b.c[i] > b.o[ss[i]] + k * (ps[i][0] - ps[i][1]) else 0 for i in range(ctx.n)]
    return _once_per_session(ctx, out)

def g_bollinger_reversion(ctx, p):
    _, _, lo = ctx.get(("bb", p.get("n", 20), p.get("k", 2.0)), lambda: ind.bollinger(ctx.b.c, int(p.get("n", 20)), p.get("k", 2.0)))
    return [1 if lo[i] is not None and ctx.b.c[i] < lo[i] else 0 for i in range(ctx.n)]

def g_initial_balance_breakout(ctx, p):
    if ctx.market != "stock": return [0] * ctx.n
    k = max(1, 60 // _bar_minutes(ctx))                              # first hour of the session
    return _opening_range(ctx, k)[0]

def g_squeeze_breakout(ctx, p):
    b, need = ctx.b, int(p.get("p", 10))
    mid, bu, bl = ind.bollinger(b.c, 20, 2.0)
    _, ku, kl = ind.keltner(b.h, b.l, b.c, 20, 1.5)
    out, run = [0] * ctx.n, 0
    for i in range(ctx.n):
        if None in (bu[i], ku[i], bl[i], kl[i]): continue
        squeezed = bu[i] < ku[i] and bl[i] > kl[i]
        if not squeezed and run >= need and b.c[i] > mid[i] and b.c[i] > b.c[i - 1]: out[i] = 1
        run = run + 1 if squeezed else 0
    return out

def g_zscore_reversion(ctx, p):
    z = ctx.get(("z", p.get("n", 50)), lambda: ind.zscore(ctx.b.c, int(p.get("n", 50))))
    return [1 if v is not None and v < -p.get("z", 2.0) else 0 for v in z]

def g_crypto_weekend(ctx, p):
    """Long into the weekend: the last bar before Friday 4 pm MT. (Weekend ENTRIES are blocked by the rules.)"""
    if ctx.market == "stock": return [0] * ctx.n
    bm = _bar_minutes(ctx); out = [0] * ctx.n
    for i in range(ctx.n):
        lt = datetime.fromtimestamp(ctx.b.t[i] + bm * 60, MT)
        if lt.weekday() == 4 and lt.hour == 15 and lt.minute == 0: out[i] = 1
    return out

def g_failed_breakout(ctx, p):
    """Failed breakdown: price closes below the n-bar low, then within m bars closes back above it."""
    b, n, m = ctx.b, int(p.get("n", 20)), int(p.get("m", 3))
    ll = ind.rolling_min(b.l, n, shift=1)
    out, broke = [0] * ctx.n, None
    for i in range(ctx.n):
        if ll[i] is None: continue
        if b.c[i] < ll[i]: broke = (i, ll[i])
        elif broke and i - broke[0] <= m and b.c[i] > broke[1]:
            out[i] = 1; broke = None
        elif broke and i - broke[0] > m: broke = None
    return out

def _gap(ctx, g, up: bool):
    if ctx.market != "stock": return [0] * ctx.n
    b, ss, ps, out = ctx.b, ctx.session_starts(), ctx.prev_session(), [0] * ctx.n
    for i in range(ctx.n):
        if ss[i] != i or ps[i] is None: continue                    # signal on the first bar of the day
        gap = b.o[i] / ps[i][2] - 1
        if up and gap > g and b.c[i] > b.o[i]: out[i] = 1
        if not up and gap < -g and b.c[i] > b.o[i]: out[i] = 1    # long-only fade of a gap DOWN
    return out

def g_gap_continuation(ctx, p): return _gap(ctx, p.get("g", 0.005), True)
def g_gap_fade(ctx, p): return _gap(ctx, p.get("g", 0.004), False)

def g_keltner_reversion(ctx, p):
    _, _, lo = ctx.get(("kc", p.get("n", 20), p.get("m", 2.0)),
                       lambda: ind.keltner(ctx.b.h, ctx.b.l, ctx.b.c, int(p.get("n", 20)), p.get("m", 2.0)))
    return [1 if lo[i] is not None and ctx.b.c[i] < lo[i] else 0 for i in range(ctx.n)]

def g_pairs_zscore(ctx, p):
    """Long the symbol when its log-price spread vs a benchmark (BTC for crypto) is n-bar z-score below -z.
    No benchmark supplied = no signals (fail closed)."""
    bm = ctx.benchmark
    if bm is None or bm.symbol == ctx.b.symbol: return [0] * ctx.n
    idx = {t: k for k, t in enumerate(bm.t)}
    spread = [math.log(c) - math.log(bm.c[idx[t]]) if t in idx else None for t, c in zip(ctx.b.t, ctx.b.c)]
    n, z = int(p.get("n", 100)), p.get("z", 2.0)
    out = [0] * ctx.n
    for i in range(n, ctx.n):
        w = spread[i - n + 1:i + 1]
        if any(v is None for v in w): continue
        mu = sum(w) / n; sd = math.sqrt(sum((v - mu) ** 2 for v in w) / n)
        if sd > 0 and (spread[i] - mu) / sd < -z: out[i] = 1
    return out

def g_prior_session_breakout(ctx, p):
    ps = ctx.prev_session()
    out = [1 if ps[i] and ctx.b.c[i] > ps[i][0] else 0 for i in range(ctx.n)]
    return _once_per_session(ctx, out)

def g_sweep_reclaim(ctx, p):
    """Liquidity sweep: the bar's low pierces the n-bar low by x ATR and the bar closes back above it.
    `hold` = minimum bars between two signals."""
    b, n, x, hold = ctx.b, int(p.get("n", 20)), p.get("x", 0.1), int(p.get("hold", 10))
    ll, a = ind.rolling_min(b.l, n, shift=1), ctx.atr(14)
    out, last = [0] * ctx.n, -10 ** 9
    for i in range(ctx.n):
        if ll[i] is None or a[i] is None: continue
        if b.l[i] < ll[i] - x * a[i] and b.c[i] > ll[i] and i - last >= hold:
            out[i] = 1; last = i
    return out

def g_variance_ratio_switch(ctx, p):
    """Variance ratio > 1 (trending): momentum entry. < 1 (mean reverting): dip entry. Long only."""
    vr = ctx.get("vr", lambda: ind.variance_ratio(ctx.b.c, 4, 100))
    z, e20 = ctx.get(("z", 20), lambda: ind.zscore(ctx.b.c, 20)), ctx.ema(20)
    b, out = ctx.b, [0] * ctx.n
    for i in range(5, ctx.n):
        if vr[i] is None or z[i] is None or e20[i] is None: continue
        if vr[i] > 1.1 and b.c[i] > b.c[i - 4] and b.c[i] > e20[i] and b.c[i - 1] <= e20[i - 1]: out[i] = 1
        if vr[i] < 0.9 and z[i] < -1.5 <= (z[i - 1] or 0): out[i] = 1
    return out

def g_volume_breakout(ctx, p):
    hh, rv = ind.rolling_max(ctx.b.h, 20, shift=1), ctx.relvol(20)
    return [1 if hh[i] is not None and rv[i] is not None and rv[i] > p.get("rv", 2.0) and ctx.b.c[i] > hh[i] else 0
            for i in range(ctx.n)]


# ---------------- generators (New (to build) sheet) ----------------
def g_vwap_reversion(ctx, p):
    vw, dev = ctx.vwap(); b = ctx.b; k = p.get("k", 2.0)
    return [1 if dev[i] and b.c[i] < vw[i] - k * dev[i] else 0 for i in range(ctx.n)]

def x_vwap_reversion(ctx, p):
    vw, _ = ctx.vwap()
    return [ctx.b.c[i] >= vw[i] for i in range(ctx.n)]

def g_vwap_trend_pullback(ctx, p):
    (a, pdi, mdi), (vw, _) = ctx.adx(14), ctx.vwap()
    b, out = ctx.b, [0] * ctx.n
    for i in range(1, ctx.n):
        if a[i] is None: continue
        if a[i] > 25 and pdi[i] > mdi[i] and b.l[i] <= vw[i] < b.c[i] and b.c[i] > b.o[i]: out[i] = 1
    return out

def g_orb_stocks_in_play(ctx, p):
    """One-stock slice of the pooled stocks-in-play ORB. The fleet ranks the universe; here: first 5-min candle
    closed up, opening relative volume >= 1.0 (vs the same bar on the last 14 sessions), ATR above 1% of price."""
    if ctx.market != "stock": return [0] * ctx.n
    b, ss, out = ctx.b, ctx.session_starts(), [0] * ctx.n
    a = ctx.atr(14); first_vols = []
    for i in range(ctx.n):
        if ss[i] != i: continue
        rv = b.v[i] / (sum(first_vols[-14:]) / len(first_vols[-14:])) if len(first_vols) >= 5 else 0
        first_vols.append(b.v[i])
        if rv >= p.get("rvol", 1.0) and a[i] and a[i] / b.c[i] * math.sqrt(78) > 0.01 and b.c[i] > b.o[i]:
            out[i] = 1
    return out

def g_vol_gate_swing(ctx, p):
    """BTC vol-gate swing. Direction is barely predictable (AUC 0.513), so the entry is a plain trend condition
    (close above the 50-bar EMA, at most one entry per 24 bars); the edge, if any, comes from the volatility gate
    (skip QUIET), the wide 4 ATR stop, the 2R target and the 96-bar limit."""
    e50, b, out, last = ctx.ema(50), ctx.b, [0] * ctx.n, -10 ** 9
    for i in range(ctx.n):
        if e50[i] is None: continue
        vs = ctx.vol_states[i] if ctx.vol_states else "NORMAL"
        if vs in ("LOUD", "NORMAL") and b.c[i] > e50[i] and i - last >= 24:
            out[i] = 1; last = i
    return out

def g_crypto_session_orb(ctx, p):
    """BTC/ETH/SOL opening range at the US stock open: first 15 minutes after 7:30 am MT. Needs 5m bars."""
    if ctx.market == "stock" or _bar_minutes(ctx) > 15: return [0] * ctx.n
    k = max(1, 15 // _bar_minutes(ctx))
    sig, orh, orl = _opening_range(ctx, k, start_hm=(7, 30))
    a, rv, e = ctx.atr(14), ctx.relvol(20), ctx.ema(12 * 60 // _bar_minutes(ctx))
    out = [0] * ctx.n
    for i, s in enumerate(sig):
        if not s or a[i] is None or rv[i] is None or e[i] is None: continue
        if orh[i] - orl[i] > 1.5 * a[i] * math.sqrt(60 / _bar_minutes(ctx)): continue      # range already used up
        if rv[i] > 1.5 and ctx.b.c[i] > e[i]: out[i] = 1
    return out

def g_btc_led_alt_trend(ctx, p):
    bm = ctx.benchmark
    if bm is None or ctx.market == "stock": return [0] * ctx.n
    e200 = ind.ema(bm.c, 200); on = {t: (e200[k] is not None and bm.c[k] > e200[k]) for k, t in enumerate(bm.t)}
    hh = ind.rolling_max(ctx.b.h, 20, shift=1)
    return [1 if hh[i] is not None and on.get(t) and ctx.b.c[i] > hh[i] else 0 for i, t in enumerate(ctx.b.t)]

def g_attention_momentum(ctx, p):
    bm = ctx.benchmark
    if ctx.market == "stock": return [0] * ctx.n
    on = None
    if bm is not None:
        e200 = ind.ema(bm.c, 200); on = {t: (e200[k] is not None and bm.c[k] > e200[k]) for k, t in enumerate(bm.t)}
    hh, rv = ind.rolling_max(ctx.b.h, 24, shift=1), ctx.relvol(24)
    return [1 if hh[i] is not None and rv[i] is not None and rv[i] > 3.0 and ctx.b.c[i] > hh[i]
            and (on is None or on.get(t)) else 0 for i, t in enumerate(ctx.b.t)]


GENERATORS = {
    "active_orb": g_active_orb, "orb": g_orb, "connors_rsi_rev": g_connors_rsi_rev, "donchian_trend": g_donchian_trend,
    "first_hour_momentum": g_first_hour_momentum, "ibs_reversion": g_ibs_reversion,
    "noise_band_momentum": g_noise_band_momentum, "nr7_breakout": g_nr7_breakout, "rsi2_reversion": g_rsi2_reversion,
    "vol_breakout": g_vol_breakout, "bollinger_reversion": g_bollinger_reversion,
    "initial_balance_breakout": g_initial_balance_breakout, "squeeze_breakout": g_squeeze_breakout,
    "zscore_reversion": g_zscore_reversion, "crypto_weekend": g_crypto_weekend, "failed_breakout": g_failed_breakout,
    "gap_continuation": g_gap_continuation, "gap_fade": g_gap_fade, "keltner_reversion": g_keltner_reversion,
    "pairs_zscore": g_pairs_zscore, "prior_session_breakout": g_prior_session_breakout, "sweep_reclaim": g_sweep_reclaim,
    "variance_ratio_switch": g_variance_ratio_switch, "volume_breakout": g_volume_breakout,
    # New (to build)
    "vwap_reversion": g_vwap_reversion, "vwap_trend_pullback": g_vwap_trend_pullback, "orb_stocks_in_play": g_orb_stocks_in_play,
    "vol_gate_swing": g_vol_gate_swing, "crypto_session_orb": g_crypto_session_orb, "btc_led_alt_trend": g_btc_led_alt_trend,
    "attention_momentum": g_attention_momentum,
}
SIGNAL_EXITS = {"donchian_trend": x_donchian_trend, "rsi2_reversion": x_rsi2_reversion, "vwap_reversion": x_vwap_reversion}
DEAD_HOURS_OK = {"crypto_weekend"}                 # built for off-hours (still blocked on weekends by the rules)


# ---------------- filters ----------------
def f_none(ctx, i, p): return True
def f_relvol(ctx, i, p):
    rv = ctx.relvol(20)[i]; return rv is not None and rv >= p.get("rv", 1.5)
def f_adx(ctx, i, p):
    a = ctx.adx(14)[0][i]; return a is not None and a >= 20
def f_trend200(ctx, i, p):
    s = ctx.sma(200)[i]; return s is not None and ctx.b.c[i] > s
def f_vol_band(ctx, i, p):
    """Volatility inside its normal band: ATR% between the 20th and 80th percentile of the last 200 bars."""
    pr = ctx.get("atrpr", lambda: ind.pct_rank_window([None if a is None else a / c for a, c in zip(ctx.atr(14), ctx.b.c)], 200))
    return pr[i] is not None and 0.2 <= pr[i] <= 0.8
def f_session(ctx, i, p):
    lt = ctx.local(i)
    if ctx.market == "stock": return (9, 35) <= (lt.hour, lt.minute) <= (15, 30)
    return (7, 30) <= (lt.hour, lt.minute) <= (14, 0)               # crypto: US-hours liquidity window (MT)
def f_vol_gate(ctx, i, p):
    return not ctx.vol_states or ctx.vol_states[i] in ("LOUD", "NORMAL")
def f_btc_trend(ctx, i, p):
    bm = ctx.benchmark
    if bm is None: return False
    on = ctx.get("bm_on", lambda: {t: (e is not None and c > e) for t, c, e in zip(bm.t, bm.c, ind.ema(bm.c, 200))})
    return bool(on.get(ctx.b.t[i]))

FILTERS = {"none": f_none, "relvol": f_relvol, "adx": f_adx, "trend200": f_trend200, "vol_band": f_vol_band,
           "session": f_session, "vol_gate": f_vol_gate, "btc": f_vol_gate, "trend_confirm": f_btc_trend,
           "liquidity_gate": f_relvol, "stocks_in_play": f_none}

EXITS = {"atr_bracket", "time", "trail", "eod_or_atr_stop", "wide_stop", "long_only", "time_stop"}


# ---------------- strategy spec ----------------
NEW_ID_MAP = {   # New (to build) ids -> (generator, filter, exit, default params)
    "vwap_reversion.vol_band.time": ("vwap_reversion", "vol_band", "time", {"max_bars": 20, "stop_atr": 2.0}),
    "vwap_reversion.none.atr_bracket": ("vwap_reversion", "none", "atr_bracket", {"stop_atr": 2.0, "target_r": 2.0}),
    "vwap_trend_pullback.adx.atr_bracket": ("vwap_trend_pullback", "adx", "atr_bracket", {"stop_atr": 2.0, "target_r": 2.0}),
    "vwap_trend_pullback.trend200.trail": ("vwap_trend_pullback", "trend200", "trail", {"trail_atr": 3.0}),
    "orb.stocks_in_play.eod_or_atr_stop": ("orb_stocks_in_play", "none", "time", {"max_bars": 78, "stop_atr": 1.0}),
    "vol_gate_swing.btc.long_only": ("vol_gate_swing", "vol_gate", "atr_bracket",
                                     {"stop_atr": 4.0, "target_r": 2.0, "max_bars": 96, "swing": True}),
    "crypto_session_orb.vol_gate.wide_stop": ("crypto_session_orb", "vol_gate", "atr_bracket", {"stop_atr": 4.0, "target_r": 2.0}),
    "btc_led_alt_trend.trend_confirm.trail": ("btc_led_alt_trend", "trend_confirm", "trail", {"trail_atr": 3.0}),
    "attention_momentum.liquidity_gate.time_stop": ("attention_momentum", "liquidity_gate", "time", {"max_bars": 48, "stop_atr": 4.0}),
}
OVERLAYS = {"vol_gate.overlay", "maker_limit_entry.overlay"}


def parse_settings(s: str) -> dict:
    out = {}
    for part in (s or "").split(","):
        if "=" not in part: continue
        k, v = part.split("=", 1)
        try: out[k.strip()] = float(v) if "." in v else int(v)
        except ValueError: out[k.strip()] = v.strip()
    return out


def spec(strategy_id: str, settings: str | dict | None = None) -> dict:
    """Turn 'rsi2_reversion.trend200.time' (+ its Settings cell) into a runnable spec."""
    if isinstance(strategy_id, dict): return strategy_id            # already a spec
    if strategy_id in NEW_ID_MAP:
        g, f, e, p = NEW_ID_MAP[strategy_id]; p = dict(p)
    else:
        parts = strategy_id.split(".")
        if len(parts) != 3: raise ValueError(f"bad strategy id {strategy_id}")
        g, f, e = parts; p = {}
    if isinstance(settings, dict): p.update(settings)
    elif settings: p.update(parse_settings(settings))
    if g not in GENERATORS: raise ValueError(f"unknown generator {g}")
    if f not in FILTERS: raise ValueError(f"unknown filter {f}")
    if e == "time" and "max_bars" not in p: p["max_bars"] = 20
    p.setdefault("stop_atr", p.get("trail_atr", 2.0) if e == "trail" else 2.0)
    if e == "atr_bracket": p.setdefault("target_r", 2.0)
    return {"id": strategy_id, "generator": g, "filter": f, "exit": e, "params": p}


def entry_signals(sp: dict, ctx: Ctx) -> list:
    """1 where a NEW long signal appears on closed bar i AND the filter allows it."""
    raw = GENERATORS[sp["generator"]](ctx, sp["params"])
    flt = FILTERS[sp["filter"]]
    return [1 if s and flt(ctx, i, sp["params"]) else 0 for i, s in enumerate(raw)]


def exit_signals(sp: dict, ctx: Ctx):
    fn = SIGNAL_EXITS.get(sp["generator"])
    return fn(ctx, sp["params"]) if fn else None
