"""tests/test_engine.py - stdlib unittest suite for the paper-only trading engine (engine/).

Run from the project folder:
    python3 -m unittest tests.test_engine -v
    python3 tests/test_engine.py

Rules these tests follow:
  - NO network. Bars come from demo_bars() (simulated, labelled DEMO), DataService(mode='demo') or are built
    by hand. A module-level guard makes any accidental HTTP call raise.
  - All state goes to temporary folders. Nothing is written to the project's state/ folder.
  - Source files are never modified. Tests that expose a real source bug are kept, marked
    @unittest.expectedFailure, and carry a 'SOURCE BUG:' comment.
"""
from __future__ import annotations
import csv, json, os, random, shutil, sys, tempfile, unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import engine                                   # noqa: E402  (adds kit/ to sys.path)
from engine import indicators as ind            # noqa: E402
from engine import data_feed as df              # noqa: E402
from engine import library, volatility, validation   # noqa: E402
from engine import backtest as bt               # noqa: E402
from engine import fleet as fleet_mod           # noqa: E402
from engine import paper_broker                 # noqa: E402
from engine.data_feed import (BAR_SECONDS, ET, MT, UTC, Bars, DataService, demo_bars, market_kind,  # noqa: E402
                              merge, nyse_session, session_key)
from engine.paper_broker import Book, LOG_FIELDS, COLLAR_PCT   # noqa: E402
from engine.strategies import (Ctx, FILTERS, GENERATORS, NEW_ID_MAP, entry_signals, exit_signals,  # noqa: E402
                               parse_settings, spec)
import policy                                   # noqa: E402  (kit/)
import quality_gate                             # noqa: E402  (kit/)
import jarvus_reporter                          # noqa: E402  (kit/)

# A fixed "now" so every demo series is reproducible: Wednesday 2026-10-14, 20:00 UTC (2 pm MT).
NOW = datetime(2026, 10, 14, 20, 0, tzinfo=UTC).timestamp()
VERDICTS = {"REJECT", "RESEARCH FURTHER", "PAPER TEST"}


# ---------------------------------------------------------------- network guard
_PATCHES: list = []


def _no_network(*a, **k):
    raise RuntimeError("tests must not use the network")


def setUpModule():
    for target in ("urllib.request.urlopen", "engine.data_feed._get_json"):
        p = mock.patch(target, _no_network)
        p.start()
        _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


# ---------------------------------------------------------------- helpers
def make_bars(symbol: str, bar: str, t0: float, rows: list, vol: float = 1000.0) -> Bars:
    """Hand-built bars on a regular grid starting at t0. rows = [(o, h, l, c), ...]."""
    sec = BAR_SECONDS[bar]
    return make_bars_at(symbol, bar, [int(t0 + k * sec) for k in range(len(rows))], rows, vol)


def make_bars_at(symbol: str, bar: str, times: list, rows: list, vol: float = 1000.0) -> Bars:
    b = Bars(symbol, bar, source="TEST (hand-built)")
    for t, (o, h, l, c) in zip(times, rows):
        b.t.append(int(t)); b.o.append(float(o)); b.h.append(float(h)); b.l.append(float(l))
        b.c.append(float(c)); b.v.append(float(vol))
    return b


def session_times(day: date, bar: str, close_hm=(16, 0)) -> list:
    """Bar open times of one NYSE session (9:30 ET to the close)."""
    sec = BAR_SECONDS[bar]
    t = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET).timestamp()
    end = datetime(day.year, day.month, day.day, close_hm[0], close_hm[1], tzinfo=ET).timestamp()
    out = []
    while t < end:
        out.append(int(t)); t += sec
    return out


def read_json(path: str):
    with open(path) as f:
        return json.load(f)


def read_text(path: str) -> str:
    with open(path) as f:
        return f.read()


def write_json(obj, path: str):
    with open(path, "w") as f:
        json.dump(obj, f)


def tmpdir(testcase: unittest.TestCase) -> str:
    d = tempfile.mkdtemp(prefix="jarvis_test_")
    testcase.addCleanup(shutil.rmtree, d, True)
    return d


def all_strategy_ids() -> list:
    """(id, settings) for every Keep variant and every runnable New (to build) strategy."""
    lib = library.load()
    out = [(d["id"], d.get("settings") or "") for d in lib["keep"]]
    out += [(d["id"], library.settings_for(d["id"])) for d in lib["new"] if d.get("runnable")]
    return out


