"""data_feed.py - market data service.

Sources
  - crypto: Coinbase Exchange public candles (no key). Real prices.
  - US stocks: Yahoo chart API (no key). PROTOTYPE ONLY: unofficial, can break, intraday history is short.
  - demo: simulated bars (random walk with volatility clustering). Clearly labelled "DEMO"; never real prices.

Rules from the build brief
  - Only CLOSED bars are returned (the bar that is still forming is dropped).
  - Every result says its source, when it was fetched, and how old the last closed bar is.
  - Stale = last closed bar older than (bar size + 15 minutes). Stale data means WAIT downstream.
  - Fetch errors never turn into made-up prices: the call returns ok=False and the caller waits.
"""
from __future__ import annotations
import json, math, os, random, threading, time, urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MT = ZoneInfo("America/Edmonton")
UTC = timezone.utc

BAR_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
COINBASE_GRAN = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
YAHOO_RANGE = {"1m": "7d", "5m": "60d", "15m": "60d", "30m": "60d", "1h": "730d", "1d": "5y"}
UA = "Mozilla/5.0 (JarvisTradingPortal; paper-only research)"

# ---------------- NYSE calendar ----------------
# VERIFY against https://www.nyse.com/markets/hours-calendars before relying on it (the kit asks for this too).
NYSE_HOLIDAYS = {date.fromisoformat(d) for d in [
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04",
    "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
    "2027-11-25", "2027-12-24"]}
NYSE_EARLY_CLOSE = {date.fromisoformat(d) for d in ["2025-07-03", "2025-11-28", "2025-12-24", "2026-11-27", "2026-12-24",
                                                    "2027-11-26"]}


def market_kind(symbol: str, meme: bool = False) -> str:
    if meme: return "meme"
    return "crypto" if symbol.upper().endswith("-USD") else "stock"


