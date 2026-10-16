// tz.mjs - time zones and calendar dates for the engine (Intl-based, zero dependencies).
//
// Every time-zone conversion in the JS engine goes through this file, so ET / MT handling is the same
// everywhere and matches Python's zoneinfo (fold=0 rules for DST gaps and overlaps).
//
// Python -> JS name map
//   ZoneInfo("America/New_York") / ZoneInfo("America/Edmonton") / timezone.utc -> ET / MT / UTC
//   datetime.fromtimestamp(t, tz)                         -> localParts(t, tz)
//   datetime.fromtimestamp(t, tz).date().isoformat()      -> localDate(t, tz)
//   dt.utcoffset().total_seconds()                        -> utcOffset(t, tz)
//   datetime(y, m, d, H, M, S, tzinfo=tz[, fold]).timestamp() -> zonedEpoch(y, m, d, H, M, S, tz[, fold])
//   dt.replace(hour=H, minute=M, second=0, microsecond=0) -> replaceTime(t, tz, H, M, 0)
//   dt.isoformat(timespec=...)                            -> isoLocal(t, tz, timespec)
//   datetime.fromtimestamp(t, timezone.utc).isoformat()   -> isoUTC(t, timespec)
//   aware_dt + timedelta(seconds=s)  (wall-clock add)     -> wallAdd(t, tz, s)
//   datetime.fromisoformat(s).timestamp()                 -> parseIso(s, tzForNaive)
//   date.fromisoformat(s).weekday()                       -> weekdayOf(s)        (Monday = 0)
//   date.fromisoformat(s) + timedelta(days=n)             -> addDays(s, n)
//   (date.fromisoformat(b) - date.fromisoformat(a)).days  -> daysBetween(a, b)
//   date.fromisoformat(s).isocalendar()                   -> isoCalendar(s)
//   time.time()                                           -> nowSec()
//
// Times are epoch SECONDS (numbers, may have a fraction). Dates are 'YYYY-MM-DD' strings.

export const ET = 'America/New_York';
export const MT = 'America/Edmonton';
export const UTC = 'UTC';

const DAY = 86400;

/** Python time.time(): current epoch seconds (float). */
export const nowSec = () => Date.now() / 1000;

// ---------------- pure calendar math (proleptic Gregorian, like Python's date) ----------------

/** Days since 1970-01-01 for a civil date (month may overflow; day may overflow). */
export function daysFromCivil(year, month, day) {
  // normalise the month first (floor division, like Python)
  const m0 = month - 1;
  const y = year + Math.floor(m0 / 12);
  const m = m0 - 12 * Math.floor(m0 / 12) + 1;
  // Howard Hinnant's algorithm, for day 1 of the month, then add the days
  const yy = m <= 2 ? y - 1 : y;
  const era = Math.floor(yy / 400);
  const yoe = yy - era * 400;
  const mp = (m + 9) % 12;
  const doy = Math.floor((153 * mp + 2) / 5);
  const doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy;
  return era * 146097 + doe - 719468 + (day - 1);
}

/** Inverse of daysFromCivil: {year, month, day}. */
export function civilFromDays(n) {
  const z = n + 719468;
  const era = Math.floor(z / 146097);
  const doe = z - era * 146097;
  const yoe = Math.floor((doe - Math.floor(doe / 1460) + Math.floor(doe / 36524) - Math.floor(doe / 146096)) / 365);
  const doy = doe - (365 * yoe + Math.floor(yoe / 4) - Math.floor(yoe / 100));
  const mp = Math.floor((5 * doy + 2) / 153);
  const day = doy - Math.floor((153 * mp + 2) / 5) + 1;
  const month = mp < 10 ? mp + 3 : mp - 9;
  return { year: yoe + era * 400 + (month <= 2 ? 1 : 0), month, day };
}

const pad2 = (x) => (x < 10 ? '0' : '') + x;
const pad4 = (y) => (y < 0 ? '-' + String(-y).padStart(4, '0') : String(y).padStart(4, '0'));

/** 'YYYY-MM-DD' for a civil date. */
export function fmtDate(year, month, day) {
  return `${pad4(year)}-${pad2(month)}-${pad2(day)}`;
}