# ================================================================ indicators
class TestIndicators(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = demo_bars("ETH-USD", "1h", 400, now=NOW)

    def _series(self, b: Bars) -> dict:
        a, pdi, mdi = ind.adx(b.h, b.l, b.c, 14)
        mid, up, lo = ind.bollinger(b.c, 20, 2.0)
        return {"sma20": ind.sma(b.c, 20), "ema20": ind.ema(b.c, 20), "atr14": ind.atr(b.h, b.l, b.c, 14),
                "rsi14": ind.rsi(b.c, 14), "rsi2": ind.rsi(b.c, 2), "adx14": a, "plus_di": pdi, "minus_di": mdi,
                "boll_mid": mid, "boll_up": up, "boll_lo": lo, "zscore20": ind.zscore(b.c, 20),
                "relvol20": ind.relvol(b.v, 20)}

    def test_lengths_and_warmup_none(self):
        # index of the first non-None value for each indicator (warm-up before it must be None)
        first = {"sma20": 19, "ema20": 19, "atr14": 13, "rsi14": 14, "rsi2": 2, "adx14": 27, "plus_di": 14,
                 "minus_di": 14, "boll_mid": 19, "boll_up": 19, "boll_lo": 19, "zscore20": 19, "relvol20": 20}
        s = self._series(self.b)
        n = len(self.b)
        for name, xs in s.items():
            with self.subTest(indicator=name):
                self.assertEqual(len(xs), n)
                k = first[name]
                self.assertTrue(all(v is None for v in xs[:k]), f"{name} has a value during warm-up")
                self.assertIsNotNone(xs[k], f"{name} should have a value at index {k}")
                self.assertTrue(all(v is not None for v in xs[k:]), f"{name} has a gap after warm-up")

    def test_no_lookahead_prefix_unchanged(self):
        full = self._series(self.b)
        for k in (60, 151, 233, 399):
            pre = self._series(self.b.slice(0, k))
            for name in full:
                with self.subTest(indicator=name, prefix=k):
                    self.assertEqual(pre[name], full[name][:k], f"{name}: value at i < {k} changed when later bars were added")

    def test_other_helpers_no_lookahead(self):
        b = self.b
        fns = {"keltner_lo": lambda x: ind.keltner(x.h, x.l, x.c, 20, 2.0)[2],
               "ibs": lambda x: ind.ibs(x.h, x.l, x.c),
               "connors": lambda x: ind.connors_rsi(x.c),
               "rolling_max_shift1": lambda x: ind.rolling_max(x.h, 20, shift=1),
               "rolling_min_shift1": lambda x: ind.rolling_min(x.l, 20, shift=1),
               "variance_ratio": lambda x: ind.variance_ratio(x.c, 4, 100),
               "pct_rank": lambda x: ind.pct_rank_window(x.c, 50),
               "vwap": lambda x: ind.session_vwap(x.h, x.l, x.c, x.v, [session_key(t, "crypto") for t in x.t])[0]}
        for name, f in fns.items():
            full = f(b)
            self.assertEqual(len(full), len(b))
            for k in (130, 287):
                with self.subTest(fn=name, prefix=k):
                    self.assertEqual(f(b.slice(0, k)), full[:k])

    def test_rolling_max_shift_excludes_current_bar(self):
        x = [1.0, 2.0, 3.0, 10.0, 4.0]
        self.assertEqual(ind.rolling_max(x, 3, shift=1), [None, None, None, 3.0, 10.0])
        self.assertEqual(ind.rolling_max(x, 3), [None, None, 3.0, 10.0, 10.0])

    def test_known_values(self):
        self.assertEqual(ind.sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])
        self.assertEqual(ind.ema([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])
        up = [float(i) for i in range(1, 40)]
        self.assertEqual(ind.rsi(up, 14)[-1], 100.0)
        self.assertAlmostEqual(ind.rsi(up[::-1], 14)[-1], 0.0)
        h = [101.0] * 30; l = [99.0] * 30; c = [100.0] * 30
        self.assertAlmostEqual(ind.atr(h, l, c, 14)[-1], 2.0)
        self.assertEqual(ind.true_range([3, 5], [1, 2], [2, 4]), [2, 3])
        rv = ind.relvol([500.0] * 30, 20)
        self.assertEqual(rv[20:], [1.0] * 10)

    def test_bollinger_ordering_and_mid_is_sma(self):
        mid, up, lo = ind.bollinger(self.b.c, 20, 2.0)
        self.assertEqual(mid, ind.sma(self.b.c, 20))
        for m, u, l in zip(mid[19:], up[19:], lo[19:]):
            self.assertLessEqual(l, m); self.assertLessEqual(m, u)

    def test_zscore_flat_series_is_none(self):
        self.assertTrue(all(v is None for v in ind.zscore([5.0] * 30, 10)))

    def test_empty_and_short_inputs(self):
        self.assertEqual(ind.sma([], 5), [])
        self.assertEqual(ind.rsi([1.0], 14), [None])
        self.assertEqual(ind.ema([1.0, 2.0], 5), [None, None])


# ================================================================ data feed
class TestDataFeed(unittest.TestCase):
    def test_demo_bars_labelled_demo(self):
        for sym in ("BTC-USD", "AAPL"):
            b = demo_bars(sym, "1h", 300, now=NOW)
            self.assertEqual(len(b), 300)
            self.assertTrue(b.source.startswith("DEMO"), b.source)
            self.assertIn("NOT real", b.note)
            j = b.to_json(last=10)
            self.assertTrue(j["source"].startswith("DEMO"))
            self.assertEqual(len(j["c"]), 10)

    def test_demo_bars_closed_only_sorted_and_sane(self):
        for sym, bar in (("BTC-USD", "1h"), ("ETH-USD", "5m"), ("AAPL", "1h"), ("AAPL", "5m"), ("AAPL", "15m")):
            with self.subTest(symbol=sym, bar=bar):
                b = demo_bars(sym, bar, 400, now=NOW)
                sec = BAR_SECONDS[bar]
                self.assertTrue(all(t + sec <= NOW for t in b.t), "a still-forming bar was returned")
                self.assertLessEqual(b.last_close_time(), NOW)
                self.assertEqual(b.t, sorted(set(b.t)))
                for o, h, l, c in zip(b.o, b.h, b.l, b.c):
                    self.assertGreater(l, 0)
                    self.assertLessEqual(l, min(o, c)); self.assertGreaterEqual(h, max(o, c))
                if market_kind(sym) == "crypto":
                    self.assertTrue(all(b.t[k + 1] - b.t[k] == sec for k in range(len(b) - 1)))

    def test_demo_bars_deterministic(self):
        a = demo_bars("SOL-USD", "1h", 200, now=NOW); b = demo_bars("SOL-USD", "1h", 200, now=NOW)
        self.assertEqual((a.t, a.c), (b.t, b.c))

    def test_demo_stock_bars_only_in_session(self):
        now = datetime(2026, 7, 10, 18, 0, tzinfo=UTC).timestamp()
        b = demo_bars("AAPL", "1h", 300, now=now)
        days = set()
        for t in b.t:
            et = datetime.fromtimestamp(t, ET)
            days.add(et.date())
            self.assertLess(et.weekday(), 5)
            self.assertNotIn(et.date(), df.NYSE_HOLIDAYS)
            self.assertTrue((9, 30) <= (et.hour, et.minute) < (16, 0))
        self.assertNotIn(date(2026, 7, 3), days)           # Independence Day (observed) holiday
        self.assertIn(date(2026, 7, 2), days)

    def test_age_and_stale(self):
        b = Bars("BTC-USD", "1h", t=[1_000_000], o=[1.0], h=[1.0], l=[1.0], c=[1.0], v=[1.0])
        lc = 1_000_000 + 3600
        self.assertEqual(b.last_close_time(), lc)
        self.assertEqual(b.age_s(now=lc + 120), 120)
        self.assertFalse(b.stale(now=lc + 3600 + 900))         # bar size + 15 min = still fresh
        self.assertTrue(b.stale(now=lc + 3600 + 901))
        empty = Bars("BTC-USD", "1h")
        self.assertIsNone(empty.last_close_time()); self.assertIsNone(empty.age_s(now=lc))
        self.assertTrue(empty.stale(now=lc))
        d = demo_bars("BTC-USD", "1h", 50, now=NOW)
        self.assertFalse(d.stale(now=NOW)); self.assertTrue(d.stale(now=NOW + 3 * 3600))

    def test_sane_drops_bad_bars_and_never_fixes_prices(self):
        rows = [(100, 10, 11, 9, 10, 5),      # good
                (200, 10, 9, 11, 10, 5),      # high < low
                (300, 0, 11, 9, 10, 5),       # zero open
                (400, 10, 11, 9, 12, 5),      # close above high
                (500, 8, 11, 9, 10, 5),       # open below low
                (600, None, 11, 9, 10, 5),    # missing value
                (700, 10, 11, -1, 10, 5),     # negative low
                (100, 10, 11, 9, 10.5, 5),    # duplicate time: first one kept
                (50, 10, 11, 9, 10, None)]    # good, missing volume -> 0
        out = df._sane(rows)
        self.assertEqual(out, [(50, 10.0, 11.0, 9.0, 10.0, 0.0), (100, 10.0, 11.0, 9.0, 10.0, 5.0)])

    def test_closed_only(self):
        rows = [(0, 1, 1, 1, 1, 1), (3600, 1, 1, 1, 1, 1), (7200, 1, 1, 1, 1, 1)]
        self.assertEqual([r[0] for r in df._closed_only(rows, "1h", 7200 + 1800)], [0, 3600])
        self.assertEqual([r[0] for r in df._closed_only(rows, "1h", 10800)], [0, 3600, 7200])

    def test_nyse_session_holiday(self):
        s = nyse_session(datetime(2026, 7, 3, 11, 0, tzinfo=ET))
        self.assertFalse(s["open"]); self.assertTrue(s["holiday"]); self.assertFalse(s["trading_day"])
        self.assertEqual(s["next_open_mt"], "2026-07-06T07:30-06:00")

    def test_nyse_session_weekend(self):
        s = nyse_session(datetime(2026, 10, 17, 12, 0, tzinfo=ET))
        self.assertFalse(s["open"]); self.assertFalse(s["trading_day"]); self.assertFalse(s["holiday"])
        self.assertEqual(s["next_open_mt"], "2026-10-19T07:30-06:00")

    def test_nyse_session_open_and_closed_times(self):
        s = nyse_session(datetime(2026, 10, 14, 10, 0, tzinfo=ET))
        self.assertTrue(s["open"]); self.assertTrue(s["trading_day"]); self.assertEqual(s["close_et"], "16:00")
        self.assertTrue(nyse_session(datetime(2026, 10, 14, 9, 30, tzinfo=ET))["open"])
        self.assertFalse(nyse_session(datetime(2026, 10, 14, 16, 0, tzinfo=ET))["open"])
        pre = nyse_session(datetime(2026, 10, 14, 8, 0, tzinfo=ET))
        self.assertFalse(pre["open"]); self.assertEqual(pre["next_open_mt"], "2026-10-14T07:30-06:00")
        fri = nyse_session(datetime(2026, 10, 16, 17, 0, tzinfo=ET))
        self.assertEqual(fri["next_open_mt"], "2026-10-19T07:30-06:00")
        # accepts any aware datetime (UTC here)
        self.assertTrue(nyse_session(datetime(2026, 10, 14, 15, 0, tzinfo=UTC))["open"])

    def test_nyse_early_close(self):
        s = nyse_session(datetime(2026, 11, 27, 13, 30, tzinfo=ET))
        self.assertFalse(s["open"]); self.assertTrue(s["early_close"]); self.assertEqual(s["close_et"], "13:00")
        self.assertTrue(nyse_session(datetime(2026, 11, 27, 12, 0, tzinfo=ET))["open"])

    def test_market_kind_and_session_key(self):
        self.assertEqual(market_kind("BTC-USD"), "crypto"); self.assertEqual(market_kind("AAPL"), "stock")
        self.assertEqual(market_kind("DOGE-USD", meme=True), "meme")
        t = int(datetime(2026, 10, 15, 3, 0, tzinfo=UTC).timestamp())     # 11 pm ET / 9 pm MT on the 14th
        self.assertEqual(session_key(t, "stock"), "2026-10-14"); self.assertEqual(session_key(t, "crypto"), "2026-10-14")

    def test_merge(self):
        old = make_bars("BTC-USD", "1h", 0, [(1, 1, 1, 1), (2, 2, 2, 2), (3, 3, 3, 3)])
        new = make_bars("BTC-USD", "1h", 7200, [(30, 30, 30, 30), (4, 4, 4, 4)])
        new.source, new.fetched_at = "coinbase", 123.0
        m = merge(old, new, keep=10)
        self.assertEqual(m.t, [0, 3600, 7200, 10800])
        self.assertEqual(m.c, [1.0, 2.0, 30.0, 4.0])                      # newer data wins on overlap
        self.assertEqual((m.source, m.fetched_at), ("coinbase", 123.0))
        self.assertEqual(merge(old, new, keep=2).t, [7200, 10800])         # keeps the newest bars

    def test_json_roundtrip(self):
        b = demo_bars("BTC-USD", "1h", 30, now=NOW)
        r = Bars.from_json(b.to_json())
        self.assertEqual((r.t, r.o, r.h, r.l, r.c, r.v, r.source, r.note), (b.t, b.o, b.h, b.l, b.c, b.v, b.source, b.note))

    def test_data_service_demo(self):
        d = tmpdir(self)
        ds = DataService(d, "demo")
        r = ds.get("BTC-USD", "1h", 120)
        self.assertTrue(r["ok"]); self.assertEqual(len(r["bars"]), 120)
        self.assertTrue(r["source"].startswith("DEMO")); self.assertFalse(r["stale"])
        self.assertTrue(os.path.exists(os.path.join(d, "bars", "BTC-USD_1h.json")))
        q = ds.quote("BTC-USD")
        self.assertTrue(q["ok"]); self.assertEqual(q["price"], ds.get("BTC-USD", "1h", 60)["bars"].c[-1])
        self.assertTrue(q["source"].startswith("DEMO"))
        self.assertIsNone(ds.live_price("BTC-USD"))                      # demo never shows a "live" price
        # a second service in the same folder reads the disk cache without refreshing
        r2 = DataService(d, "demo").get("BTC-USD", "1h", 50, refresh=False)
        self.assertTrue(r2["ok"]); self.assertTrue(r2["source"].startswith("DEMO"))

    def test_data_service_fail_closed_without_data(self):
        r = DataService(tmpdir(self), "demo").get("BTC-USD", "1h", 100, refresh=False)
        self.assertFalse(r["ok"]); self.assertIsNone(r["bars"]); self.assertTrue(r["stale"])
        q = DataService(tmpdir(self), "live").quote("BTC-USD")
        self.assertFalse(q["ok"]); self.assertNotIn("price", q)

    def test_live_mode_never_serves_demo_cache(self):
        d = tmpdir(self)
        DataService(d, "demo").get("ETH-USD", "1h", 100)
        r = DataService(d, "live").get("ETH-USD", "1h", 100, refresh=False)
        self.assertFalse(r["ok"]); self.assertIsNone(r["bars"])

    def test_live_fetch_error_returns_no_prices(self):
        # the network guard makes the fetch fail: the service must say so, not invent bars
        r = DataService(tmpdir(self), "live").get("BTC-USD", "1h", 100)
        self.assertFalse(r["ok"]); self.assertIsNone(r["bars"]); self.assertTrue(r["error"])


# ================================================================ library + strategies
class TestLibrary(unittest.TestCase):
    def test_counts(self):
        lib = library.load()
        self.assertEqual(len(lib["keep"]), 100)
        self.assertEqual(len(lib["new"]), 11)
        self.assertEqual(len(lib["bots"]), 99)
        self.assertEqual(len(lib["roles"]), 7)
        self.assertTrue(all(d["runnable"] for d in lib["keep"]))
        runnable_new = {d["id"] for d in lib["new"] if d["runnable"]}
        self.assertEqual(runnable_new, set(NEW_ID_MAP))
        self.assertEqual({d["id"] for d in lib["new"] if d["overlay"]}, {"vol_gate.overlay", "maker_limit_entry.overlay"})
        self.assertEqual(sum(1 for b in lib["bots"] if b["status"] != "PARKED"), 74)

    def test_lookups(self):
        self.assertEqual(library.bot("A001")["symbol"], "BTC-USD")
        self.assertIsNone(library.bot("A999"))
        sid = library.load()["keep"][0]["id"]
        self.assertEqual(library.strategy(sid)["id"], sid)
        self.assertTrue(library.settings_for(sid))
        self.assertEqual(library.settings_for("no.such.id"), "")
        self.assertTrue(library.is_meme({"role": "Meme coins: trend sleeve"}))
        self.assertFalse(library.is_meme({"role": "Crypto majors: trend", "group": "Crypto"}))


class TestStrategies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bm = demo_bars("BTC-USD", "1h", 1500, now=NOW)
        cls.sets = {"crypto 1h": demo_bars("ETH-USD", "1h", 1500, now=NOW),
                    "stock 1h": demo_bars("AAPL", "1h", 1500, now=NOW),
                    "stock 5m": demo_bars("AAPL", "5m", 1500, now=NOW)}
        cls.ids = all_strategy_ids()

    def test_every_strategy_builds_and_runs_on_all_demo_sets(self):
        self.assertEqual(len(self.ids), 100 + len(NEW_ID_MAP))
        fired = 0
        for name, b in self.sets.items():
            ctx = Ctx(b, None, self.bm if b.market == "crypto" else None, None)
            for sid, settings in self.ids:
                with self.subTest(dataset=name, strategy=sid):
                    sp = spec(sid, settings)
                    self.assertIn(sp["generator"], GENERATORS); self.assertIn(sp["filter"], FILTERS)
                    self.assertGreater(sp["params"]["stop_atr"], 0)         # a hard stop on every strategy
                    sig = entry_signals(sp, ctx)
                    self.assertEqual(len(sig), len(b))
                    self.assertTrue(set(sig) <= {0, 1}, "long-only: a signal is 0 (nothing) or 1 (new long)")
                    fired += sum(sig)
                    xs = exit_signals(sp, ctx)
                    if xs is not None:
                        self.assertEqual(len(xs), len(b))
                        self.assertTrue(all(isinstance(x, bool) for x in xs))
        self.assertGreater(fired, 0, "no strategy fired at all on 4500 demo bars")

    def test_signals_with_fresh_ctx_match_shared_ctx(self):
        b = self.sets["crypto 1h"]
        shared = Ctx(b, None, self.bm, None)
        for sid, settings in self.ids[::7]:
            with self.subTest(strategy=sid):
                sp = spec(sid, settings)
                self.assertEqual(entry_signals(sp, shared), entry_signals(sp, Ctx(b, None, self.bm, None)))

    def test_no_lookahead_signals_unchanged_when_bars_appended(self):
        # orb_stocks_in_play is excluded here on purpose: see test_orb_stocks_in_play_signal_on_newest_bar (SOURCE BUG)
        for name, b in self.sets.items():
            bm = self.bm if b.market == "crypto" else None
            full = Ctx(b, None, bm, None)
            ss = full.session_starts()
            first_of_session = next(i for i in range(1000, len(b)) if ss[i] == i)
            for k in (700, first_of_session + 1, 1201):
                pre = Ctx(b.slice(0, k), None, bm, None)
                for sid, settings in self.ids:
                    sp = spec(sid, settings)
                    if sp["generator"] == "orb_stocks_in_play": continue
                    with self.subTest(dataset=name, prefix=k, strategy=sid):
                        self.assertEqual(entry_signals(sp, pre), entry_signals(sp, full)[:k])

    def test_orb_stocks_in_play_fires_in_history(self):
        b = demo_bars("AAPL", "5m", 3000, now=NOW)
        sig = GENERATORS["orb_stocks_in_play"](Ctx(b), {"rvol": 1.0})
        self.assertTrue(any(sig), "precondition for the expected-failure test below")

    # SOURCE BUG: engine/strategies.py g_orb_stocks_in_play only signals when `i + 1 < ctx.n`, i.e. when a LATER
    # bar already exists. On the newest closed bar (what the fleet/live check looks at) it can never fire, while the
    # backtest sees the signal once the next bar is appended -> backtest and live disagree (look-ahead dependence).
    @unittest.expectedFailure
    def test_orb_stocks_in_play_signal_on_newest_bar(self):
        b = demo_bars("AAPL", "5m", 3000, now=NOW)
        full = GENERATORS["orb_stocks_in_play"](Ctx(b), {"rvol": 1.0})
        i = next(k for k, s in enumerate(full) if s)
        newest = GENERATORS["orb_stocks_in_play"](Ctx(b.slice(0, i + 1)), {"rvol": 1.0})
        self.assertEqual(newest[i], 1, "signal on bar i must not depend on whether bar i+1 exists")

    def test_spec_parses_settings_cell(self):
        sp = spec("rsi2_reversion.trend200.time", "lo=10, exit=70, max_bars=20, stop_atr=2.0")
        self.assertEqual((sp["generator"], sp["filter"], sp["exit"]), ("rsi2_reversion", "trend200", "time"))
        self.assertEqual(sp["params"], {"lo": 10, "exit": 70, "max_bars": 20, "stop_atr": 2.0})
        self.assertIsInstance(sp["params"]["lo"], int); self.assertIsInstance(sp["params"]["stop_atr"], float)
        self.assertEqual(spec("donchian_trend.adx.trail", "entry=55, exit=20, trail_atr=3.0")["params"]["stop_atr"], 3.0)
        p = spec("ibs_reversion.none.atr_bracket")["params"]
        self.assertEqual((p["stop_atr"], p["target_r"]), (2.0, 2.0))
        self.assertEqual(spec("orb.relvol.time")["params"]["max_bars"], 20)
        self.assertEqual(spec("orb.relvol.time", {"max_bars": 7})["params"]["max_bars"], 7)
        self.assertEqual(parse_settings("a=1, b=2.5, c=x, junk"), {"a": 1, "b": 2.5, "c": "x"})
        self.assertEqual(parse_settings(None), {})
        already = spec("orb.relvol.time")
        self.assertIs(spec(already), already)

    def test_every_keep_settings_cell_parses_into_params(self):
        for d in library.load()["keep"]:
            with self.subTest(strategy=d["id"]):
                parsed = parse_settings(d["settings"])
                self.assertTrue(parsed, "empty Settings cell")
                sp = spec(d["id"], d["settings"])
                for k, v in parsed.items():
                    self.assertEqual(sp["params"][k], v)

    def test_new_ids_use_map_and_do_not_mutate_it(self):
        before = json.dumps(NEW_ID_MAP, sort_keys=True)
        sp = spec("vol_gate_swing.btc.long_only", {"stop_atr": 9.0})
        self.assertEqual(sp["generator"], "vol_gate_swing"); self.assertEqual(sp["params"]["stop_atr"], 9.0)
        self.assertTrue(sp["params"]["swing"])
        self.assertEqual(json.dumps(NEW_ID_MAP, sort_keys=True), before)

    def test_unknown_parts_raise(self):
        with self.assertRaises(ValueError): spec("no_such_generator.none.time")
        with self.assertRaises(ValueError): spec("rsi2_reversion.no_such_filter.time")
        with self.assertRaises(ValueError): spec("rsi2_reversion.none")

    def test_benchmark_strategies_fail_closed_without_benchmark(self):
        b = self.sets["crypto 1h"]
        ctx = Ctx(b)
        for g in ("pairs_zscore", "btc_led_alt_trend"):
            self.assertEqual(GENERATORS[g](ctx, {}), [0] * len(b), g)
        self.assertFalse(any(FILTERS["trend_confirm"](ctx, i, {}) for i in range(len(b))))

    def test_market_specific_generators_stay_silent_elsewhere(self):
        crypto, stock = Ctx(self.sets["crypto 1h"], None, self.bm), Ctx(self.sets["stock 1h"])
        for g in ("first_hour_momentum", "initial_balance_breakout", "gap_continuation", "gap_fade", "orb_stocks_in_play"):
            self.assertEqual(sum(GENERATORS[g](crypto, {})), 0, g)
        for g in ("crypto_weekend", "crypto_session_orb", "btc_led_alt_trend", "attention_momentum"):
            self.assertEqual(sum(GENERATORS[g](stock, {})), 0, g)

    def test_quiet_volatility_blocks_vol_gated_entries(self):
        b = self.sets["crypto 1h"]
        sp = spec("vol_gate_swing.btc.long_only")
        self.assertEqual(sum(entry_signals(sp, Ctx(b, None, None, ["QUIET"] * len(b)))), 0)
        self.assertGreater(sum(entry_signals(sp, Ctx(b, None, None, ["NORMAL"] * len(b)))), 0)


# ================================================================ volatility gate
class TestVolatility(unittest.TestCase):
    def test_auc(self):
        self.assertEqual(volatility.auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]), 1.0)
        self.assertEqual(volatility.auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]), 0.0)
        self.assertEqual(volatility.auc([0.5] * 4, [0, 1, 0, 1]), 0.5)
        self.assertIsNone(volatility.auc([0.1, 0.2], [1, 1]))

    def test_classify(self):
        self.assertEqual(volatility.classify(0.6, 0.2), "LOUD")
        self.assertEqual(volatility.classify(0.2, 0.6), "QUIET")
        self.assertEqual(volatility.classify(0.4, 0.4), "NORMAL")

    def test_rule_state(self):
        b = make_bars("BTC-USD", "1h", 0, [(100, 101, 99, 100)] * 120)
        self.assertEqual(volatility.rule_state(b, 5), "UNKNOWN")
        self.assertEqual(volatility.rule_state(b, 119), "NORMAL")

    def test_gate_on_demo_bars(self):
        b = demo_bars("BTC-USD", "1h", 1500, now=NOW)
        g = volatility.gate(b)
        self.assertEqual(len(g["states"]), len(b))
        self.assertTrue(set(g["states"]) <= {"LOUD", "NORMAL", "QUIET", "UNKNOWN"})
        self.assertTrue(set(g["source"]) <= {"rule", "model"})
        self.assertIn("model", g["source"])
        for k in ("auc_loud", "auc_quiet"):
            self.assertTrue(g[k] is None or 0.0 <= g[k] <= 1.0)
        s = volatility.summary(b)
        self.assertIn("never direction", s["note"]); self.assertTrue(s["source"].startswith("DEMO"))