def nyse_session(now: datetime) -> dict:
    """Is the US stock market open right now? Times in ET and MT."""
    et = now.astimezone(ET); d = et.date()
    close_t = (13, 0) if d in NYSE_EARLY_CLOSE else (16, 0)
    open_dt = et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_dt = et.replace(hour=close_t[0], minute=close_t[1], second=0, microsecond=0)
    trading_day = et.weekday() < 5 and d not in NYSE_HOLIDAYS
    is_open = trading_day and open_dt <= et < close_dt
    nxt = et
    if is_open or not trading_day or et >= close_dt:
        nxt = (et + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
        while nxt.weekday() >= 5 or nxt.date() in NYSE_HOLIDAYS: nxt += timedelta(days=1)
    else:
        nxt = open_dt
    return {"open": is_open, "trading_day": trading_day, "early_close": d in NYSE_EARLY_CLOSE,
            "holiday": d in NYSE_HOLIDAYS, "next_open_mt": nxt.astimezone(MT).isoformat(timespec="minutes"),
            "close_et": f"{close_t[0]}:{close_t[1]:02d}"}


def session_key(t: int, market: str) -> str:
    """Trading-session id for a bar open time: ET date for stocks, MT date for crypto."""
    tz = ET if market == "stock" else MT
    return datetime.fromtimestamp(t, tz).date().isoformat()


# ---------------- bars ----------------
@dataclass
class Bars:
    symbol: str
    bar: str
    t: list = field(default_factory=list)      # bar OPEN time, unix seconds UTC
    o: list = field(default_factory=list)
    h: list = field(default_factory=list)
    l: list = field(default_factory=list)
    c: list = field(default_factory=list)
    v: list = field(default_factory=list)
    source: str = ""
    fetched_at: float = 0.0
    note: str = ""
    requested: int = 0          # how many bars were asked for when fetched (so a small request never shrinks the cache)

    def __len__(self): return len(self.c)

    @property
    def market(self): return market_kind(self.symbol)

    def last_close_time(self):
        return self.t[-1] + BAR_SECONDS[self.bar] if self.t else None

    def age_s(self, now: float | None = None):
        lc = self.last_close_time()
        return None if lc is None else (now or time.time()) - lc

    def stale(self, now: float | None = None) -> bool:
        a = self.age_s(now)
        return a is None or a > BAR_SECONDS[self.bar] + 900

    def slice(self, a: int, b: int | None = None) -> "Bars":
        return Bars(self.symbol, self.bar, self.t[a:b], self.o[a:b], self.h[a:b], self.l[a:b], self.c[a:b], self.v[a:b],
                    self.source, self.fetched_at, self.note, self.requested)

    def to_json(self, last: int | None = None) -> dict:
        s = slice(-last, None) if last else slice(None)
        return {"symbol": self.symbol, "bar": self.bar, "source": self.source, "fetched_at": self.fetched_at,
                "t": self.t[s], "o": self.o[s], "h": self.h[s], "l": self.l[s], "c": self.c[s], "v": self.v[s],
                "note": self.note, "stale": self.stale(), "age_s": self.age_s(), "requested": self.requested}

    @classmethod
    def from_json(cls, d: dict) -> "Bars":
        return cls(d["symbol"], d["bar"], d["t"], d["o"], d["h"], d["l"], d["c"], d["v"], d.get("source", ""),
                   d.get("fetched_at", 0.0), d.get("note", ""), d.get("requested", 0))


def _closed_only(rows: list, bar: str, now: float) -> list:
    sec = BAR_SECONDS[bar]
    return [r for r in rows if r[0] + sec <= now]


def _sane(rows: list) -> list:
    """Drop bars with impossible values (non-positive prices, high < low). Never 'fix' a price."""
    out, seen = [], set()
    for r in rows:
        t, o, h, l, c, v = r
        if t in seen or None in (o, h, l, c): continue
        if min(o, h, l, c) <= 0 or h < l or not (l <= o <= h and l <= c <= h): continue
        seen.add(t); out.append((int(t), float(o), float(h), float(l), float(c), float(v or 0)))
    return sorted(out)


def _get_json(url: str, timeout: float = 15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_coinbase(symbol: str, bar: str, n_bars: int = 1500, now: float | None = None) -> Bars:
    if bar not in COINBASE_GRAN: raise ValueError(f"Coinbase has no {bar} bars")
    gran, now = COINBASE_GRAN[bar], now or time.time()
    end, rows = int(now // gran * gran), []
    while len(rows) < n_bars:
        start = end - 300 * gran
        url = (f"https://api.exchange.coinbase.com/products/{symbol}/candles?granularity={gran}"
               f"&start={datetime.fromtimestamp(start, UTC).isoformat()}&end={datetime.fromtimestamp(end, UTC).isoformat()}")
        data = _get_json(url)
        if not isinstance(data, list) or not data: break
        rows += [(r[0], r[3], r[2], r[1], r[4], r[5]) for r in data]        # [time, low, high, open, close, vol]
        end = start
        if len(data) < 290: break
    rows = _closed_only(_sane(rows), bar, now)[-n_bars:]
    b = Bars(symbol, bar, source="coinbase", fetched_at=now)
    for t, o, h, l, c, v in rows:
        b.t.append(t); b.o.append(o); b.h.append(h); b.l.append(l); b.c.append(c); b.v.append(v)
    return b


def fetch_yahoo(symbol: str, bar: str, now: float | None = None) -> Bars:
    now = now or time.time()
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={bar}"
           f"&range={YAHOO_RANGE.get(bar, '60d')}&includePrePost=false")
    data = _get_json(url)
    res = (data.get("chart") or {}).get("result") or []
    if not res: raise RuntimeError("yahoo returned no data")
    r0 = res[0]; q = r0["indicators"]["quote"][0]
    rows = list(zip(r0.get("timestamp") or [], q["open"], q["high"], q["low"], q["close"], q["volume"]))
    rows = _closed_only(_sane(rows), bar, now)
    b = Bars(symbol, bar, source="yahoo (prototype)", fetched_at=now,
             note="Yahoo is prototype-only: unofficial, may change, short intraday history.")
    for t, o, h, l, c, v in rows:
        b.t.append(t); b.o.append(o); b.h.append(h); b.l.append(l); b.c.append(c); b.v.append(v)
    return b


# ---------------- demo (simulated) bars ----------------
DEMO_START = {"BTC-USD": 64000, "ETH-USD": 2500, "SOL-USD": 150, "DOGE-USD": 0.12, "AAPL": 225, "NVDA": 130,
              "MSFT": 420, "JPM": 220, "TSLA": 240, "AMZN": 185}


DEMO_ANCHOR = {"1m": "2026-09-01", "5m": "2026-06-01", "15m": "2026-03-01", "30m": "2026-01-01", "1h": "2021-01-01",
               "4h": "2018-01-01", "1d": "2012-01-01"}


def demo_bars(symbol: str, bar: str = "1h", n: int = 1500, seed: int | None = None, now: float | None = None) -> Bars:
    """Simulated prices for demos and tests. Labelled DEMO everywhere. Not a forecast, not real data.
    The walk starts at a fixed anchor date and runs forward, so the same symbol always shows the same history
    no matter how many bars are requested."""
    sec, now = BAR_SECONDS[bar], now or time.time()
    rng = random.Random(seed if seed is not None else sum(map(ord, symbol + bar)))
    kind = market_kind(symbol)
    price = DEMO_START.get(symbol, rng.uniform(5, 300))
    base_vol = {"crypto": 0.0075, "stock": 0.0045}[kind] * math.sqrt(sec / 3600)
    end = int(now // sec * sec)
    t = int(datetime.fromisoformat(DEMO_ANCHOR.get(bar, "2021-01-01")).replace(tzinfo=UTC).timestamp()) // sec * sec
    t = min(t, end - (n + 1) * sec * (8 if kind == "stock" else 1))   # reach further back if more bars are asked for
    b = Bars(symbol, bar, source="DEMO (simulated)", fetched_at=now, note="Simulated prices for demo/testing. NOT real.")
    var, drift = base_vol ** 2, 0.00002
    while t + sec <= end:
        if kind == "stock":
            dt = datetime.fromtimestamp(t, ET)
            if not (dt.weekday() < 5 and dt.date() not in NYSE_HOLIDAYS and (9, 30) <= (dt.hour, dt.minute) < (16, 0)):
                t += sec; continue
        shock = rng.gauss(0, 1)
        var = 0.05 * base_vol ** 2 + 0.85 * var + 0.10 * (shock ** 2) * var       # GARCH-like clustering
        sd = math.sqrt(var)
        o = price
        c = o * math.exp(drift + sd * shock)
        h = max(o, c) * (1 + abs(rng.gauss(0, sd * 0.6))); l = min(o, c) * (1 - abs(rng.gauss(0, sd * 0.6)))
        vol = rng.uniform(0.6, 1.4) * (1 + 6 * abs(shock) * sd / base_vol / 3) * 1000
        b.t.append(t); b.o.append(o); b.h.append(h); b.l.append(l); b.c.append(c); b.v.append(vol)
        price = c; t += sec
    if symbol in DEMO_START and b.c:                    # keep demo prices near a familiar level (still simulated)
        k = DEMO_START[symbol] / b.c[-1]
        for arr in (b.o, b.h, b.l, b.c):
            arr[:] = [x * k for x in arr]
    return b.slice(-n) if len(b) > n else b


# ---------------- service with cache ----------------
class DataService:
    """One place that hands out bars. mode='live' uses the real feeds, 'demo' uses simulated bars."""

    def __init__(self, state_dir: str, mode: str = "live", max_cache_age_s: int = 120):
        self.dir = os.path.join(state_dir, "bars")
        self.mode = mode
        self.max_cache_age_s = max_cache_age_s
        self.mem: dict = {}
        self.errors: dict = {}
        self.lock = threading.Lock()
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, symbol, bar):
        return os.path.join(self.dir, f"{symbol.replace('/', '_')}_{bar}.json")

    def get(self, symbol: str, bar: str = "1h", n: int = 1500, refresh: bool = True) -> dict:
        """Returns {ok, bars, source, stale, age_s, error}. Never invents data."""
        key = (symbol, bar)
        now = time.time()
        with self.lock:
            cached = self.mem.get(key)
        if cached is None and os.path.exists(self._path(symbol, bar)):
            try:
                cached = Bars.from_json(json.load(open(self._path(symbol, bar))))
                if self.mode == "live" and cached.source.startswith("DEMO"): cached = None
                if self.mode == "demo" and not cached.source.startswith("DEMO"): cached = None
            except Exception:
                cached = None
        max_age = self.max_cache_age_s * (3 if self.mode == "demo" else 1)
        enough = cached is not None and (len(cached) >= n or cached.requested >= n)
        fresh_enough = enough and (now - cached.fetched_at) < max_age
        if refresh and not fresh_enough:
            try:
                want = max(n, len(cached) if cached else 0, cached.requested if cached else 0)   # never shrink the cache
                b = self._fetch(symbol, bar, want)
                b.requested = want
                if len(b) == 0: raise RuntimeError("no closed bars returned")
                cached = b
                with self.lock:
                    self.mem[key] = b; self.errors.pop(key, None)
                try:
                    json.dump(b.to_json(), open(self._path(symbol, bar), "w"))
                except Exception:
                    pass
            except Exception as e:
                with self.lock: self.errors[key] = f"{type(e).__name__}: {str(e)[:160]}"
        elif cached is not None:
            with self.lock: self.mem[key] = cached
        if cached is None or len(cached) == 0:
            return {"ok": False, "bars": None, "error": self.errors.get(key, "no data"), "source": None, "stale": True}
        b = cached.slice(-n) if len(cached) > n else cached
        return {"ok": True, "bars": b, "source": b.source, "stale": b.stale(), "age_s": b.age_s(),
                "error": self.errors.get(key), "note": b.note}

    def _fetch(self, symbol, bar, n) -> Bars:
        if self.mode == "demo":
            return demo_bars(symbol, bar, n)
        if market_kind(symbol) == "crypto":
            old = self.mem.get((symbol, bar))
            if old is not None and len(old) >= n and old.source == "coinbase":
                gap = int((time.time() - old.t[-1]) // BAR_SECONDS[bar]) + 3
                if gap < 300:                                     # incremental update: only the newest bars
                    new = fetch_coinbase(symbol, bar, gap)
                    return merge(old, new, keep=max(n, len(old)))
            return fetch_coinbase(symbol, bar, n)
        return fetch_yahoo(symbol, bar)

    def live_price(self, symbol: str):
        """Latest traded price for DISPLAY only (decisions always use closed bars). None if unavailable."""
        if self.mode == "demo": return None
        try:
            if market_kind(symbol) == "crypto":
                d = _get_json(f"https://api.exchange.coinbase.com/products/{symbol}/ticker", timeout=6)
                return float(d["price"])
            d = _get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d", timeout=6)
            return float(d["chart"]["result"][0]["meta"]["regularMarketPrice"])
        except Exception:
            return None

    def quote(self, symbol: str, bar: str = "1h") -> dict:
        r = self.get(symbol, bar, 60)
        if not r["ok"]: return {"ok": False, "symbol": symbol, "error": r["error"]}
        b = r["bars"]
        prev = b.c[-25] if len(b) >= 25 else b.c[0]
        return {"ok": True, "symbol": symbol, "price": b.c[-1], "change_pct": round(100 * (b.c[-1] / prev - 1), 2),
                "as_of": datetime.fromtimestamp(b.last_close_time(), UTC).isoformat(timespec="seconds"),
                "source": b.source, "stale": r["stale"], "spark": b.c[-48:]}


def merge(old: Bars, new: Bars, keep: int) -> Bars:
    rows = {t: (o, h, l, c, v) for t, o, h, l, c, v in zip(old.t, old.o, old.h, old.l, old.c, old.v)}
    rows.update({t: (o, h, l, c, v) for t, o, h, l, c, v in zip(new.t, new.o, new.h, new.l, new.c, new.v)})
    b = Bars(new.symbol, new.bar, source=new.source, fetched_at=new.fetched_at, note=new.note)
    for t in sorted(rows)[-keep:]:
        o, h, l, c, v = rows[t]
        b.t.append(t); b.o.append(o); b.h.append(h); b.l.append(l); b.c.append(c); b.v.append(v)
    return b