/** Parse 'YYYY-MM-DD' (Python date.fromisoformat) -> {year, month, day}. Throws on bad input. */
export function parseDate(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (!m) throw new RangeError(`Invalid isoformat string: '${s}'`);
  const year = +m[1], month = +m[2], day = +m[3];
  const back = civilFromDays(daysFromCivil(year, month, day));
  if (month < 1 || month > 12 || back.month !== month || back.day !== day) throw new RangeError(`day is out of range for month: '${s}'`);
  return { year, month, day };
}

/** Days since 1970-01-01 for a 'YYYY-MM-DD' string. */
export function dateToDays(s) {
  const { year, month, day } = parseDate(s);
  return daysFromCivil(year, month, day);
}

/** 'YYYY-MM-DD' for a day number (days since 1970-01-01). */
export function daysToDate(n) {
  const { year, month, day } = civilFromDays(n);
  return fmtDate(year, month, day);
}

/** Python date.weekday(): Monday = 0 ... Sunday = 6. */
export function weekdayOf(s) {
  return weekdayFromDays(dateToDays(s));
}

function weekdayFromDays(n) {
  // 1970-01-01 was a Thursday (weekday 3)
  return (((n + 3) % 7) + 7) % 7;
}

/** date + timedelta(days=n) -> 'YYYY-MM-DD'. */
export function addDays(s, n) {
  return daysToDate(dateToDays(s) + n);
}

/** (date b - date a).days */
export function daysBetween(a, b) {
  return dateToDays(b) - dateToDays(a);
}

/** Python date.isocalendar() -> {year, week, weekday (1 = Monday ... 7 = Sunday)}. */
export function isoCalendar(s) {
  const n = dateToDays(s);
  const wd = weekdayFromDays(n); // Mon=0
  const thursday = n - wd + 3; // the ISO year is the year of this week's Thursday
  const year = civilFromDays(thursday).year;
  const week1Thursday = (() => {
    const jan4 = daysFromCivil(year, 1, 4);
    return jan4 - weekdayFromDays(jan4) + 3;
  })();
  return { year, week: Math.floor((thursday - week1Thursday) / 7) + 1, weekday: wd + 1 };
}

// ---------------- UTC offsets via Intl (cached) ----------------

const fmtCache = new Map();
function formatter(tz) {
  let f = fmtCache.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric',
      hour: 'numeric', minute: 'numeric', second: 'numeric',
    });
    fmtCache.set(tz, f);
  }
  return f;
}

/** Offset (local - UTC, seconds) at an integer epoch second, straight from Intl. */
function rawOffset(tz, s) {
  let y = 0, mo = 0, d = 0, h = 0, mi = 0, se = 0;
  for (const p of formatter(tz).formatToParts(new Date(s * 1000))) {
    switch (p.type) {
      case 'year': y = +p.value; break;
      case 'month': mo = +p.value; break;
      case 'day': d = +p.value; break;
      case 'hour': h = +p.value; break;
      case 'minute': mi = +p.value; break;
      case 'second': se = +p.value; break;
      default: break;
    }
  }
  return daysFromCivil(y, mo, d) * DAY + h * 3600 + mi * 60 + se - s;
}

// Per-zone caches: offset for a whole UTC day when it does not change inside that day; otherwise per hour.
const dayCache = new Map();
const hourCache = new Map();
const CACHE_MAX = 400000;

function cacheFor(map, tz) {
  let c = map.get(tz);
  if (!c) { c = new Map(); map.set(tz, c); }
  if (c.size > CACHE_MAX) c.clear();
  return c;
}

/** UTC offset in seconds (local - UTC) of zone `tz` at epoch second `t`. */
export function utcOffset(t, tz) {
  if (tz === UTC) return 0;
  const s = Math.floor(t);
  const dayIdx = Math.floor(s / DAY);
  const dc = cacheFor(dayCache, tz);
  let o = dc.get(dayIdx);
  if (o !== undefined) {
    if (o !== null) return o;
  } else {
    const a = rawOffset(tz, dayIdx * DAY), b = rawOffset(tz, dayIdx * DAY + DAY - 1);
    if (a === b) { dc.set(dayIdx, a); return a; }
    dc.set(dayIdx, null); // a transition happens inside this UTC day: fall back to hourly buckets
  }
  const hourIdx = Math.floor(s / 3600);
  const hc = cacheFor(hourCache, tz);
  o = hc.get(hourIdx);
  if (o !== undefined && o !== null) return o;
  if (o === undefined) {
    const a = rawOffset(tz, hourIdx * 3600), b = rawOffset(tz, hourIdx * 3600 + 3599);
    if (a === b) { hc.set(hourIdx, a); return a; }
    hc.set(hourIdx, null);
  }
  return rawOffset(tz, s);
}

