"""indicators.py - plain-Python indicators. Every function returns a list the same length as its input,
with None during warm-up. Values at index i only use data up to and including bar i (no look-ahead)."""
from __future__ import annotations
import math
from typing import Optional

Num = Optional[float]


def sma(x: list, n: int) -> list:
    out, s, q = [None] * len(x), 0.0, []
    for i, v in enumerate(x):
        if v is None:
            q, s = [], 0.0
            continue
        q.append(v); s += v
        if len(q) > n: s -= q.pop(0)
        if len(q) == n: out[i] = s / n
    return out


def ema(x: list, n: int) -> list:
    out, k, prev, seed = [None] * len(x), 2.0 / (n + 1), None, []
    for i, v in enumerate(x):
        if v is None: continue
        if prev is None:
            seed.append(v)
            if len(seed) == n: prev = sum(seed) / n; out[i] = prev
            continue
        prev = v * k + prev * (1 - k); out[i] = prev
    return out


def rolling_std(x: list, n: int) -> list:
    out = [None] * len(x)
    for i in range(n - 1, len(x)):
        w = x[i - n + 1:i + 1]
        if any(v is None for v in w): continue
        m = sum(w) / n
        out[i] = math.sqrt(sum((v - m) ** 2 for v in w) / n)
    return out


def rolling_max(x: list, n: int, shift: int = 0) -> list:
    """Highest value of the n bars ending `shift` bars ago (shift=1 -> previous n bars, excluding bar i)."""
    out = [None] * len(x)
    for i in range(n - 1 + shift, len(x)):
        w = x[i - shift - n + 1:i - shift + 1]
        out[i] = max(w)
    return out


def rolling_min(x: list, n: int, shift: int = 0) -> list:
    out = [None] * len(x)
    for i in range(n - 1 + shift, len(x)):
        w = x[i - shift - n + 1:i - shift + 1]
        out[i] = min(w)
    return out


def true_range(h: list, l: list, c: list) -> list:
    return [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]


def wilder(x: list, n: int) -> list:
    out, prev = [None] * len(x), None
    for i, v in enumerate(x):
        if v is None: continue
        if prev is None:
            if i >= n - 1 and all(z is not None for z in x[i - n + 1:i + 1]):
                prev = sum(x[i - n + 1:i + 1]) / n; out[i] = prev
            continue
        prev = (prev * (n - 1) + v) / n; out[i] = prev
    return out


def atr(h: list, l: list, c: list, n: int = 14) -> list:
    return wilder(true_range(h, l, c), n)


def rsi(c: list, n: int = 14) -> list:
    if len(c) < 2: return [None] * len(c)
    up = [None] + [max(c[i] - c[i - 1], 0.0) for i in range(1, len(c))]
    dn = [None] + [max(c[i - 1] - c[i], 0.0) for i in range(1, len(c))]
    au, ad = wilder(up, n), wilder(dn, n)
    out = [None] * len(c)
    for i in range(len(c)):
        if au[i] is None or ad[i] is None: continue
        out[i] = 100.0 if ad[i] == 0 else 100 - 100 / (1 + au[i] / ad[i])
    return out


def adx(h: list, l: list, c: list, n: int = 14):
    """Returns (adx, plus_di, minus_di)."""
    L = len(c)
    pdm, mdm = [None] * L, [None] * L
    for i in range(1, L):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
    tr = [None] + true_range(h, l, c)[1:]
    atr_, p, m = wilder(tr, n), wilder(pdm, n), wilder(mdm, n)
    pdi, mdi, dx = [None] * L, [None] * L, [None] * L
    for i in range(L):
        if atr_[i] and p[i] is not None and m[i] is not None:
            pdi[i] = 100 * p[i] / atr_[i]; mdi[i] = 100 * m[i] / atr_[i]
            s = pdi[i] + mdi[i]
            dx[i] = 0.0 if s == 0 else 100 * abs(pdi[i] - mdi[i]) / s
    return wilder(dx, n), pdi, mdi


def bollinger(c: list, n: int = 20, k: float = 2.0):
    mid, sd = sma(c, n), rolling_std(c, n)
    up = [None if m is None or s is None else m + k * s for m, s in zip(mid, sd)]
    lo = [None if m is None or s is None else m - k * s for m, s in zip(mid, sd)]
    return mid, up, lo


