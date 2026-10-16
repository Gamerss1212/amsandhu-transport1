// calendar.mjs - port of jarvis-trading/engine/data_feed.py without the networking:
// bar sizes, the NYSE calendar, session keys, the Bars helpers and the DEMO (simulated) bars.
//
// OWNER'S HARD RULES: paper trading only (nothing here talks to a broker or sends an order); bars are only ever
// real fetched data or clearly labelled DEMO data - never invented prices; stale or missing data = WAIT downstream.
//
// Python -> JS name map
//   BAR_SECONDS, COINBASE_GRAN, YAHOO_RANGE, DEMO_START, DEMO_ANCHOR -> same names
//   NYSE_HOLIDAYS / NYSE_EARLY_CLOSE (set of date)      -> Set of 'YYYY-MM-DD' strings
//   market_kind(symbol, meme)                           -> marketKind(symbol, meme)
//   nyse_session(now: datetime)                         -> nyseSession(epochSec)   (same dict keys)
//   session_key(t, market)                              -> sessionKey(t, market)
//   Bars(...) dataclass                                 -> makeBars({...}) plain object
//       {symbol, bar, t, o, h, l, c, v, source, fetched (= Python fetched_at), note, requested}
//   len(bars) / bars.market                             -> barsLen(b) / barsMarket(b)
//   bars.last_close_time() / age_s(now) / stale(now)    -> lastCloseTime(b) / ageS(b, now) / isStale(b, now)
//   bars.slice(a, b)                                    -> sliceBars(b, a, end)
//   bars.to_json(last) / Bars.from_json(d)              -> barsToJson(b, last, now) / barsFromJson(d)
//   _closed_only(rows, bar, now) / _sane(rows)          -> closedOnly(rows, bar, now) / sane(rows)
//   merge(old, new, keep)                               -> merge(old, nw, keep)
//   demo_bars(symbol, bar, n, seed, now)                -> demoBars(symbol, bar, n, seed, now)
//   float // float (Python floor division)              -> pyFloorDiv(a, b)
// Not ported here (networking): fetch_coinbase, fetch_yahoo, _get_json, DataService.

import { ET, MT, localParts, localDate, zonedEpoch, isoLocal, dateToDays, daysToDate, weekdayOf, nowSec } from './tz.mjs';
import { PyRandom } from './pyrandom.mjs';

export const BAR_SECONDS = Object.freeze({ '1m': 60, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, '1d': 86400 });
export const COINBASE_GRAN = Object.freeze({ '1m': 60, '5m': 300, '15m': 900, '1h': 3600, '1d': 86400 });
export const YAHOO_RANGE = Object.freeze({ '1m': '7d', '5m': '60d', '15m': '60d', '30m': '60d', '1h': '730d', '1d': '5y' });

// ---------------- NYSE calendar ----------------
// VERIFY against https://www.nyse.com/markets/hours-calendars before relying on it (the kit asks for this too).
export const NYSE_HOLIDAYS = new Set([
  '2025-01-01', '2025-01-09', '2025-01-20', '2025-02-17', '2025-04-18', '2025-05-26', '2025-06-19', '2025-07-04',
  '2025-09-01', '2025-11-27', '2025-12-25',
  '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25', '2026-06-19', '2026-07-03', '2026-09-07',
  '2026-11-26', '2026-12-25',
  '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31', '2027-06-18', '2027-07-05', '2027-09-06',
  '2027-11-25', '2027-12-24']);
export const NYSE_EARLY_CLOSE = new Set(['2025-07-03', '2025-11-28', '2025-12-24', '2026-11-27', '2026-12-24', '2027-11-26']);

/** Python float floor division `a // b` (fmod based, exactly like CPython's float_floor_div). */
export function pyFloorDiv(a, b) {
  if (b === 0) throw new RangeError('float floor division by zero');
  // JS % on numbers is C fmod (exact), so this is exact for safe integers too.
  let mod = a % b;
  let div = (a - mod) / b;
  if (mod) {
    if ((b < 0) !== (mod < 0)) { mod += b; div -= 1.0; }
  }
  let floordiv;
  if (div) {
    floordiv = Math.floor(div);
    if (div - floordiv > 0.5) floordiv += 1.0;
  } else {
    floordiv = (a / b) < 0 || Object.is(a / b, -0) ? -0 : 0;
  }
  return floordiv;
}