// C round(): half away from zero.
const cRound = (x) => (x < 0 ? -Math.round(-x) : Math.round(x));

// Python rounds a float timestamp to microseconds with round-half-even (after modf).
function splitMicros(t) {
  let ip = Math.trunc(t);
  const fp = (t - ip) * 1e6;
  let us = cRound(fp);
  if (Math.abs(fp - us) === 0.5) us = 2 * cRound(fp / 2); // tie -> even
  if (us >= 1e6) { us -= 1e6; ip += 1; } else if (us < 0) { us += 1e6; ip -= 1; }
  return [ip, us];
}

/** Resolve a wall-clock time (seconds since 1970-01-01 as if UTC) to epoch seconds, zoneinfo style:
 *  fold=0 -> in an overlap the first occurrence, in a gap the offset from BEFORE the transition;
 *  fold=1 -> the second occurrence / the offset from AFTER the transition. */
function resolveWall(W, tz, fold) {
  if (tz === UTC) return W;
  const oB = utcOffset(W - DAY, tz), oA = utcOffset(W + DAY, tz);
  if (oB === oA && utcOffset(W - oB, tz) === oB) return W - oB;
  const e1 = W - oB, e2 = W - oA;
  const v1 = utcOffset(e1, tz) === oB, v2 = utcOffset(e2, tz) === oA;
  if (v1 && v2) return fold ? e2 : e1; // ambiguous (clocks went back)
  if (v1) return e1;
  if (v2) return e2;
  return fold ? e2 : e1; // gap (clocks went forward)
}

/** Python datetime.fromtimestamp(t, tz) as plain fields:
 *  {year, month, day, hour, minute, second, microsecond, weekday (Mon=0..Sun=6), date:'YYYY-MM-DD', offset (s), fold}. */
export function localParts(t, tz) {
  if (t instanceof Date) t = t.getTime() / 1000;
  const [ip, us] = splitMicros(t);
  const off = utcOffset(ip, tz);
  const W = ip + off;
  const days = Math.floor(W / DAY);
  const sod = W - days * DAY;
  const { year, month, day } = civilFromDays(days);
  const hour = Math.floor(sod / 3600), minute = Math.floor((sod % 3600) / 60), second = sod % 60;
  let fold = 0;
  if (tz !== UTC && resolveWall(W, tz, 0) !== ip) fold = 1;
  return {
    year, month, day, hour, minute, second, microsecond: us, weekday: weekdayFromDays(days),
    date: fmtDate(year, month, day), offset: off, fold,
  };
}

/** Python datetime.fromtimestamp(t, tz).date().isoformat() (fast path). */
export function localDate(t, tz) {
  const ip = splitMicros(t)[0];
  return daysToDate(Math.floor((ip + utcOffset(ip, tz)) / DAY));
}

/** Epoch seconds of a wall-clock time in `tz` (Python datetime(..., tzinfo=tz, fold=fold).timestamp()).
 *  Fields may overflow (e.g. day 32 or hour 25) and are normalised like wall-clock arithmetic. */
export function zonedEpoch(year, month, day, hour = 0, minute = 0, second = 0, tz = UTC, fold = 0) {
  const W = (daysFromCivil(year, month, 1) + day - 1) * DAY + hour * 3600 + minute * 60;
  const whole = Math.floor(second);
  return resolveWall(W + whole, tz, fold) + (second - whole);
}

/** aware datetime + timedelta(seconds=delta): wall-clock arithmetic in `tz`, result re-resolved with fold=0. */
export function wallAdd(t, tz, delta) {
  const p = localParts(t, tz);
  const frac = p.microsecond / 1e6;
  return zonedEpoch(p.year, p.month, p.day, p.hour, p.minute, p.second + frac + delta, tz, 0);
}