# ================================================================ backtest (hand-built bars)
class TestBacktestFills(unittest.TestCase):
    """40 hourly crypto bars, every bar o=c=100, h=101, l=99 -> ATR(14) = 2 after warm-up.
    Signal on bar 20 (Mon 04:00 MT), the order fills at the open of bar 21 (Mon 05:00 MT).
    Crypto minimum stop 3 ATR = 6 points; 2R target = 12 points."""
    SID = "ibs_reversion.none.atr_bracket"           # plain 2R bracket, no signal exit
    T0 = datetime(2026, 10, 11, 8, 0, tzinfo=MT).timestamp()

    def rows(self, n=40, half_range=1.0):
        return [(100.0, 100.0 + half_range, 100.0 - half_range, 100.0)] * n

    def run_rows(self, rows, sig_at=20, symbol="BTC-USD", t0=None, sid=None, **kw):
        sig = [0] * len(rows); sig[sig_at] = 1
        kw.setdefault("vol_gate", False)
        b = make_bars(symbol, "1h", self.T0 if t0 is None else t0, rows)
        return b, bt.run(b, sid or self.SID, signals=sig, **kw)

    def assert_identity(self, trades):
        for t in trades:
            self.assertAlmostEqual(t["r_net"], t["r_gross"] - t["cost_r"], delta=2e-4)
            self.assertGreater(t["cost_r"], 0)
            self.assertLess(t["stop"], t["entry"])                # long only, stop below entry

    def test_fills_at_next_bar_open(self):
        rows = self.rows(); rows[21] = (100.5, 101.0, 99.5, 100.5)
        b, r = self.run_rows(rows)
        self.assertEqual(len(r["trades"]), 1)
        t = r["trades"][0]
        self.assertEqual(t["entry_t"], b.t[21]); self.assertGreater(t["entry_t"], b.t[20])
        self.assertEqual(t["entry"], 100.5)                       # the open of the bar AFTER the signal
        self.assertEqual(t["stop"], 94.5); self.assertEqual(t["target"], 112.5)
        self.assertEqual(r["stop_atr_used"], 3.0)
        self.assert_identity(r["trades"])

    def test_signal_on_last_bar_never_fills_same_bar(self):
        b, r = self.run_rows(self.rows(), sig_at=39)
        self.assertEqual(r["trades"], [])

    def test_stop_before_target_when_both_touched(self):
        rows = self.rows(); rows[22] = (100.0, 113.0, 94.0, 100.0)    # touches target 112 AND stop 94
        b, r = self.run_rows(rows)
        t = r["trades"][0]
        self.assertEqual(t["reason"], "stop"); self.assertEqual(t["exit"], 94.0)
        self.assertEqual(t["r_gross"], -1.0); self.assertEqual(t["exit_t"], b.t[22])
        self.assert_identity(r["trades"])

    def test_target_hit(self):
        rows = self.rows(); rows[22] = (100.0, 113.0, 99.5, 100.0)
        _, r = self.run_rows(rows)
        t = r["trades"][0]
        self.assertEqual((t["reason"], t["exit"], t["r_gross"]), ("target", 112.0, 2.0))

    def test_gap_below_stop_fills_at_open(self):
        rows = self.rows(); rows[22] = (90.0, 91.0, 89.0, 90.0)
        _, r = self.run_rows(rows)
        t = r["trades"][0]
        self.assertEqual(t["reason"], "stop (gap)"); self.assertEqual(t["exit"], 90.0)
        self.assertAlmostEqual(t["r_gross"], -10 / 6, places=3)
        self.assert_identity(r["trades"])

    def test_flat_within_12h(self):
        b, r = self.run_rows(self.rows())
        t = r["trades"][0]
        self.assertEqual(t["reason"], "flat after 12h")
        self.assertLessEqual(t["exit_t"] + 3600 - t["entry_t"], 12 * 3600)

    def test_cost_over_033R_is_skipped(self):
        _, r = self.run_rows(self.rows(half_range=0.1))           # ATR 0.2 -> 3 ATR stop = 0.6% -> cost 0.67R
        self.assertEqual(r["trades"], [])
        self.assertEqual(r["skipped"].get("cost over 0.33R"), 1)

    def test_cost_between_020_and_033R_is_half_size(self):
        _, r = self.run_rows(self.rows(half_range=0.25))          # ATR 0.5 -> stop 1.5% -> cost 0.27R
        self.assertEqual(len(r["trades"]), 1)
        t = r["trades"][0]
        self.assertEqual(t["size_mult"], 0.5)
        self.assertTrue(0.20 < t["cost_r"] <= 0.33, t["cost_r"])

    def test_cost_under_020R_full_size(self):
        _, r = self.run_rows(self.rows())
        self.assertEqual(r["trades"][0]["size_mult"], 1.0)
        self.assertLessEqual(r["trades"][0]["cost_r"], 0.20)

    def test_min_stop_atr_enforced(self):
        _, r = self.run_rows(self.rows(), settings={"stop_atr": 1.0})
        self.assertEqual(r["stop_atr_used"], 3.0)
        t = r["trades"][0]
        self.assertGreaterEqual(t["entry"] - t["stop"], 3 * 2.0 - 1e-9)
        _, rm = self.run_rows(self.rows(), settings={"stop_atr": 1.0}, market="meme")
        self.assertEqual(rm["stop_atr_used"], 4.0)
        self.assertGreaterEqual(rm["trades"][0]["entry"] - rm["trades"][0]["stop"], 4 * 2.0 - 1e-9)
        _, rs = self.run_rows(self.rows(), settings={"stop_atr": 1.0}, market="stock")
        self.assertEqual(rs["stop_atr_used"], 2.0)

    def test_quiet_state_skips_when_gate_on(self):
        rows = self.rows()
        _, r = self.run_rows(rows, vol_gate=True, vol_states=["QUIET"] * len(rows))
        self.assertEqual(r["trades"], []); self.assertEqual(r["skipped"].get("volatility QUIET"), 1)
        _, r2 = self.run_rows(rows, vol_gate=False, vol_states=["QUIET"] * len(rows))
        self.assertEqual(len(r2["trades"]), 1)

    def test_unknown_vol_state_fails_closed(self):
        rows = self.rows()
        _, r = self.run_rows(rows, vol_gate=True)                 # 40 bars: too short for the gate -> UNKNOWN
        self.assertEqual(r["trades"], []); self.assertEqual(r["skipped"].get("volatility unknown"), 1)

    def test_loud_state_wider_stop_smaller_size(self):
        rows = self.rows()
        _, r = self.run_rows(rows, vol_gate=True, vol_states=["LOUD"] * len(rows))
        t = r["trades"][0]
        self.assertAlmostEqual(t["size_mult"], 0.6)
        self.assertAlmostEqual(t["entry"] - t["stop"], 3.0 * 1.25 * 2.0)
        self.assertEqual(t["vol_state"], "LOUD")

    def test_warmup_signal_skipped(self):
        _, r = self.run_rows(self.rows(), sig_at=5)
        self.assertEqual(r["trades"], []); self.assertEqual(r["skipped"].get("warm-up"), 1)

    def test_no_crypto_entries_on_weekend(self):
        for day in (17, 18):                                       # fill bar on Saturday / Sunday 00:00 MT
            t0 = datetime(2026, 10, day, 0, 0, tzinfo=MT).timestamp() - 21 * 3600
            with self.subTest(day=day):
                _, r = self.run_rows(self.rows(), t0=t0)
                self.assertEqual(r["trades"], [])
                self.assertEqual(r["skipped"].get("weekend (no crypto entries Sat/Sun)"), 1)

    def test_no_crypto_entries_in_dead_hours(self):
        t0 = datetime(2026, 10, 12, 19, 0, tzinfo=MT).timestamp() - 21 * 3600   # fill at Mon 7 pm MT
        _, r = self.run_rows(self.rows(), t0=t0)
        self.assertEqual(r["trades"], []); self.assertEqual(r["skipped"].get("dead hours 7 pm-12 am MT"), 1)
        # the one generator built for off-hours may enter then (weekends stay blocked)
        _, r2 = self.run_rows(self.rows(), t0=t0, sid="crypto_weekend.none.atr_bracket")
        self.assertEqual(len(r2["trades"]), 1)

    def test_maker_limit_can_miss(self):
        rows = self.rows(); rows[21] = (101.0, 102.0, 100.5, 101.5)  # never trades back down to the 100 limit
        _, r = self.run_rows(rows, execution="maker")
        self.assertEqual(r["trades"], []); self.assertEqual(r["skipped"].get("limit order not filled"), 1)

    def test_maker_limit_fills_at_limit(self):
        rows = self.rows(); rows[21] = (100.5, 101.0, 99.5, 100.5)
        _, r = self.run_rows(rows, execution="maker")
        self.assertEqual(r["trades"][0]["entry"], 100.0)            # min(open, limit = signal close)
        _, rt = self.run_rows(rows)
        self.assertEqual(rt["trades"][0]["entry"], 100.5)

    def test_rules_off_ignores_min_stop(self):
        _, r = self.run_rows(self.rows(), settings={"stop_atr": 1.0}, rules=False)
        self.assertEqual(r["stop_atr_used"], 1.0)

    def test_result_shape(self):
        b, r = self.run_rows(self.rows())
        for k in ("strategy", "symbol", "bar", "market", "venue", "trades", "skipped", "stats", "equity", "signals",
                  "stop_atr_used", "source", "period", "bars_tested"):
            self.assertIn(k, r)
        self.assertEqual(r["venue"], "ndax"); self.assertEqual(r["signals"], 1)
        self.assertEqual(r["stats"]["trades"], 1)


