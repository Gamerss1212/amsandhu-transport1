"""make_golden.py - build golden fixtures from the Python reference engine (../jarvis-trading) so the JavaScript
port can be tested number-for-number (differential testing).

    python tools/make_golden.py            -> test/fixtures/golden.json.gz

Inputs are fixed (bars are stored in the fixture), so the JS side never has to reproduce Python's RNG.
"""
from __future__ import annotations
import gzip, json, math, os, random, sys, time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, "..", "..", "jarvis-trading")
sys.path.insert(0, PY)
import engine  # noqa: E402  (adds kit/ to sys.path)
from engine import indicators as ind, strategies as st, backtest as bt, volatility as vol, validation as val, library  # noqa: E402
from engine.data_feed import demo_bars, Bars, MT, ET  # noqa: E402
import policy, quality_gate  # noqa: E402

NOW = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc).timestamp()   # fixed clock for every dataset


def r(x):
    if x is None: return None
    if isinstance(x, bool): return x
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x): return None
        return float(f"{x:.12g}")
    return x


def arr(xs): return [r(x) for x in xs]


def bars_json(b: Bars): return {"symbol": b.symbol, "bar": b.bar, "t": b.t, "o": arr(b.o), "h": arr(b.h), "l": arr(b.l), "c": arr(b.c), "v": arr(b.v)}


def from_json(d):
    return Bars(d["symbol"], d["bar"], d["t"], d["o"], d["h"], d["l"], d["c"], d["v"], "golden", NOW, "")