/** market_kind: 'meme' if flagged, 'crypto' for '-USD' pairs, otherwise 'stock'. */
export function marketKind(symbol, meme = false) {
  if (meme) return 'meme';
  return symbol.toUpperCase().endsWith('-USD') ? 'crypto' : 'stock';
}

/** nyse_session: is the US stock market open right now? Times in ET and MT.
 *  Returns {open, trading_day, early_close, holiday, next_open_mt, close_et} like the Python dict. */
export function nyseSession(now) {
  if (now instanceof Date) now = now.getTime() / 1000;
  const et = localParts(now, ET);
  const d = et.date;
  const early = NYSE_EARLY_CLOSE.has(d);
  const closeT = early ? [13, 0] : [16, 0];
  const holiday = NYSE_HOLIDAYS.has(d);
  const tradingDay = et.weekday < 5 && !holiday;
  // Python compares datetimes with the same tzinfo by wall clock: compare the time of day directly.
  const tod = et.hour * 3600 + et.minute * 60 + et.second + et.microsecond / 1e6;
  const openTod = 9 * 3600 + 30 * 60, closeTod = closeT[0] * 3600 + closeT[1] * 60;
  const isOpen = tradingDay && openTod <= tod && tod < closeTod;
  let nxtDate;
  if (isOpen || !tradingDay || tod >= closeTod) {
    nxtDate = daysToDate(dateToDays(d) + 1);
    while (weekdayOf(nxtDate) >= 5 || NYSE_HOLIDAYS.has(nxtDate)) nxtDate = daysToDate(dateToDays(nxtDate) + 1);
  } else {
    nxtDate = d;
  }
  const [y, m, dd] = nxtDate.split('-').map(Number);
  const nxt = zonedEpoch(y, m, dd, 9, 30, 0, ET, 0);
  return {
    open: isOpen, trading_day: tradingDay, early_close: early, holiday,
    next_open_mt: isoLocal(nxt, MT, 'minutes'), close_et: `${closeT[0]}:${String(closeT[1]).padStart(2, '0')}`,
  };
}

/** session_key: trading-session id for a bar open time: ET date for stocks, MT date for crypto (and meme). */
export function sessionKey(t, market) {
  return localDate(t, market === 'stock' ? ET : MT);
}

// ---------------- bars ----------------

/** Bars(...) as a plain object. Arrays are used as given (not copied). */
export function makeBars({ symbol, bar, t = [], o = [], h = [], l = [], c = [], v = [], source = '', fetched, fetched_at, note = '', requested = 0 } = {}) {
  return { symbol, bar, t, o, h, l, c, v, source, fetched: fetched ?? fetched_at ?? 0.0, note, requested };
}

/** len(bars) */
export const barsLen = (b) => b.c.length;

/** bars.market */
export const barsMarket = (b) => marketKind(b.symbol);

/** Close time of the last bar (open time + bar size), or null when there are no bars. */
export function lastCloseTime(b) {
  return b.t.length ? b.t[b.t.length - 1] + BAR_SECONDS[b.bar] : null;
}

/** Seconds since the last bar closed (null if no bars). now defaults to the clock, like Python's `now or time.time()`. */
export function ageS(b, now = null) {
  const lc = lastCloseTime(b);
  return lc === null ? null : (now || nowSec()) - lc;
}

/** Stale = no bars, or the last closed bar is older than (bar size + 15 minutes). Stale data means WAIT. */
export function isStale(b, now = null) {
  const a = ageS(b, now);
  return a === null || a > BAR_SECONDS[b.bar] + 900;
}