class TestBacktestStockSession(unittest.TestCase):
    """Two NYSE sessions of hand-built 5m bars (o=c=100, h=100.5, l=99.5 -> ATR 1)."""
    SID = "ibs_reversion.none.atr_bracket"

    def two_days(self):
        times = session_times(date(2026, 10, 14), "5m") + session_times(date(2026, 10, 15), "5m")
        return make_bars_at("AAPL", "5m", times, [(100.0, 100.5, 99.5, 100.0)] * len(times))

    def run_sig(self, b, idx, **kw):
        sig = [0] * len(b)
        for i in idx: sig[i] = 1
        return bt.run(b, self.SID, signals=sig, vol_gate=False, **kw)

    def et(self, t): return datetime.fromtimestamp(t, ET)

    def test_layout(self):
        b = self.two_days()
        self.assertEqual(len(b), 156)
        self.assertEqual((self.et(b.t[66]).hour, self.et(b.t[66]).minute), (15, 0))
        self.assertEqual((self.et(b.t[76]).hour, self.et(b.t[76]).minute), (15, 50))

    def test_flat_by_close_same_session(self):
        b = self.two_days()
        r = self.run_sig(b, [66])                                  # signal 3:00 pm, fill 3:05 pm ET
        self.assertEqual(len(r["trades"]), 1)
        t = r["trades"][0]
        self.assertEqual(t["entry_t"], b.t[67]); self.assertEqual(t["reason"], "flat by close")
        self.assertEqual(t["exit_t"], b.t[76])                     # the 3:50 bar, closes 3:55 pm ET
        self.assertEqual(self.et(t["entry_t"]).date(), self.et(t["exit_t"]).date())

    def test_last_entry_1530(self):
        b = self.two_days()
        r = self.run_sig(b, [71])                                  # fill at 3:30 pm: allowed
        self.assertEqual(len(r["trades"]), 1)
        r2 = self.run_sig(b, [72])                                 # fill at 3:35 pm: refused
        self.assertEqual(r2["trades"], []); self.assertEqual(r2["skipped"].get("outside entry hours"), 1)

    def test_no_overnight_entry(self):
        b = self.two_days()
        r = self.run_sig(b, [77])                                  # signal on the 3:55 bar; next bar is tomorrow
        self.assertEqual(r["trades"], []); self.assertEqual(r["skipped"].get("outside entry hours"), 1)

    def test_early_close_day_no_entries(self):
        times = session_times(date(2026, 11, 27), "5m", close_hm=(13, 0))
        b = make_bars_at("AAPL", "5m", times, [(100.0, 100.5, 99.5, 100.0)] * len(times))
        r = self.run_sig(b, [20])
        self.assertEqual(r["trades"], []); self.assertEqual(r["skipped"].get("early close day"), 1)


