"""validation.py - the extra evidence the quality gate needs before it can ever open:
PBO (probability of backtest overfitting, CSCV), the random-entry placebo, and buy-and-hold.
Then everything is handed to kit/quality_gate.run_gate. Jarvis cannot edit any verdict.

Every backtest try is counted (state/trials.json) and the count feeds the Deflated Sharpe check.
"""
from __future__ import annotations
import itertools, json, math, os, random, threading
from datetime import datetime
import quality_gate                                            # kit/
from .data_feed import MT, Bars, market_kind
from . import backtest as bt
from .strategies import Ctx, entry_signals, spec as make_spec

MIN_ROLE_TRIES = 22            # Roles sheet: "Total tries (all candidates) 22"
_lock = threading.Lock()


# ---------------- trial counter ----------------
def count_try(state_dir: str, key: str, n: int = 1) -> int:
    path = os.path.join(state_dir, "trials.json")
    with _lock:
        try: d = json.load(open(path))
        except Exception: d = {}
        d[key] = d.get(key, 0) + n
        d["_total"] = d.get("_total", 0) + n
        os.makedirs(state_dir, exist_ok=True)
        json.dump(d, open(path, "w"), indent=1)
        return d[key]


def tries(state_dir: str, key: str | None = None) -> int:
    try: d = json.load(open(os.path.join(state_dir, "trials.json")))
    except Exception: return 0
    return d.get(key or "_total", 0)


# ---------------- placebo ----------------
def placebo_percentile(bars: Bars, strategy_id, actual_mean: float, n_trades: int, draws: int = 200,
                       seed: int = 11, **kw) -> float | None:
    """Random entries (same number of trades, same exits and rules). Percentile of the real mean R among them."""
    if n_trades < 3: return None
    rng = random.Random(seed); n = len(bars); means = []
    start = kw.get("start", 0); end = kw.get("end") or n
    for _ in range(draws):
        sig = [0] * n
        for k in rng.sample(range(start, end - 1), min(end - start - 1, n_trades * 3)): sig[k] = 1
        r = bt.run(bars, strategy_id, signals=sig, keep_equity=False, **kw)
        rs = [t["r_net"] for t in r["trades"]][:n_trades]
        if rs: means.append(sum(rs) / len(rs))
    if not means: return None
    return round(100 * sum(1 for m in means if m < actual_mean) / len(means), 1)