/** bars.slice(a, b): Python list slicing on every series (negative indexes allowed; end null = to the end). */
export function sliceBars(b, a = null, end = null) {
  const s = a ?? undefined, e = end ?? undefined;
  return {
    symbol: b.symbol, bar: b.bar, t: b.t.slice(s, e), o: b.o.slice(s, e), h: b.h.slice(s, e), l: b.l.slice(s, e),
    c: b.c.slice(s, e), v: b.v.slice(s, e), source: b.source, fetched: b.fetched, note: b.note, requested: b.requested ?? 0,
  };
}

/** bars.to_json(last): the Python dict (keys fetched_at / stale / age_s / requested). `now` is for tests (default clock). */
export function barsToJson(b, last = null, now = null) {
  const s = last ? -last : undefined;
  return {
    symbol: b.symbol, bar: b.bar, source: b.source, fetched_at: b.fetched,
    t: b.t.slice(s), o: b.o.slice(s), h: b.h.slice(s), l: b.l.slice(s), c: b.c.slice(s), v: b.v.slice(s),
    note: b.note, stale: isStale(b, now), age_s: ageS(b, now), requested: b.requested ?? 0,
  };
}

/** Bars.from_json(d) */
export function barsFromJson(d) {
  for (const k of ['symbol', 'bar', 't', 'o', 'h', 'l', 'c', 'v']) if (!(k in d)) throw new RangeError(`bars json is missing '${k}'`);
  return makeBars({
    symbol: d.symbol, bar: d.bar, t: d.t, o: d.o, h: d.h, l: d.l, c: d.c, v: d.v, source: d.source ?? '',
    fetched: d.fetched_at ?? d.fetched ?? 0.0, note: d.note ?? '', requested: d.requested ?? 0,
  });
}

/** Keep only CLOSED bars: rows [t, o, h, l, c, v] whose close time (t + bar size) is <= now. */
export function closedOnly(rows, bar, now) {
  const sec = BAR_SECONDS[bar];
  return rows.filter((r) => r[0] + sec <= now);
}