# ================================================================ backtest rules on demo bars
class TestBacktestRulesOnDemoBars(unittest.TestCase):
    SID = "ibs_reversion.none.atr_bracket"

    def every_bar(self, sym, bar, n=1500, sid=None, **kw):
        b = demo_bars(sym, bar, n, now=NOW)
        kw.setdefault("vol_gate", False)
        return b, bt.run(b, sid or self.SID, signals=[1] * len(b), **kw)

    def check_common(self, b, r):
        self.assertGreater(len(r["trades"]), 5)
        idx = {t: k for k, t in enumerate(b.t)}
        for t in r["trades"]:
            k = idx[t["entry_t"]]
            self.assertGreater(k, 0)
            self.assertAlmostEqual(t["entry"], b.o[k], places=6)     # filled at a bar OPEN, after the signal bar
            self.assertGreaterEqual(t["exit_t"], t["entry_t"])
            self.assertLess(t["stop"], t["entry"])
            self.assertAlmostEqual(t["r_net"], t["r_gross"] - t["cost_r"], delta=2e-4)
            self.assertGreater(t["cost_r"], 0); self.assertLessEqual(t["cost_r"], 0.33 + 1e-9)

    def test_crypto_entry_days_hours_and_12h_flat(self):
        b, r = self.every_bar("ETH-USD", "1h")
        self.check_common(b, r)
        self.assertGreater(r["skipped"].get("weekend (no crypto entries Sat/Sun)", 0), 0)
        for t in r["trades"]:
            mt = datetime.fromtimestamp(t["entry_t"], MT)
            self.assertLess(mt.weekday(), 5, f"weekend entry {mt}")
            self.assertLess(mt.hour, 19, f"dead-hours entry {mt}")
            self.assertLessEqual(t["exit_t"] + 3600 - t["entry_t"], 12 * 3600)

    def test_stock_5m_entry_hours_and_flat_by_close(self):
        b, r = self.every_bar("AAPL", "5m")
        self.check_common(b, r)
        for t in r["trades"]:
            e, x = datetime.fromtimestamp(t["entry_t"], ET), datetime.fromtimestamp(t["exit_t"] + 300, ET)
            self.assertTrue((9, 35) <= (e.hour, e.minute) <= (15, 30), f"entry {e}")
            self.assertEqual(e.date(), x.date(), "trade spans two sessions")
            self.assertLessEqual((x.hour, x.minute), (15, 55), f"still open after 3:55 pm ET: {x}")

    def test_stock_1h_entries_until_1430(self):
        b, r = self.every_bar("AAPL", "1h")
        self.check_common(b, r)
        for t in r["trades"]:
            e, x = datetime.fromtimestamp(t["entry_t"], ET), datetime.fromtimestamp(t["exit_t"], ET)
            self.assertTrue((9, 35) <= (e.hour, e.minute) <= (14, 30), f"entry {e}")
            self.assertEqual(e.date(), x.date(), "trade spans two sessions")

    def test_meme_market_rules(self):
        b, r = self.every_bar("DOGE-USD", "1h", market="meme")
        self.assertEqual(r["stop_atr_used"], 4.0)
        for t in r["trades"]:
            self.assertLess(datetime.fromtimestamp(t["entry_t"], MT).weekday(), 5)

    def test_real_strategy_with_vol_gate(self):
        b = demo_bars("BTC-USD", "1h", 1500, now=NOW)
        sid = "donchian_trend.adx.trail"
        r = bt.run(b, sid, library.settings_for(sid))
        self.assertTrue(r["source"].startswith("DEMO"))
        for t in r["trades"]:
            self.assertIn(t["vol_state"], ("LOUD", "NORMAL"))
        self.assertNotIn("QUIET", {t["vol_state"] for t in r["trades"]})

    def test_stats_benchmarks_and_sharpe(self):
        b, r = self.every_bar("ETH-USD", "1h")
        s = bt.stats(r["trades"])
        self.assertEqual(s["trades"], len(r["trades"]))
        self.assertAlmostEqual(s["total_r"], round(sum(t["r_net"] for t in r["trades"]), 2), places=2)
        self.assertEqual(bt.stats([])["trades"], 0)
        bh = bt.buy_and_hold(b)
        self.assertAlmostEqual(bh["return_pct"], round(100 * (b.c[-1] / b.c[0] - 1), 2))
        self.assertEqual(bt.buy_and_hold(b, 0, 1)["return_pct"], None)
        eb = bt.every_bar_benchmark(b, self.SID, vol_gate=False)
        self.assertEqual(eb["trades"], len(r["trades"]))
        sh = bt.strategy_daily_sharpe(r["trades"], "crypto")
        self.assertTrue(sh is None or isinstance(sh, float))
        self.assertIsNone(bt.strategy_daily_sharpe([], "crypto"))