# ---------------- PBO via CSCV (Bailey, Borwein, Lopez de Prado, Zhu) ----------------
def pbo_cscv(matrix: list, s: int = 8) -> float | None:
    """matrix[t][k] = return of variant k in period t. Returns the share of in-sample winners that land in the
    bottom half out of sample (logit <= 0). Needs >= 2 variants and >= s periods."""
    T = len(matrix)
    if T < s or not matrix or len(matrix[0]) < 2: return None
    N = len(matrix[0]); size = T // s
    blocks = [list(range(b * size, (b + 1) * size)) for b in range(s)]
    def perf(rows, k):
        xs = [matrix[t][k] for t in rows]
        m = sum(xs) / len(xs); sd = math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))
        return m / sd if sd > 0 else m
    below, total = 0, 0
    for combo in itertools.combinations(range(s), s // 2):
        ins = [t for b in combo for t in blocks[b]]
        oos = [t for b in range(s) if b not in combo for t in blocks[b]]
        pin = [perf(ins, k) for k in range(N)]
        best = max(range(N), key=lambda k: pin[k])
        pout = [perf(oos, k) for k in range(N)]
        rank = sum(1 for v in pout if v < pout[best]) + 0.5 * sum(1 for v in pout if v == pout[best]) + 0.5
        w = rank / (N + 1)
        logit = math.log(w / (1 - w))
        below += logit <= 0; total += 1
    return round(below / total, 3)


def weekly_matrix(results_by_variant: list) -> list:
    """Turn per-variant trade lists into a week x variant matrix of summed net R."""
    weeks = sorted({_week(t["date"]) for res in results_by_variant for t in res})
    idx = {w: k for k, w in enumerate(weeks)}
    M = [[0.0] * len(results_by_variant) for _ in weeks]
    for k, res in enumerate(results_by_variant):
        for t in res: M[idx[_week(t["date"])]][k] += t["r_net"]
    return M


def _week(d: str) -> str:
    y, w, _ = datetime.fromisoformat(d).isocalendar(); return f"{y}-{w:02d}"


# ---------------- single-symbol full report ----------------
def full_report(bars: Bars, strategy_id: str, state_dir: str, settings=None, benchmark: Bars | None = None,
                test_split: float = 0.6, draws: int = 120, **kw) -> dict:
    """Backtest + every-bar benchmark + buy & hold + placebo + gate on the out-of-sample part."""
    sp = make_spec(strategy_id, settings)
    n = len(bars); cut = int(n * test_split)
    full = bt.run(bars, sp, benchmark=benchmark, **kw)
    test = bt.run(bars, sp, benchmark=benchmark, start=cut, keep_equity=False, **kw)
    key = f"{sp['generator']}|{bars.symbol}"
    count_try(state_dir, key)
    n_trials = max(MIN_ROLE_TRIES, tries(state_dir))
    tt = test["trades"]
    mean = sum(t["r_net"] for t in tt) / len(tt) if tt else 0.0
    plc = placebo_percentile(bars, sp, mean, len(tt), draws=draws, benchmark=benchmark, start=cut,
                             **{k: v for k, v in kw.items() if k not in ("start", "end")})
    bh = bt.buy_and_hold(bars, cut)
    s_sh = bt.strategy_daily_sharpe(tt, full["market"])
    beats = None if (s_sh is None or bh["sharpe_daily"] is None) else s_sh > bh["sharpe_daily"]
    # PBO across the variants of the same generator (filters x exits) on this symbol
    variants = _sibling_variants(sp["generator"])
    pbo = None
    if len(variants) >= 2:
        per = [bt.run(bars, v, benchmark=benchmark, keep_equity=False, **kw)["trades"] for v in variants]
        if sum(1 for p in per if p) >= 2: pbo = pbo_cscv(weekly_matrix(per))
    gate = quality_gate.run_gate([{"date": t["date"], "r_net": t["r_net"], "cost_r": t["cost_r"]} for t in tt],
                                 n_trials=n_trials, pbo=pbo, beats_buy_hold=beats, placebo_pct=plc)
    every = bt.every_bar_benchmark(bars, sp, benchmark=benchmark, **{k: v for k, v in kw.items() if k != "signals"})
    full.update({
        "test": {"from": bars.t[cut] if cut < n else None, "stats": test["stats"], "trades": len(tt)},
        "every_bar": every, "buy_hold": bh, "strategy_sharpe_daily_test": s_sh, "placebo_pct": plc, "pbo": pbo,
        "pbo_variants": variants, "n_trials_counted": n_trials,
        "gate": {"verdict": gate.verdict, "checks": [c.__dict__ for c in gate.checks], "table": gate.table()},
    })
    return full


def _sibling_variants(generator: str) -> list:
    from . import library
    return [d["id"] for d in library.load()["keep"] if d["generator"] == generator][:6]


# ---------------- role test (pooled) ----------------
def role_test(role: str, data, state_dir: str, bar: str | None = None, max_symbols: int = 20, progress=None) -> dict:
    """The build-order step 5 test: pick ONE candidate on pooled train data, test it once on pooled test data."""
    from . import library
    r = next((x for x in library.roles() if x["role"] == role), None)
    if r is None: return {"ok": False, "error": "unknown role"}
    bots = [b for b in library.bots() if b["role"] == role and b["status"] != "PARKED"][:max_symbols]
    bar = bar or r["bar"]
    cands = [c for c in (r["candidate_1"], r["candidate_2"], r["candidate_3"]) if c]
    if r.get("extra") and "vol_gate_swing" in str(r["extra"]): cands.append("vol_gate_swing.btc.long_only")
    series, missing = {}, []
    bm = None
    for k, b in enumerate(bots):
        if progress: progress(k / max(1, len(bots)) * 0.4, f"loading {b['symbol']}")
        got = data.get(b["symbol"], bar, 3000)
        if got["ok"] and len(got["bars"]) > 300: series[b["bot_id"]] = (b, got["bars"])
        else: missing.append(b["symbol"])
    if any(library.is_meme(b) or b["symbol"].endswith("-USD") for b in bots):
        g = data.get("BTC-USD", bar, 3000); bm = g["bars"] if g["ok"] else None
    if not series: return {"ok": False, "error": "no data for this role", "missing": missing}
    train, test = {}, {}
    for ci, c in enumerate(cands):
        if progress: progress(0.4 + 0.5 * ci / len(cands), f"testing {c}")
        tr_all, te_all = [], []
        for bid, (b, bars) in series.items():
            mk = "meme" if library.is_meme(b) else market_kind(bars.symbol)
            cut = int(len(bars) * 0.6)
            settings = library.settings_for(c)
            tr_all += bt.run(bars, c, settings, market=mk, benchmark=bm, end=cut, keep_equity=False)["trades"]
            te_all += bt.run(bars, c, settings, market=mk, benchmark=bm, start=cut, keep_equity=False)["trades"]
        train[c], test[c] = tr_all, te_all
        count_try(state_dir, f"role|{role}")
    def mean(ts): return sum(t["r_net"] for t in ts) / len(ts) if ts else -9
    chosen = max(cands, key=lambda c: mean(train[c]))
    tt = test[chosen]
    pbo = pbo_cscv(weekly_matrix([train[c] + test[c] for c in cands])) if len(cands) >= 2 else None
    # pooled placebo: random entries per symbol with the same trade count share
    if progress: progress(0.92, "placebo and buy-and-hold")
    plc = _pooled_placebo(series, chosen, bm, mean(tt), len(tt)) if tt else None
    bh = [bt.buy_and_hold(bars, int(len(bars) * 0.6))["sharpe_daily"] for _, bars in series.values()]
    bh = [x for x in bh if x is not None]
    bh_sh = sum(bh) / len(bh) if bh else None
    mk0 = market_kind(next(iter(series.values()))[1].symbol)
    s_sh = bt.strategy_daily_sharpe(tt, mk0)
    beats = None if s_sh is None or bh_sh is None else s_sh > bh_sh
    n_trials = max(MIN_ROLE_TRIES, tries(state_dir))
    gate = quality_gate.run_gate([{"date": t["date"], "r_net": t["r_net"], "cost_r": t["cost_r"]} for t in tt],
                                 n_trials=n_trials, pbo=pbo, beats_buy_hold=beats, placebo_pct=plc)
    out = {"ok": True, "role": role, "bar": bar, "symbols": [b["symbol"] for b, _ in series.values()], "missing": missing,
           "candidates": {c: {"train": bt.stats(train[c]), "test": bt.stats(test[c])} for c in cands},
           "chosen": chosen, "test_stats": bt.stats(tt), "pbo": pbo, "placebo_pct": plc, "buy_hold_sharpe": bh_sh,
           "strategy_sharpe": s_sh, "n_trials_counted": n_trials,
           "gate": {"verdict": gate.verdict, "checks": [c.__dict__ for c in gate.checks], "table": gate.table()},
           "source": next(iter(series.values()))[1].source, "run_at": datetime.now(MT).isoformat(timespec="seconds")}
    save_gate_result(state_dir, role, out)
    return out


def _pooled_placebo(series, sid, bm, actual_mean, n_trades, draws=60, seed=5):
    rng = random.Random(seed); means = []
    items = list(series.values())
    per = max(1, n_trades // max(1, len(items)))
    for _ in range(draws):
        rs = []
        for b, bars in items:
            n = len(bars); cut = int(n * 0.6)
            sig = [0] * n
            for k in rng.sample(range(cut, n - 1), min(n - cut - 1, per * 3)): sig[k] = 1
            rs += [t["r_net"] for t in bt.run(bars, sid, signals=sig, benchmark=bm, start=cut, keep_equity=False)["trades"][:per]]
        if rs: means.append(sum(rs) / len(rs))
    return round(100 * sum(1 for m in means if m < actual_mean) / len(means), 1) if means else None


def save_gate_result(state_dir: str, role: str, res: dict):
    path = os.path.join(state_dir, "gate_results.json")
    with _lock:
        try: d = json.load(open(path))
        except Exception: d = {}
        d[role] = {"verdict": res["gate"]["verdict"], "chosen": res["chosen"], "run_at": res["run_at"],
                   "test_trades": res["test_stats"]["trades"], "source": res["source"]}
        json.dump(d, open(path, "w"), indent=1)


def gate_results(state_dir: str) -> dict:
    try: return json.load(open(os.path.join(state_dir, "gate_results.json")))
    except Exception: return {}
