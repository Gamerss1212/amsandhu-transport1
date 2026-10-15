"""volatility.py - the volatility gate: will the next 12 bars be LOUD, NORMAL or QUIET?

It forecasts HOW MUCH price will move, never which way. (Owner's BTC study: volatility AUC 0.72-0.75,
direction AUC 0.513.) LOUD = wider stop and 0.6x size. QUIET = no new trades.

Model: two small logistic regressions (LOUD vs rest, QUIET vs rest) on causal features, trained walk-forward
with a purge gap, so every reported AUC is out-of-sample. Before the first walk-forward fold has data, a plain
rule (ATR vs its 30-day average) is used and labelled as such.
"""
from __future__ import annotations
import math
from . import indicators as ind
from .data_feed import BAR_SECONDS, Bars, MT
from datetime import datetime

H = 12                     # forecast horizon in bars


def _features(b: Bars) -> list:
    n = len(b)
    a14 = ind.atr(b.h, b.l, b.c, 14)
    a_avg = ind.sma(a14, min(720, max(50, n // 3)))
    _, bu, bl = ind.bollinger(b.c, 20, 2.0)
    bw = [None if u is None else (u - l) / c for u, l, c in zip(bu, bl, b.c)]
    bw_avg = ind.sma(bw, 168)
    lr = [0.0] + [math.log(b.c[i] / b.c[i - 1]) for i in range(1, n)]
    rv24 = ind.rolling_std(lr, 24); rv168 = ind.rolling_std(lr, 168)
    v6 = ind.sma(b.v, 6); v48 = ind.sma(b.v, 48)
    sec = BAR_SECONDS[b.bar]
    out = []
    for i in range(n):
        vals = (a14[i], a_avg[i], bw[i], bw_avg[i], rv24[i], rv168[i], v6[i], v48[i])
        if any(x is None or x <= 0 for x in vals) or i < 12:
            out.append(None); continue
        lt = datetime.fromtimestamp(b.t[i] + sec, MT)
        hr = 2 * math.pi * (lt.hour + lt.minute / 60) / 24
        mv = abs(b.c[i] - b.c[i - 12]) / a14[i]
        out.append([math.log(a14[i] / a_avg[i]), math.log(bw[i] / bw_avg[i]), math.log(rv24[i] / rv168[i]),
                    math.log(v6[i] / v48[i]), math.sin(hr), math.cos(hr), 1.0 if lt.weekday() >= 5 else 0.0, mv])
    return out


def _future_range(b: Bars) -> list:
    n, out = len(b), []
    for i in range(n):
        if i + H >= n: out.append(None); continue
        out.append((max(b.h[i + 1:i + H + 1]) - min(b.l[i + 1:i + H + 1])) / b.c[i])
    return out


def _solve(A, y):
    n = len(y); M = [row[:] + [y[k]] for k, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12: return [0.0] * n
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1): M[r][k] -= f * M[c][k]
    return [M[k][n] / M[k][k] for k in range(n)]


class Logit:
    """Ridge-penalised logistic regression fit by Newton's method (small, exact, no numpy)."""
    def fit(self, X, y, l2=1.0, iters=12):
        d = len(X[0])
        self.mu = [sum(r[j] for r in X) / len(X) for j in range(d)]
        self.sd = [math.sqrt(sum((r[j] - self.mu[j]) ** 2 for r in X) / len(X)) or 1.0 for j in range(d)]
        Z = [[1.0] + [(r[j] - self.mu[j]) / self.sd[j] for j in range(d)] for r in X]
        w = [0.0] * (d + 1)
        for _ in range(iters):
            g = [0.0] * (d + 1); Hm = [[0.0] * (d + 1) for _ in range(d + 1)]
            for z, t in zip(Z, y):
                s = sum(a * b for a, b in zip(w, z)); p = 1 / (1 + math.exp(-max(-30, min(30, s))))
                e, q = p - t, p * (1 - p)
                for a in range(d + 1):
                    g[a] += e * z[a]; za = q * z[a]; row = Hm[a]
                    for c in range(a, d + 1): row[c] += za * z[c]
            for a in range(d + 1):
                for c in range(a): Hm[a][c] = Hm[c][a]
                if a: g[a] += l2 * w[a]; Hm[a][a] += l2
            step = _solve(Hm, g)
            w = [a - b for a, b in zip(w, step)]
            if max(abs(s) for s in step) < 1e-6: break
        self.w = w
        return self

    def p(self, x):
        z = [1.0] + [(v - m) / s for v, m, s in zip(x, self.mu, self.sd)]
        s = sum(a * b for a, b in zip(self.w, z))
        return 1 / (1 + math.exp(-max(-30, min(30, s))))


def auc(scores: list, labels: list):
    pairs = sorted(zip(scores, labels))
    pos = sum(labels); neg = len(labels) - pos
    if pos == 0 or neg == 0: return None
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]: j += 1
        r = (i + j + 2) / 2
        rank_sum += r * sum(1 for k in range(i, j + 1) if pairs[k][1])
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def _pct(xs, q):
    s = sorted(xs); return s[min(len(s) - 1, max(0, int(q * (len(s) - 1))))]


def rule_state(b: Bars, i: int, a14=None, a_avg=None) -> str:
    a14 = a14 or ind.atr(b.h, b.l, b.c, 14)
    a_avg = a_avg or ind.sma(a14, min(720, max(50, len(b) // 3)))
    if a14[i] is None or a_avg[i] is None: return "UNKNOWN"
    r = a14[i] / a_avg[i]
    return "LOUD" if r > 1.25 else "QUIET" if r < 0.8 else "NORMAL"


def classify(p_loud: float, p_quiet: float) -> str:
    if p_loud >= 0.5 and p_loud >= p_quiet: return "LOUD"
    if p_quiet >= 0.5: return "QUIET"
    return "NORMAL"


def walk_forward(b: Bars, min_train: int = 400, step: int = 200, max_train: int = 3000) -> dict:
    """Per-bar out-of-sample states + honest scores. Labels use terciles of the TRAIN window only."""
    n = len(b)
    X, fr = _features(b), _future_range(b)
    a14 = ind.atr(b.h, b.l, b.c, 14); a_avg = ind.sma(a14, min(720, max(50, n // 3)))
    states = [rule_state(b, i, a14, a_avg) for i in range(n)]
    source = ["rule"] * n
    oos_l, oos_q, lab_l, lab_q, p_l_all, p_q_all = [], [], [], [], [None] * n, [None] * n
    start = min_train
    model_l = model_q = None
    while start < n:
        end = min(n, start + step)
        tr = [k for k in range(max(0, start - H - max_train), start - H) if X[k] is not None and fr[k] is not None]
        if len(tr) >= 150:
            ranges = [fr[k] for k in tr]; hi, lo = _pct(ranges, 2 / 3), _pct(ranges, 1 / 3)
            tr_s = tr[::2] if len(tr) > 1500 else tr
            Xt = [X[k] for k in tr_s]
            model_l = Logit().fit(Xt, [1 if fr[k] >= hi else 0 for k in tr_s])
            model_q = Logit().fit(Xt, [1 if fr[k] <= lo else 0 for k in tr_s])
            for k in range(start, end):
                if X[k] is None: continue
                pl, pq = model_l.p(X[k]), model_q.p(X[k])
                p_l_all[k], p_q_all[k] = pl, pq
                states[k], source[k] = classify(pl, pq), "model"
                if fr[k] is not None:
                    oos_l.append(pl); lab_l.append(1 if fr[k] >= hi else 0)
                    oos_q.append(pq); lab_q.append(1 if fr[k] <= lo else 0)
        start = end
    def conf_acc(ps, ls, thr=0.5):
        hits = [(l) for p, l in zip(ps, ls) if p >= thr]
        return (round(sum(hits) / len(hits), 3), len(hits)) if hits else (None, 0)
    acc_l, n_l = conf_acc(oos_l, lab_l); acc_q, n_q = conf_acc(oos_q, lab_q)
    cur = n - 1
    return {"states": states, "source": source, "p_loud": p_l_all, "p_quiet": p_q_all,
            "auc_loud": None if not oos_l else round(auc(oos_l, lab_l) or 0, 3),
            "auc_quiet": None if not oos_q else round(auc(oos_q, lab_q) or 0, 3),
            "confident_acc_loud": acc_l, "confident_n_loud": n_l, "confident_acc_quiet": acc_q, "confident_n_quiet": n_q,
            "oos_samples": len(oos_l), "horizon_bars": H,
            "current": {"state": states[cur], "source": source[cur], "p_loud": p_l_all[cur], "p_quiet": p_q_all[cur],
                        "atr_ratio": None if not (a14[cur] and a_avg[cur]) else round(a14[cur] / a_avg[cur], 3)}}


_CACHE: dict = {}


def gate(b: Bars) -> dict:
    """Cached walk-forward result for the latest bars of a symbol."""
    key = (b.symbol, b.bar, b.t[-1] if b.t else 0, len(b))
    if key not in _CACHE:
        if len(_CACHE) > 64: _CACHE.clear()
        _CACHE[key] = walk_forward(b)
    return _CACHE[key]


def summary(b: Bars) -> dict:
    g = gate(b)
    c = g["current"]
    def r(x): return None if x is None else round(x, 3)
    return {"symbol": b.symbol, "bar": b.bar, "state": c["state"], "method": c["source"], "p_loud": r(c["p_loud"]),
            "p_quiet": r(c["p_quiet"]), "atr_ratio": c["atr_ratio"], "auc_loud_oos": g["auc_loud"],
            "auc_quiet_oos": g["auc_quiet"], "confident_acc_loud": g["confident_acc_loud"],
            "confident_n_loud": g["confident_n_loud"], "confident_acc_quiet": g["confident_acc_quiet"],
            "confident_n_quiet": g["confident_n_quiet"], "oos_samples": g["oos_samples"], "horizon_bars": H,
            "source": b.source, "note": "Forecasts size of the move, never direction."}