const isNone = (x) => x === null || x === undefined;
function cmpTuple(a, b) {
  for (let i = 0; i < a.length; i++) {
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return 0;
}

/** Drop bars with impossible values (non-positive prices, high < low, open/close outside the range, duplicate time).
 *  Never 'fix' a price. Returns rows [t, o, h, l, c, v] sorted by time. */
export function sane(rows) {
  const out = [];
  const seen = new Set();
  for (const r of rows) {
    const [t, o, h, l, c, v] = r;
    if (seen.has(t) || isNone(o) || isNone(h) || isNone(l) || isNone(c)) continue;
    let mn = o; // Python min(o, h, l, c)
    if (h < mn) mn = h;
    if (l < mn) mn = l;
    if (c < mn) mn = c;
    if (mn <= 0 || h < l || !(l <= o && o <= h && l <= c && c <= h)) continue;
    seen.add(t);
    out.push([Math.trunc(t), Number(o), Number(h), Number(l), Number(c), (isNone(v) || v === 0 || v === false) ? 0 : Number(v)]);
  }
  return out.sort(cmpTuple);
}

/** merge(old, new, keep): union by bar time (new wins), sorted, newest `keep` bars; source/fetched/note from `nw`. */
export function merge(old, nw, keep) {
  const rows = new Map();
  for (let i = 0; i < old.t.length; i++) rows.set(old.t[i], [old.o[i], old.h[i], old.l[i], old.c[i], old.v[i]]);
  for (let i = 0; i < nw.t.length; i++) rows.set(nw.t[i], [nw.o[i], nw.h[i], nw.l[i], nw.c[i], nw.v[i]]);
  const b = makeBars({ symbol: nw.symbol, bar: nw.bar, source: nw.source, fetched: nw.fetched, note: nw.note });
  const ts = [...rows.keys()].sort((x, y) => x - y).slice(-keep);
  for (const t of ts) {
    const [o, h, l, c, v] = rows.get(t);
    b.t.push(t); b.o.push(o); b.h.push(h); b.l.push(l); b.c.push(c); b.v.push(v);
  }
  return b;
}

// ---------------- demo (simulated) bars ----------------
export const DEMO_START = Object.freeze({
  'BTC-USD': 64000, 'ETH-USD': 2500, 'SOL-USD': 150, 'DOGE-USD': 0.12, AAPL: 225, NVDA: 130,
  MSFT: 420, JPM: 220, TSLA: 240, AMZN: 185,
});

export const DEMO_ANCHOR = Object.freeze({
  '1m': '2026-09-01', '5m': '2026-06-01', '15m': '2026-03-01', '30m': '2026-01-01', '1h': '2021-01-01',
  '4h': '2018-01-01', '1d': '2012-01-01',
});

/** demo_bars: simulated prices for demos and tests. Labelled DEMO everywhere. Not a forecast, not real data.
 *  The walk starts at a fixed anchor date and runs forward, so the same symbol always shows the same history
 *  no matter how many bars are requested. Same RNG stream and formulas as Python (see pyrandom.mjs for the
 *  last-bit caveat on gauss / exp). */
export function demoBars(symbol, bar = '1h', n = 1500, seed = null, now = null) {
  const sec = BAR_SECONDS[bar];
  if (sec === undefined) throw new RangeError(`unknown bar size '${bar}'`);
  now = now || nowSec();
  let defaultSeed = 0;
  for (const ch of symbol + bar) defaultSeed += ch.codePointAt(0);
  const rng = new PyRandom(seed !== null && seed !== undefined ? seed : defaultSeed);
  const kind = marketKind(symbol);
  const fallbackStart = rng.uniform(5, 300); // Python evaluates dict.get's default first, so this draw always happens
  let price = Object.hasOwn(DEMO_START, symbol) ? DEMO_START[symbol] : fallbackStart;
  const baseVol = { crypto: 0.0075, stock: 0.0045 }[kind] * Math.sqrt(sec / 3600);
  const end = Math.trunc(pyFloorDiv(now, sec) * sec);
  let t = Math.floor((dateToDays(DEMO_ANCHOR[bar] ?? '2021-01-01') * 86400) / sec) * sec;
  t = Math.min(t, end - (n + 1) * sec * (kind === 'stock' ? 8 : 1)); // reach further back if more bars are asked for
  const b = makeBars({ symbol, bar, source: 'DEMO (simulated)', fetched: now, note: 'Simulated prices for demo/testing. NOT real.' });
  const bv2 = baseVol * baseVol;
  let vr = bv2;
  const drift = 0.00002;
  while (t + sec <= end) {
    if (kind === 'stock') {
      const p = localParts(t, ET);
      const hm = p.hour * 100 + p.minute;
      if (!(p.weekday < 5 && !NYSE_HOLIDAYS.has(p.date) && hm >= 930 && hm < 1600)) { t += sec; continue; }
    }
    const shock = rng.gauss(0, 1);
    vr = 0.05 * bv2 + 0.85 * vr + 0.10 * (shock * shock) * vr; // GARCH-like clustering
    const sd = Math.sqrt(vr);
    const o = price;
    const c = o * Math.exp(drift + sd * shock);
    const h = (c > o ? c : o) * (1 + Math.abs(rng.gauss(0, sd * 0.6)));
    const l = (c < o ? c : o) * (1 - Math.abs(rng.gauss(0, sd * 0.6)));
    const vol = rng.uniform(0.6, 1.4) * (1 + 6 * Math.abs(shock) * sd / baseVol / 3) * 1000;
    b.t.push(t); b.o.push(o); b.h.push(h); b.l.push(l); b.c.push(c); b.v.push(vol);
    price = c; t += sec;
  }
  if (Object.hasOwn(DEMO_START, symbol) && b.c.length) { // keep demo prices near a familiar level (still simulated)
    const k = DEMO_START[symbol] / b.c[b.c.length - 1];
    for (const key of ['o', 'h', 'l', 'c']) b[key] = b[key].map((x) => x * k);
  }
  return b.c.length > n ? sliceBars(b, -n) : b;
}