/** dt.replace(hour, minute, second, microsecond=0) for the local date of `t` in `tz` (keeps fold, like Python). */
export function replaceTime(t, tz, hour, minute = 0, second = 0) {
  const p = localParts(t, tz);
  return zonedEpoch(p.year, p.month, p.day, hour, minute, second, tz, p.fold);
}

function fmtOffset(off) {
  const sign = off < 0 ? '-' : '+';
  const a = Math.abs(off);
  const hh = Math.floor(a / 3600), mm = Math.floor((a % 3600) / 60), ss = a % 60;
  return `${sign}${pad2(hh)}:${pad2(mm)}${ss ? ':' + pad2(ss) : ''}`;
}

/** Python datetime.fromtimestamp(t, tz).isoformat(timespec=...) e.g. '2026-10-14T14:00:00-06:00'.
 *  timespec: 'auto' | 'hours' | 'minutes' | 'seconds' | 'milliseconds' | 'microseconds'. */
export function isoLocal(t, tz, timespec = 'auto') {
  const p = localParts(t, tz);
  let time;
  switch (timespec) {
    case 'auto': time = `${pad2(p.hour)}:${pad2(p.minute)}:${pad2(p.second)}${p.microsecond ? '.' + String(p.microsecond).padStart(6, '0') : ''}`; break;
    case 'hours': time = pad2(p.hour); break;
    case 'minutes': time = `${pad2(p.hour)}:${pad2(p.minute)}`; break;
    case 'seconds': time = `${pad2(p.hour)}:${pad2(p.minute)}:${pad2(p.second)}`; break;
    case 'milliseconds': time = `${pad2(p.hour)}:${pad2(p.minute)}:${pad2(p.second)}.${String(Math.floor(p.microsecond / 1000)).padStart(3, '0')}`; break;
    case 'microseconds': time = `${pad2(p.hour)}:${pad2(p.minute)}:${pad2(p.second)}.${String(p.microsecond).padStart(6, '0')}`; break;
    default: throw new RangeError(`Unknown timespec value: ${timespec}`);
  }
  return `${p.date}T${time}${fmtOffset(p.offset)}`;
}

/** Python datetime.fromtimestamp(t, timezone.utc).isoformat(timespec=...) e.g. '2026-10-14T20:00:00+00:00'. */
export function isoUTC(t, timespec = 'auto') {
  return isoLocal(t, UTC, timespec);
}

/** Python datetime.fromisoformat(s).timestamp(). Accepts 'YYYY-MM-DD', 'YYYY-MM-DD[T ]HH[:MM[:SS[.ffffff]]]'
 *  with an optional 'Z' / '+HH:MM[:SS]' / '+HHMM' offset. A string without an offset is read as wall time
 *  in `tzForNaive` (Python would use the computer's local zone; here it is explicit, default UTC). */
export function parseIso(s, tzForNaive = UTC) {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2})(?::?(\d{2})(?::?(\d{2})(?:[.,](\d{1,6}))?)?)?)?(?:(Z)|([+-])(\d{2}):?(\d{2})(?::?(\d{2}))?)?$/.exec(String(s).trim());
  if (!m) throw new RangeError(`Invalid isoformat string: '${s}'`);
  parseDate(`${m[1]}-${m[2]}-${m[3]}`); // validates the date
  const hour = +(m[4] || 0), minute = +(m[5] || 0), second = +(m[6] || 0);
  const frac = m[7] ? +(m[7].padEnd(6, '0')) / 1e6 : 0;
  if (hour > 23 || minute > 59 || second > 59) throw new RangeError(`Invalid isoformat string: '${s}'`);
  if (m[8] || m[9]) {
    const off = m[8] ? 0 : (m[9] === '-' ? -1 : 1) * (+m[10] * 3600 + +m[11] * 60 + +(m[12] || 0));
    return (daysFromCivil(+m[1], +m[2], +m[3]) * DAY + hour * 3600 + minute * 60 + second - off) + frac;
  }
  return zonedEpoch(+m[1], +m[2], +m[3], hour, minute, second + frac, tzForNaive, 0);
}