# ================================================================ validation
class TestValidation(unittest.TestCase):
    def test_pbo_low_when_one_variant_dominates(self):
        rng = random.Random(3)
        M = [[(1.0 if k == 0 else 0.0) + rng.gauss(0, 0.3) for k in range(6)] for _ in range(64)]
        self.assertLessEqual(validation.pbo_cscv(M), 0.1)

    def test_pbo_about_half_on_noise(self):
        vals = []
        for seed in range(30):
            rng = random.Random(seed)
            vals.append(validation.pbo_cscv([[rng.gauss(0, 1) for _ in range(10)] for _ in range(80)]))
        self.assertTrue(all(0.0 <= v <= 1.0 for v in vals))
        mean = sum(vals) / len(vals)
        self.assertTrue(0.35 <= mean <= 0.75, f"mean PBO on pure noise = {mean}")

    def test_pbo_needs_enough_data(self):
        self.assertIsNone(validation.pbo_cscv([]))
        self.assertIsNone(validation.pbo_cscv([[1.0, 2.0]] * 7))     # fewer periods than blocks
        self.assertIsNone(validation.pbo_cscv([[1.0]] * 16))          # one variant only

    def test_weekly_matrix(self):
        a = [{"date": "2026-10-12", "r_net": 1.0}, {"date": "2026-10-13", "r_net": 0.5}, {"date": "2026-10-20", "r_net": -1.0}]
        b = [{"date": "2026-10-14", "r_net": 2.0}]
        M = validation.weekly_matrix([a, b])
        self.assertEqual(M, [[1.5, 2.0], [-1.0, 0.0]])

    def test_placebo_percentile_range(self):
        b = demo_bars("ETH-USD", "1h", 600, now=NOW)
        sid = "ibs_reversion.none.atr_bracket"
        p = validation.placebo_percentile(b, sid, 0.0, 10, draws=12, vol_gate=False)
        self.assertIsNotNone(p); self.assertTrue(0.0 <= p <= 100.0)
        self.assertEqual(validation.placebo_percentile(b, sid, 1e9, 10, draws=8, vol_gate=False), 100.0)
        self.assertEqual(validation.placebo_percentile(b, sid, -1e9, 10, draws=8, vol_gate=False), 0.0)
        self.assertIsNone(validation.placebo_percentile(b, sid, 0.5, 2, draws=8, vol_gate=False))

    def test_trial_counter_persists(self):
        d = tmpdir(self)
        self.assertEqual(validation.tries(d), 0)
        self.assertEqual(validation.count_try(d, "rsi2|BTC-USD"), 1)
        self.assertEqual(validation.count_try(d, "rsi2|BTC-USD"), 2)
        self.assertEqual(validation.count_try(d, "orb|AAPL", 3), 3)
        self.assertEqual(validation.tries(d, "rsi2|BTC-USD"), 2)
        self.assertEqual(validation.tries(d), 5)
        self.assertEqual(read_json(os.path.join(d, "trials.json"))["_total"], 5)
        self.assertEqual(validation.tries(os.path.join(d, "missing")), 0)

    def test_full_report(self):
        d = tmpdir(self)
        b = demo_bars("ETH-USD", "1h", 1500, now=NOW)
        sid = "rsi2_reversion.trend200.time"
        fr = validation.full_report(b, sid, d, settings=library.settings_for(sid), draws=15)
        g = fr["gate"]
        self.assertIn(g["verdict"], VERDICTS)
        self.assertEqual(g["checks"][0]["name"], "Test trades")
        self.assertEqual(g["checks"][0]["value"], fr["test"]["trades"])
        if fr["test"]["trades"] < 30:
            self.assertNotEqual(g["verdict"], "PAPER TEST")
        self.assertGreaterEqual(fr["n_trials_counted"], validation.MIN_ROLE_TRIES)
        self.assertEqual(validation.tries(d), 1)
        self.assertTrue(fr["placebo_pct"] is None or 0 <= fr["placebo_pct"] <= 100)
        self.assertTrue(fr["pbo"] is None or 0 <= fr["pbo"] <= 1)
        for k in ("every_bar", "buy_hold", "test", "pbo_variants"):
            self.assertIn(k, fr)
        validation.full_report(b, sid, d, settings=library.settings_for(sid), draws=5)
        self.assertEqual(validation.tries(d), 2)                       # every try is counted

    def test_full_report_few_trades_never_paper_test(self):
        d = tmpdir(self)
        b = demo_bars("ETH-USD", "1h", 400, now=NOW)
        fr = validation.full_report(b, "ibs_reversion.none.atr_bracket", d, draws=5)
        self.assertLess(fr["test"]["trades"], 30)
        self.assertIn(fr["gate"]["verdict"], {"REJECT", "RESEARCH FURTHER"})

    def test_gate_never_opens_under_30_trades(self):
        trades = [{"date": (date(2026, 1, 1) + timedelta(days=k)).isoformat(), "r_net": 2.0 + 0.01 * k, "cost_r": 0.01}
                  for k in range(29)]
        g = quality_gate.run_gate(trades, n_trials=22, pbo=0.0, beats_buy_hold=True, placebo_pct=100.0)
        self.assertEqual(g.verdict, "RESEARCH FURTHER")

    def test_role_test_demo(self):
        d = tmpdir(self)
        role = "Liquid crypto: mean reversion"
        r = validation.role_test(role, DataService(d, "demo"), d)
        self.assertTrue(r["ok"])
        self.assertIn(r["gate"]["verdict"], VERDICTS)
        if r["test_stats"]["trades"] < 30:
            self.assertNotEqual(r["gate"]["verdict"], "PAPER TEST")
        self.assertIn(r["chosen"], r["candidates"])
        self.assertTrue(r["source"].startswith("DEMO"))
        saved = validation.gate_results(d)[role]
        self.assertEqual(saved["verdict"], r["gate"]["verdict"]); self.assertEqual(saved["chosen"], r["chosen"])
        self.assertEqual(validation.tries(d, f"role|{role}"), len(r["candidates"]))
        self.assertEqual(validation.role_test("no such role", DataService(d, "demo"), d), {"ok": False, "error": "unknown role"})