def main():
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "now": NOW, "datasets": {}}
    raw = {
        "btc_1h": demo_bars("BTC-USD", "1h", 1400, now=NOW),
        "jpm_1h": demo_bars("JPM", "1h", 900, now=NOW),
        "aapl_5m": demo_bars("AAPL", "5m", 1200, now=NOW),
        "eth_5m": demo_bars("ETH-USD", "5m", 1200, now=NOW),
        "doge_1h": demo_bars("DOGE-USD", "1h", 900, now=NOW),
    }
    # round-trip through the rounded JSON so Python and JS compute on identical inputs
    data = {k: from_json(bars_json(b)) for k, b in raw.items()}
    for k, b in data.items(): out["datasets"][k] = bars_json(b)

    # ---------- indicators ----------
    b = data["btc_1h"]
    a, pdi, mdi = ind.adx(b.h, b.l, b.c, 14)
    bm, bu, bl = ind.bollinger(b.c, 20, 2.0)
    km, ku, kl = ind.keltner(b.h, b.l, b.c, 20, 2.0)
    sess = [st.session_key(t, "crypto") for t in b.t]
    vw, vdev = ind.session_vwap(b.h, b.l, b.c, b.v, sess)
    out["indicators"] = {"dataset": "btc_1h", "sma20": arr(ind.sma(b.c, 20)), "ema20": arr(ind.ema(b.c, 20)),
        "atr14": arr(ind.atr(b.h, b.l, b.c, 14)), "rsi2": arr(ind.rsi(b.c, 2)), "rsi14": arr(ind.rsi(b.c, 14)),
        "adx14": arr(a), "pdi14": arr(pdi), "mdi14": arr(mdi), "bb_mid": arr(bm), "bb_up": arr(bu), "bb_lo": arr(bl),
        "kc_mid": arr(km), "kc_up": arr(ku), "kc_lo": arr(kl), "z50": arr(ind.zscore(b.c, 50)), "ibs": arr(ind.ibs(b.h, b.l, b.c)),
        "relvol20": arr(ind.relvol(b.v, 20)), "crsi": arr(ind.connors_rsi(b.c)), "vwap": arr(vw), "vwap_dev": arr(vdev),
        "vr": arr(ind.variance_ratio(b.c, 4, 100)), "rmax20s1": arr(ind.rolling_max(b.h, 20, 1)), "rmin20s1": arr(ind.rolling_min(b.l, 20, 1)),
        "pctrank": arr(ind.pct_rank_window(ind.atr(b.h, b.l, b.c, 14), 200)), "streak": arr(ind.streak(b.c)), "session": sess}

    # ---------- strategies: signals + backtests ----------
    lib = library.load()
    ids = [(d["id"], d.get("settings")) for d in lib["keep"]] + [(d["id"], None) for d in lib["new"] if d.get("runnable")]
    bench = data["btc_1h"]
    sig_out, bt_out = {}, {}
    t0 = time.time()
    for dk in ("btc_1h", "jpm_1h", "aapl_5m", "eth_5m", "doge_1h"):
        bars = data[dk]
        market = "meme" if dk == "doge_1h" else None
        for sid, settings in ids:
            sp = st.spec(sid, settings)
            vstates = vol.gate(bars)["states"]
            ctx = st.Ctx(bars, "crypto" if market == "meme" else (market or st.market_kind(bars.symbol)), bench if bars.symbol != "BTC-USD" else None, vstates)
            sig = st.entry_signals(sp, ctx)
            xs = st.exit_signals(sp, ctx)
            sig_out[f"{dk}|{sid}"] = {"entry": [i for i, s in enumerate(sig) if s], "exit": None if xs is None else [i for i, s in enumerate(xs) if s]}
            res = bt.run(bars, sid, settings, market=market, benchmark=bench if bars.symbol != "BTC-USD" else None, keep_equity=False)
            bt_out[f"{dk}|{sid}"] = {"stats": {k: r(v) for k, v in res["stats"].items()}, "skipped": res["skipped"], "signals": res["signals"],
                                    "stop_atr_used": r(res["stop_atr_used"]),
                                    "trades": [{k: r(v) for k, v in t.items() if k in ("entry_t", "exit_t", "entry", "exit", "stop", "target", "r_gross", "cost_r", "r_net", "reason", "bars", "size_mult", "vol_state", "date")} for t in res["trades"]]}
    # extra backtest variants: no vol gate, maker execution, other venue, rules off, swing on
    extra = {}
    for name, kw in {"novol": {"vol_gate": False}, "maker": {"execution": "maker"}, "kraken": {"venue": "kraken_taker"},
                     "norules": {"rules": False}, "swing": {"swing": True}}.items():
        for sid in ("rsi2_reversion.trend200.time", "donchian_trend.adx.trail", "vol_breakout.adx.atr_bracket", "vol_gate_swing.btc.long_only"):
            res = bt.run(data["btc_1h"], sid, library.settings_for(sid), keep_equity=False, **kw)
            extra[f"{name}|{sid}"] = {"stats": {k: r(v) for k, v in res["stats"].items()}, "skipped": res["skipped"],
                                      "trades": [{k: r(v) for k, v in t.items() if k in ("entry_t", "exit_t", "entry", "exit", "r_net", "reason", "size_mult")} for t in res["trades"]]}
    out["signals"], out["backtests"], out["backtest_variants"] = sig_out, bt_out, extra
    out["strategy_ids"] = [sid for sid, _ in ids]
    print(f"strategies x datasets: {len(bt_out)} in {time.time() - t0:.1f}s")

    # ---------- volatility gate ----------
    out["volatility"] = {}
    for dk in ("btc_1h", "jpm_1h"):
        g = vol.walk_forward(data[dk])
        out["volatility"][dk] = {"states": g["states"], "source": g["source"], "p_loud": arr(g["p_loud"]), "p_quiet": arr(g["p_quiet"]),
                                 "auc_loud": r(g["auc_loud"]), "auc_quiet": r(g["auc_quiet"]), "confident_acc_loud": r(g["confident_acc_loud"]),
                                 "confident_n_loud": g["confident_n_loud"], "oos_samples": g["oos_samples"]}

    # ---------- validation ----------
    rng = random.Random(42)
    good = [[0.5 + rng.gauss(0, 1) if k == 0 else rng.gauss(0, 1) for k in range(5)] for _ in range(48)]
    noise = [[rng.gauss(0, 1) for _ in range(6)] for _ in range(40)]
    out["pbo"] = {"good": {"matrix": good, "pbo": val.pbo_cscv(good)}, "noise": {"matrix": noise, "pbo": val.pbo_cscv(noise)}}
    gate_cases = []
    for case in range(8):
        rr = random.Random(100 + case)
        n = [5, 25, 40, 60, 80, 120, 35, 200][case]
        mu = [0.0, 0.3, 0.05, 0.4, -0.1, 0.25, 0.6, 0.1][case]
        trades = [{"date": (datetime(2025, 1, 1) + timedelta(days=rr.randrange(0, 400))).date().isoformat(),
                   "r_net": round(rr.gauss(mu, 1.0), 6), "cost_r": round(abs(rr.gauss(0.12, 0.03)), 6)} for _ in range(n)]
        kw = [{}, {"pbo": 0.1, "beats_buy_hold": True, "placebo_pct": 97.0}, {"pbo": 0.5, "beats_buy_hold": False, "placebo_pct": 50.0},
              {"pbo": 0.1, "beats_buy_hold": True, "placebo_pct": 99.0}, {}, {"pbo": 0.15, "beats_buy_hold": True, "placebo_pct": 96.0},
              {"pbo": 0.05, "beats_buy_hold": True, "placebo_pct": 99.5}, {"pbo": 0.3, "beats_buy_hold": None, "placebo_pct": None}][case]
        n_trials = [22, 22, 45, 22, 22, 100, 1, 22][case]
        res = quality_gate.run_gate(trades, n_trials, **kw)
        gate_cases.append({"trades": trades, "n_trials": n_trials, "kw": kw, "verdict": res.verdict,
                           "checks": [{"name": c.name, "value": r(c.value) if not isinstance(c.value, bool) else c.value, "passed": c.passed} for c in res.checks],
                           "dsr": r(quality_gate.deflated_sharpe([t["r_net"] for t in trades], n_trials)) if n >= 3 else None,
                           "sign_flip_p": r(quality_gate.sign_flip_p([t["r_net"] for t in trades])) if n >= 3 else None})
    out["gate_cases"] = gate_cases
    out["expected_max_sr"] = [[n, v, r(quality_gate.expected_max_sr(n, v))] for n in (1, 2, 10, 22, 100) for v in (0.01, 0.05, 0.2)]

    # ---------- policy.decide grid ----------
    cfg = policy.Config(state_dir=os.path.join(HERE, "_tmp_policy"))
    from engine.data_feed import NYSE_HOLIDAYS, NYSE_EARLY_CLOSE
    cfg.holidays = set(NYSE_HOLIDAYS); cfg.early_close_dates = set(NYSE_EARLY_CLOSE)
    grid = []
    rr = random.Random(7)
    base_times = [datetime(2026, 10, 14, h, m, tzinfo=MT) for h, m in [(6, 0), (7, 40), (8, 0), (10, 0), (12, 25), (12, 40), (13, 45), (18, 59), (19, 5), (23, 0)]] + \
                 [datetime(2026, 10, 17, 9, 0, tzinfo=MT), datetime(2026, 11, 26, 9, 0, tzinfo=MT), datetime(2026, 11, 27, 9, 0, tzinfo=MT)]
    for k in range(400):
        mk = rr.choice(["stock", "crypto", "meme", "fx"])
        now = rr.choice(base_times)
        intent = policy.Intent(bot=rr.choice(["A073", "A001", "A041", "A200"]), symbol="X", market=mk, now=now, bar=rr.choice(["1h", "15m", "5m"]),
                               gate_verdict=rr.choice(["PAPER TEST", "PAPER TEST", "REJECT", "RESEARCH FURTHER"]), role_passed=rr.random() < 0.8,
                               signal_long_new=rr.random() < 0.8, vol_state=rr.choice(["LOUD", "NORMAL", "NORMAL", "QUIET", "UNKNOWN"]),
                               stop_atr=rr.choice([1.0, 2.0, 3.0, 4.0, 5.0]), stop_pct=rr.choice([0.0, 0.69, 1.0, 1.39, 2.08, 2.77, 4.0]),
                               cost_roundtrip_pct=rr.choice([0.06, 0.1, 0.32, 0.4, 0.52]), data_age_s=rr.choice([30, 600, 901, 5000]),
                               sector=rr.choice(["", "banks", "tech"]), market_ok=rr.random() < 0.9, event_blackout=rr.random() < 0.1,
                               earnings_calendar_supplied=rr.random() < 0.7, built_for_dead_hours=rr.random() < 0.1,
                               swing_mode=rr.random() < 0.15, swing_approved=rr.random() < 0.5)
        state = policy.State(open_stock=rr.choice([0, 1, 3]), open_crypto=rr.choice([0, 1, 2]), open_sectors={rr.choice(["banks", "energy"])},
                             trades_today=rr.choice([0, 2, 5]), day_pnl_pct=rr.choice([0.0, -1.0, -3.0]), week_pnl_pct=rr.choice([0.0, -2.0, -6.5]))
        d = policy.decide(intent, state, cfg)
        grid.append({"intent": {k2: (v.isoformat() if isinstance(v, datetime) else v) for k2, v in intent.__dict__.items()},
                     "state": {k2: (sorted(v) if isinstance(v, set) else v) for k2, v in state.__dict__.items()},
                     "verdict": d.verdict, "reason": d.reasons[0], "size_mult": r(d.size_mult), "cost_r": r(d.cost_r),
                     "flat_by": d.flat_by.isoformat() if d.flat_by else None, "bullets": d.bullets()})
    out["policy_grid"] = grid
    # auto-pause rules
    rows = []
    for k in range(60):
        rows.append({"bot_id": f"A{k:03d}", "error": rr.choice(["", "", "", "boom"]), "heartbeat_age_s": rr.choice([0, 100, 601, 900]),
                     "data_age_s": rr.choice([0, 300, 901]), "position_unknown": rr.random() < 0.05, "slippage_vs_model": rr.choice([1.0, 2.5]),
                     "backtest_max_dd_pct": rr.choice([0, -4.0, -10.0]), "max_drawdown_pct": rr.choice([0, -5.0, -7.0, -16.0]),
                     "consecutive_losses": rr.choice([0, 2, 3]), "trades_count": rr.choice([10, 50, 80]), "sharpe_last50": rr.choice([0.2, -0.1]),
                     "cost_r_avg": rr.choice([0.1, 0.25, 0.4])})
    fleets = [{"day_pnl_pct": 0, "week_pnl_pct": 0}, {"day_pnl_pct": -3.0, "week_pnl_pct": -6.0, "crypto_meme_risk_share": 0.5, "meme_capital_pct": 6.0}]
    out["auto_pause"] = [{"bots": rows, "fleet": f, "actions": [a.__dict__ for a in policy.auto_pause_actions(rows, f, cfg)]} for f in fleets]
    path = os.path.join(HERE, "..", "test", "fixtures", "golden.json.gz")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f: json.dump(out, f, separators=(",", ":"))
    print("wrote", path, os.path.getsize(path) // 1024, "KB")


if __name__ == "__main__":
    main()