def keltner(h: list, l: list, c: list, n: int = 20, m: float = 2.0):
    mid, a = ema(c, n), atr(h, l, c, n)
    up = [None if x is None or y is None else x + m * y for x, y in zip(mid, a)]
    lo = [None if x is None or y is None else x - m * y for x, y in zip(mid, a)]
    return mid, up, lo


def zscore(c: list, n: int) -> list:
    m, s = sma(c, n), rolling_std(c, n)
    return [None if a is None or b in (None, 0) else (v - a) / b for v, a, b in zip(c, m, s)]


def ibs(h: list, l: list, c: list) -> list:
    return [None if hi == lo else (cl - lo) / (hi - lo) for hi, lo, cl in zip(h, l, c)]


def relvol(v: list, n: int = 20) -> list:
    """Volume of bar i divided by the average of the previous n bars."""
    avg = sma(v, n)
    return [None] + [None if avg[i - 1] in (None, 0) else v[i] / avg[i - 1] for i in range(1, len(v))]


def streak(c: list) -> list:
    out, s = [0.0] * len(c), 0
    for i in range(1, len(c)):
        if c[i] > c[i - 1]: s = s + 1 if s > 0 else 1
        elif c[i] < c[i - 1]: s = s - 1 if s < 0 else -1
        else: s = 0
        out[i] = float(s)
    return out


def percent_rank(x: list, n: int) -> list:
    out = [None] * len(x)
    for i in range(n, len(x)):
        w = x[i - n:i]
        if x[i] is None or any(v is None for v in w): continue
        out[i] = 100.0 * sum(1 for v in w if v < x[i]) / n
    return out


def connors_rsi(c: list, rsi_n: int = 3, streak_n: int = 2, rank_n: int = 100) -> list:
    r1, r2 = rsi(c, rsi_n), rsi(streak(c), streak_n)
    roc = [None] + [(c[i] / c[i - 1] - 1) if c[i - 1] else 0.0 for i in range(1, len(c))]
    pr = percent_rank(roc, rank_n)
    return [None if a is None or b is None or p is None else (a + b + p) / 3 for a, b, p in zip(r1, r2, pr)]


def session_vwap(h: list, l: list, c: list, v: list, session: list):
    """VWAP anchored at each new session key, plus the running std of (typical price - vwap)."""
    out, dev = [None] * len(c), [None] * len(c)
    pv = vv = sq = 0.0; key = object()
    for i in range(len(c)):
        if session[i] != key:
            key, pv, vv, sq = session[i], 0.0, 0.0, 0.0
        tp = (h[i] + l[i] + c[i]) / 3; w = v[i] if v[i] > 0 else 1e-9
        pv += tp * w; vv += w
        out[i] = pv / vv
        sq += w * (tp - out[i]) ** 2
        dev[i] = math.sqrt(sq / vv)
    return out, dev


def variance_ratio(c: list, q: int = 4, n: int = 100) -> list:
    """Lo-MacKinlay variance ratio of log returns over a rolling window (>1 trending, <1 mean reverting)."""
    lr = [None] + [math.log(c[i] / c[i - 1]) if c[i - 1] > 0 and c[i] > 0 else 0.0 for i in range(1, len(c))]
    out = [None] * len(c)
    for i in range(n + q, len(c)):
        w = lr[i - n + 1:i + 1]
        m = sum(w) / n
        v1 = sum((x - m) ** 2 for x in w) / n
        if v1 == 0: continue
        qs = [sum(lr[j - q + 1:j + 1]) for j in range(i - n + 1, i + 1)]
        vq = sum((x - q * m) ** 2 for x in qs) / n
        out[i] = vq / (q * v1)
    return out


def pct_rank_window(x: list, n: int) -> list:
    """Percentile (0..1) of x[i] inside the window of the previous n values."""
    out = [None] * len(x)
    for i in range(n, len(x)):
        w = [v for v in x[i - n:i] if v is not None]
        if x[i] is None or len(w) < n // 2: continue
        out[i] = sum(1 for v in w if v < x[i]) / len(w)
    return out