# ================================================================ paper broker
class TestPaperBroker(unittest.TestCase):
    T0 = datetime(2026, 10, 11, 8, 0, tzinfo=MT).timestamp()

    def setUp(self):
        self.dir = tmpdir(self)

    def bars(self, rows=None, n=25):
        return make_bars("BTC-USD", "1h", self.T0, rows or [(100.0, 101.0, 99.0, 100.0)] * n)

    def place(self, book, b, bot="A001", sig=20, **kw):
        args = dict(bot=bot, symbol=b.symbol, market="crypto", signal_bar_t=b.t[sig], bar="1h", stop_atr=3.0, atr=2.0,
                    target_r=2.0, flat_by=b.t[-1] + 30 * 86400, size_mult=1.0, cost_pct=0.4, cid="cid-" + bot,
                    strategy="test.none.atr_bracket", flags={}, halt=False)
        args.update(kw)
        return book.place(**args)

    def test_place_queues_and_waits_for_next_bar(self):
        book, b = Book(self.dir, "paper"), self.bars()
        r = self.place(book, b)
        self.assertTrue(r["ok"]); self.assertIn("A001", book.s["pending"]); self.assertEqual(book.s["positions"], {})
        self.assertEqual(book.on_bars("A001", b.slice(0, 21)), [])          # only the signal bar exists so far
        self.assertIn("A001", book.s["pending"]); self.assertEqual(book.s["positions"], {})
        self.assertIn("A001", Book(self.dir, "paper").s["pending"])         # persisted to disk

    def test_fills_at_first_bar_after_signal(self):
        rows = [(100.0, 101.0, 99.0, 100.0)] * 25; rows[21] = (100.5, 101.0, 99.5, 100.5)
        book, b = Book(self.dir, "paper"), self.bars(rows)
        self.place(book, b)
        ev = book.on_bars("A001", b)
        self.assertTrue(any("BUY" in e["event"] for e in ev))
        p = book.s["positions"]["A001"]
        self.assertEqual(p["entry_t"], b.t[21]); self.assertEqual(p["entry"], 100.5)
        self.assertEqual(p["stop"], 94.5); self.assertEqual(p["target"], 112.5)
        self.assertEqual(book.s["pending"], {})
        self.assertEqual(len(book.positions()), 1)

    def test_duplicate_refused(self):
        book, b = Book(self.dir, "paper"), self.bars()
        self.assertTrue(self.place(book, b)["ok"])
        r = self.place(book, b)
        self.assertFalse(r["ok"]); self.assertIn("duplicate", r["reason"])
        book.on_bars("A001", b)
        self.assertIn("A001", book.s["positions"])
        self.assertIn("duplicate", self.place(book, b, sig=22)["reason"])  # open position also blocks

    def test_collar_refuses_big_gap(self):
        for o in (103.0, 97.0):                                             # > 2% from the last close (100)
            with self.subTest(open=o):
                book = Book(tmpdir(self), "paper")
                rows = [(100.0, 101.0, 99.0, 100.0)] * 25; rows[21] = (o, o + 0.5, o - 0.5, o)
                b = self.bars(rows)
                self.place(book, b)
                ev = book.on_bars("A001", b)
                self.assertTrue(any("collar" in e["event"] for e in ev))
                self.assertEqual(book.s["positions"], {}); self.assertEqual(book.s["pending"], {})
        self.assertEqual(COLLAR_PCT, 2.0)

    def test_halt_refuses_new_orders(self):
        book, b = Book(self.dir, "paper"), self.bars()
        r = self.place(book, b, halt=True)
        self.assertFalse(r["ok"]); self.assertIn("HALT", r["reason"]); self.assertEqual(book.s["pending"], {})

    def test_halt_cancels_pending(self):
        book, b = Book(self.dir, "paper"), self.bars()
        self.place(book, b)
        ev = book.on_bars("A001", b, halt=True)
        self.assertTrue(any("cancelled (HALT)" in e["event"] for e in ev))
        self.assertEqual(book.s["pending"], {}); self.assertEqual(book.s["positions"], {})

    def test_halt_closes_open_position_on_next_bar(self):
        book, b = Book(self.dir, "paper"), self.bars()
        self.place(book, b)
        book.on_bars("A001", b.slice(0, 23))
        self.assertIn("A001", book.s["positions"])
        ev = book.on_bars("A001", b, halt=True)
        self.assertTrue(any("HALT" in e["event"] for e in ev))
        self.assertEqual(book.s["positions"], {})
        self.assertTrue(book.s["closed"][-1]["reason"].startswith("HALT"))

    def test_close_all_closes_positions_and_clears_pending(self):
        book, b = Book(self.dir, "paper"), self.bars()
        self.place(book, b)
        book.on_bars("A001", b)
        self.place(book, b, bot="A002")
        out = book.close_all("HALT: kill switch", {"BTC-USD": 99.0})
        self.assertEqual(len(out), 1)
        self.assertEqual(book.s["positions"], {}); self.assertEqual(book.s["pending"], {})

    def test_stop_target_and_flat_management(self):
        rows = [(100.0, 101.0, 99.0, 100.0)] * 25; rows[23] = (100.0, 113.0, 93.0, 100.0)   # both touched
        book, b = Book(self.dir, "paper"), self.bars(rows)
        self.place(book, b)
        book.on_bars("A001", b)
        c = book.s["closed"][-1]
        self.assertEqual(c["reason"], "stop"); self.assertEqual(c["exit"], 94.0)       # stop first, pessimistic
        self.assertAlmostEqual(c["r"], -1 - c["cost_r"], places=2)
        rows2 = [(100.0, 101.0, 99.0, 100.0)] * 25; rows2[23] = (90.0, 91.0, 89.0, 90.0)
        book2 = Book(tmpdir(self), "paper"); b2 = self.bars(rows2)
        self.place(book2, b2); book2.on_bars("A001", b2)
        self.assertEqual((book2.s["closed"][-1]["reason"], book2.s["closed"][-1]["exit"]), ("stop (gap)", 90.0))
        book3 = Book(tmpdir(self), "paper"); b3 = self.bars()
        self.place(book3, b3, flat_by=b3.t[22] + 3600)
        book3.on_bars("A001", b3)
        self.assertEqual(book3.s["closed"][-1]["reason"], "flat time")
        self.assertEqual(book3.s["closed"][-1]["exit_t"], b3.t[22])

    def test_paper_log_in_kit_format(self):
        book, b = Book(self.dir, "paper"), self.bars()
        self.place(book, b); book.on_bars("A001", b)
        book.close_all("HALT: kill switch", {"BTC-USD": 99.0})
        path = os.path.join(self.dir, "paper_log.csv")
        with open(path, newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["date", "bot", "symbol", "pnl", "r", "cost_r", "status"])
        self.assertEqual(LOG_FIELDS, rows[0])
        self.assertEqual(rows[1][1:3], ["A001", "BTC-USD"]); self.assertEqual(rows[1][6], "PAPER")
        date.fromisoformat(rows[1][0])
        kit_rows = jarvus_reporter.load(path)
        s = jarvus_reporter.summarize(kit_rows)
        self.assertTrue(s["paper_only"])
        self.assertAlmostEqual(s["pnl_all"], round(book.s["realized"], 2), places=2)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "shadow_log.csv")))

    def test_shadow_book_is_independent(self):
        paper, shadow, b = Book(self.dir, "paper"), Book(self.dir, "shadow"), self.bars()
        self.assertTrue(self.place(paper, b)["ok"]); self.assertTrue(self.place(shadow, b)["ok"])
        shadow.on_bars("A001", b)
        self.assertIn("A001", shadow.s["positions"])
        self.assertIn("A001", paper.s["pending"]); self.assertEqual(paper.s["positions"], {})
        shadow.close_all("test", {"BTC-USD": 99.0})
        self.assertTrue(os.path.exists(os.path.join(self.dir, "shadow_book.json")))
        with open(os.path.join(self.dir, "shadow_log.csv"), newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["status"], "SHADOW")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "paper_log.csv")))
        self.assertEqual(paper.s["realized"], 0.0); self.assertEqual(paper.summary()["closed_trades"], 0)

    def test_daily_limit_applies_to_paper_not_shadow(self):
        today = datetime.now(MT).date().isoformat()
        paper, shadow, b = Book(self.dir, "paper"), Book(self.dir, "shadow"), self.bars()
        paper.s["trades_by_day"][today] = 5; shadow.s["trades_by_day"][today] = 5
        r = self.place(paper, b)
        self.assertFalse(r["ok"]); self.assertEqual(r["reason"], "daily trade limit")
        self.assertTrue(self.place(shadow, b)["ok"])

    def test_risk_engine_flags(self):
        b = self.bars()
        for flags, market, ok in (({"pause_new_crypto": True}, "crypto", False), ({"pause_new_all": True}, "crypto", False),
                                  ({"pause_new_meme": True}, "meme", False), ({"pause_new_meme": True}, "crypto", True),
                                  ({"pause_new_stock": True}, "stock", False)):
            with self.subTest(flags=flags, market=market):
                r = self.place(Book(tmpdir(self), "paper"), b, flags=flags, market=market)
                self.assertEqual(r["ok"], ok, r)
        book = Book(self.dir, "paper")
        self.assertTrue(self.place(book, b, flags={"half_size": {"A001": True}})["ok"])
        self.assertEqual(book.s["pending"]["A001"]["size_mult"], 0.5)

    def test_summary_is_paper_only(self):
        s = Book(self.dir, "paper").summary()
        self.assertTrue(s["paper_only"]); self.assertEqual(s["equity"], 10000.0); self.assertEqual(s["kind"], "paper")

    def test_engine_trading_modules_have_no_network_code(self):
        for mod in (paper_broker, fleet_mod, bt, validation):
            with self.subTest(module=mod.__name__):
                src = read_text(mod.__file__)
                for bad in ("urllib", "http.client", "requests", "socket", "api_key"):
                    self.assertNotIn(bad, src)


# ================================================================ fleet
class _FailingStocks(DataService):
    """Demo data service where every stock symbol is down (to test fail-closed rows)."""
    def get(self, symbol, bar="1h", n=1500, refresh=True):
        if market_kind(symbol) == "stock":
            return {"ok": False, "bars": None, "error": "simulated outage", "source": None, "stale": True}
        return super().get(symbol, bar, n, refresh)


def _active_bots():
    return [b for b in library.bots() if b["status"] != "PARKED"]


class TestFleetDemoCycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="jarvis_fleet_")
        cls.fleet = fleet_mod.Fleet(DataService(cls.dir, "demo"), cls.dir)
        cls.result = cls.fleet.cycle(refresh_budget=200)
        cls.rows = read_json(os.path.join(cls.dir, "fleet.json"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, True)

    def test_runs_all_74_active_bots(self):
        self.assertEqual(self.result, {"ok": True, "bots": 74, "cycle": 1})
        self.assertEqual(sorted(r["bot_id"] for r in self.rows), sorted(b["bot_id"] for b in _active_bots()))
        self.assertFalse({r["status"] for r in self.rows} & {"PARKED"})

    def test_writes_state_files(self):
        for f in ("fleet.json", "report.json", "flags.json", "fleet_totals.json", "paper_book.json"):
            self.assertTrue(os.path.exists(os.path.join(self.dir, f)), f)
        rep = read_json(os.path.join(self.dir, "report.json"))
        self.assertTrue(rep["paper_only"]); self.assertFalse(rep["halt_flag"]); self.assertIn("generated_at", rep)
        flags = read_json(os.path.join(self.dir, "flags.json"))
        self.assertIsInstance(flags["half_size"], dict)
        self.assertIsInstance(flags["pause_new_crypto"], bool); self.assertIsInstance(flags["pause_new_meme"], bool)
        tot = read_json(os.path.join(self.dir, "fleet_totals.json"))
        self.assertTrue(tot["paper_only"]); self.assertEqual(tot["buy_answers"], 0)

    def test_every_row_has_heartbeat_and_correlation_id(self):
        cids = []
        for r in self.rows:
            with self.subTest(bot=r["bot_id"]):
                self.assertTrue(r.get("heartbeat_time"))
                datetime.fromisoformat(r["heartbeat_time"])
                self.assertRegex(r.get("correlation_id") or "", r"^[0-9a-f]{12}$")
                self.assertEqual(r["error"], "")
                self.assertTrue(r["source"].startswith("DEMO"))
                self.assertIn(r["decision"], ("BUY", "WAIT"))
                cids.append(r["correlation_id"])
        self.assertEqual(len(set(cids)), len(cids))

    def test_no_buy_without_passed_role(self):
        self.assertEqual(validation.gate_results(self.dir), {})
        self.assertFalse([r["bot_id"] for r in self.rows if r["decision"] == "BUY"])
        self.assertEqual(self.fleet.paper.s["pending"], {}); self.assertEqual(self.fleet.paper.s["positions"], {})

    def test_no_pause_files_or_halt_for_non_trading_bots(self):
        self.assertEqual(policy.paused_bots(self.fleet.cfg), set())
        self.assertFalse(policy.halt_active(self.fleet.cfg))

    def test_second_cycle_new_correlation_ids(self):
        f2 = fleet_mod.Fleet(DataService(self.dir, "demo"), self.dir)        # reloads fleet.json from disk
        self.assertEqual(len(f2.feed), 74)
        old = {r["bot_id"]: r["correlation_id"] for r in self.rows}
        self.assertTrue(f2.cycle(refresh_budget=0)["ok"])
        new = {r["bot_id"]: r["correlation_id"] for r in read_json(os.path.join(self.dir, "fleet.json"))}
        self.assertEqual(set(new), set(old))
        self.assertTrue(all(new[b] != old[b] for b in old))

    # SOURCE BUG: Fleet._auto_pause writes flags["updated"] = self.last_cycle, but cycle() only sets last_cycle AFTER
    # _auto_pause runs, so flags.json carries the PREVIOUS cycle's time (None on the first cycle).
    @unittest.expectedFailure
    def test_flags_updated_timestamp_is_this_cycle(self):
        flags = read_json(os.path.join(self.dir, "flags.json"))
        self.assertIsNotNone(flags["updated"], "flags.json 'updated' should be the time of the cycle that wrote it")


class TestFleetFailClosed(unittest.TestCase):
    def test_missing_data_means_wait_and_no_pause_files(self):
        d = tmpdir(self)
        f = fleet_mod.Fleet(_FailingStocks(d, "demo"), d)
        self.assertTrue(f.cycle(refresh_budget=200)["ok"])
        rows = read_json(os.path.join(d, "fleet.json"))
        self.assertEqual(len(rows), 74)
        stocks = [r for r in rows if market_kind(r["symbol"]) == "stock"]
        self.assertTrue(stocks)
        for r in stocks:
            with self.subTest(bot=r["bot_id"]):
                self.assertEqual(r["decision"], "WAIT"); self.assertEqual(r["reason"], "Need fresh prices")
                self.assertIn("no data", r["error"]); self.assertTrue(r["correlation_id"])
                self.assertIn("PAUSE", r.get("would_pause", ""))         # shown, not applied: bot cannot trade
        self.assertEqual(policy.paused_bots(f.cfg), set())
        self.assertFalse(policy.halt_active(f.cfg))

    def test_default_refresh_budget_fails_closed(self):
        d = tmpdir(self)
        f = fleet_mod.Fleet(DataService(d, "demo"), d)
        f.cycle()                                                       # only 25 symbols refreshed on cycle 1
        rows = read_json(os.path.join(d, "fleet.json"))
        self.assertEqual(len(rows), 74)
        for r in rows:
            if r.get("error"):
                self.assertEqual(r["decision"], "WAIT")
        self.assertEqual(policy.paused_bots(f.cfg), set())

    def test_event_blackout(self):
        self.assertTrue(fleet_mod.event_blackout(datetime(2026, 10, 28, 11, 30, tzinfo=MT)))
        self.assertFalse(fleet_mod.event_blackout(datetime(2026, 10, 28, 10, 30, tzinfo=MT)))
        self.assertFalse(fleet_mod.event_blackout(datetime(2026, 10, 27, 11, 30, tzinfo=MT)))


class TestFleetForcedSignals(unittest.TestCase):
    """Every bot gets a forced LONG signal at a fixed weekday 10:00 MT; only one role is marked as passed (in a temp
    gate_results.json). Checks: BUY only for the passed role, others go to the shadow book; then HALT -> all WAIT."""
    PASSED = "Crypto majors: trend"

    def setUp(self):
        self.dir = tmpdir(self)
        write_json({self.PASSED: {"verdict": "PAPER TEST", "chosen": "donchian_trend.adx.trail", "run_at": "test",
                                  "test_trades": 40, "source": "TEST"}}, os.path.join(self.dir, "gate_results.json"))
        self.fleet = fleet_mod.Fleet(DataService(self.dir, "demo"), self.dir)
        self.fleet.cfg.max_data_age_s = 10 ** 9          # demo bar age depends on the wall clock; not under test here
        self.now = datetime(2026, 10, 14, 10, 0, tzinfo=MT).astimezone(timezone.utc)
        for p in (mock.patch.object(fleet_mod, "entry_signals", lambda sp, ctx: [1] * ctx.n),
                  mock.patch.object(volatility, "gate", lambda b: {"states": ["NORMAL"] * len(b)}),
                  mock.patch.object(volatility, "rule_state", lambda b, i, *a, **k: "NORMAL")):
            p.start(); self.addCleanup(p.stop)

    def run_all(self, halt=False):
        rows = []
        for b in _active_bots():
            row = self.fleet.run_bot(b, self.now, halt, {}, {}, True)
            self.fleet.feed[b["bot_id"]] = row
            rows.append(row)
        return rows

    def test_buy_only_for_passed_role_then_halt_makes_everything_wait(self):
        rows = self.run_all()
        passed_bots = {b["bot_id"] for b in _active_bots() if b["role"] == self.PASSED}
        buys = {r["bot_id"] for r in rows if r["decision"] == "BUY"}
        self.assertTrue(buys, "with a passed role and a forced signal some bot should get BUY")
        self.assertTrue(buys <= passed_bots, f"BUY for a role that has not passed: {buys - passed_bots}")
        for r in rows:
            self.assertTrue(r["correlation_id"]); self.assertTrue(r["heartbeat_time"])
            if r["bot_id"] not in passed_bots:
                self.assertEqual(r["decision"], "WAIT")
                self.assertEqual(r["reason"], "Real money is off and the bot has not passed")
            else:
                self.assertEqual(r["status"], "ACTIVE-PAPER")
        self.assertEqual(set(self.fleet.paper.s["pending"]), buys)               # paper orders only after BUY
        shadow = set(self.fleet.shadow.s["pending"])
        self.assertTrue(shadow); self.assertFalse(shadow & buys)
        self.assertTrue((set(r["bot_id"] for r in rows) - passed_bots) <= shadow)
        # ---- HALT: create-only kill switch
        policy.trip_halt(self.fleet.cfg, "unit test")
        res = self.fleet.cycle(refresh_budget=200)
        self.assertTrue(res["ok"])
        rows2 = read_json(os.path.join(self.dir, "fleet.json"))
        self.assertEqual(len(rows2), 74)
        for r in rows2:
            with self.subTest(bot=r["bot_id"]):
                self.assertEqual(r["decision"], "WAIT"); self.assertEqual(r["reason"], "HALT flag is on")
        self.assertEqual(self.fleet.paper.s["pending"], {}); self.assertEqual(self.fleet.paper.s["positions"], {})
        self.assertTrue(any("cancelled (HALT)" in e["event"] for e in self.fleet.events))
        self.assertTrue(policy.halt_active(self.fleet.cfg))                      # nothing cleared it
        self.assertTrue(self.fleet.totals["halt"])
        self.assertTrue(read_json(os.path.join(self.dir, "report.json"))["halt_flag"])
        self.assertTrue(policy.paused_bots(self.fleet.cfg) <= passed_bots)        # pause files only for tradable bots

    def test_halt_on_direct_run_blocks_paper_orders(self):
        policy.trip_halt(self.fleet.cfg, "unit test")
        rows = self.run_all(halt=True)
        self.assertTrue(all(r["decision"] == "WAIT" and r["reason"] == "HALT flag is on" for r in rows))
        self.assertEqual(self.fleet.paper.s["pending"], {})


class TestFleetAutoPause(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)
        self.fleet = fleet_mod.Fleet(DataService(self.dir, "demo"), self.dir)

    def test_actions(self):
        f = self.fleet
        f.feed = {"A010": {"bot_id": "A010", "status": "RETRAIN", "error": "boom"},
                  "A001": {"bot_id": "A001", "status": "ACTIVE-PAPER", "error": "boom"},
                  "A020": {"bot_id": "A020", "status": "RETRAIN", "cost_r_avg": 0.25},
                  "A030": {"bot_id": "A030", "status": "RETRAIN", "open_position": True, "consecutive_losses": 3}}
        f.totals = {"day_pnl_pct": 0.0, "week_pnl_pct": 0.0, "crypto_meme_risk_share": 0.5, "meme_capital_pct": 6.0}
        f._auto_pause()
        self.assertEqual(policy.paused_bots(f.cfg), {"A001", "A030"})             # only bots that can trade
        self.assertTrue(f.feed["A010"]["would_pause"].startswith("PAUSE"))
        flags = f.flags()
        self.assertEqual(flags["half_size"], {"A020": True})
        self.assertTrue(flags["pause_new_crypto"]); self.assertTrue(flags["pause_new_meme"])
        self.assertFalse(policy.halt_active(f.cfg))
        self.assertTrue(policy.AuditLog(f.cfg.audit_file).verify())

    def test_fleet_loss_trips_halt_and_it_stays(self):
        f = self.fleet
        f.feed = {}
        f.totals = {"day_pnl_pct": -3.5, "week_pnl_pct": 0.0}
        f._auto_pause()
        self.assertTrue(policy.halt_active(f.cfg))
        f.totals = {"day_pnl_pct": 0.0, "week_pnl_pct": 0.0}
        f._auto_pause()
        self.assertTrue(policy.halt_active(f.cfg))                                # create-only: nothing clears it

    def test_no_resume_or_unhalt_api(self):
        for mod in (fleet_mod, paper_broker, policy):
            names = [n.lower() for n in dir(mod)]
            self.assertFalse([n for n in names if "resume" in n or "unhalt" in n or "clear_halt" in n], mod.__name__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
