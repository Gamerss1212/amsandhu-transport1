// pyrandom.mjs - a port of CPython 3.13 random.Random (MT19937), so JS draws the same numbers as Python.
//
// Bit-exact with CPython for: seeding from an int (or BigInt), random(), getrandbits(), _randbelow(),
// randrange(), randint(), choice(), shuffle(), sample() (both the pool and the set branch), uniform().
// gauss() / normalvariate() use the same algorithm and the same uniform draws, but they call log/cos/sin;
// V8's Math functions and the C library under Python can differ in the last bit (about 1 call in 15-30),
// so those two match Python to ~1e-15 relative, not always bit for bit.
//
// Python -> JS name map
//   random.Random(seed)            -> new PyRandom(seed)
//   r.seed(a)                      -> r.seed(a)
//   r.random() / r.uniform(a, b)   -> r.random() / r.uniform(a, b)
//   r.getrandbits(k)               -> r.getrandbits(k) (Number, k <= 53)  or  r.getrandbitsBig(k) (BigInt, any k)
//   r._randbelow(n)                -> r._randbelow(n)
//   r.randrange(a[, b[, step]])    -> r.randrange(a, b, step)
//   r.randint(a, b) / r.choice(seq) / r.shuffle(x) -> same names
//   r.sample(population, k)        -> r.sample(population, k[, counts])  population: array, string or pyRange(...)
//   range(start, stop, step)       -> pyRange(start, stop, step)
//   r.gauss(mu, sigma) / r.normalvariate(mu, sigma) -> same names (gauss_next cache kept in r.gauss_next)
//   r.getstate() / r.setstate(s)   -> same names (JS-shaped state object)

const N = 624;
const M = 397;
const MATRIX_A = 0x9908b0df;
const UPPER_MASK = 0x80000000;
const LOWER_MASK = 0x7fffffff;

const TWOPI = 2.0 * Math.PI;                 // 6.283185307179586, same double as Python's TWOPI
const NV_MAGICCONST = 1.7155277699214135;    // Python: 4 * exp(-0.5) / sqrt(2.0), copied as the exact double

/** Python range(start, stop, step) as a sequence object usable by PyRandom.sample / choice. */
export function pyRange(start, stop, step = 1) {
  if (stop === undefined || stop === null) { stop = start; start = 0; }
  if (step === 0) throw new RangeError('range() arg 3 must not be zero');
  let length = 0;
  if (step > 0 && stop > start) length = Math.floor((stop - start + step - 1) / step);
  else if (step < 0 && stop < start) length = Math.floor((start - stop - step - 1) / -step);
  return { start, stop, step, length, isRange: true, at(i) { return start + i * step; } };
}

const seqLen = (s) => s.length;
const seqAt = (s, i) => (s && s.isRange ? s.at(i) : s[i]);

function bitLength(n) {
  if (n < 0x100000000) return 32 - Math.clz32(n);
  let k = Math.floor(Math.log2(n)) + 1;
  while (2 ** k <= n) k++;
  while (k > 0 && 2 ** (k - 1) > n) k--;
  return k;
}

function checkInt(x, name = 'value') {
  if (typeof x === 'bigint') return;
  if (!Number.isInteger(x)) throw new TypeError(`${name} must be an integer, got ${x}`);
}

export class PyRandom {
  /** random.Random(seed). seed: integer Number, BigInt, or null/undefined (seeded from the OS). */
  constructor(seed = null) {
    this.mt = new Uint32Array(N);
    this.index = N + 1;
    this.gauss_next = null;
    this.seed(seed);
  }

  /** Random.seed(a): ints are seeded with all their bits (abs value split into 32-bit words), like CPython. */
  seed(a = null) {
    if (a === null || a === undefined) {
      const key = new Uint32Array(N);
      globalThis.crypto.getRandomValues(key);
      this._initByArray(Array.from(key));
    } else {
      if (typeof a === 'number') {
        if (!Number.isInteger(a)) throw new TypeError('PyRandom: only integer seeds are supported (Python hashes floats)');
        a = BigInt(a);
      }
      if (typeof a !== 'bigint') throw new TypeError('PyRandom: seed must be an integer, BigInt or null');
      let n = a < 0n ? -a : a;
      const key = [];
      if (n === 0n) key.push(0);
      while (n > 0n) { key.push(Number(n & 0xffffffffn)); n >>= 32n; }
      this._initByArray(key);
    }
    this.gauss_next = null;
  }

  _initGenrand(s) {
    const mt = this.mt;
    mt[0] = s >>> 0;
    for (let i = 1; i < N; i++) {
      const prev = mt[i - 1] ^ (mt[i - 1] >>> 30);
      mt[i] = (Math.imul(1812433253, prev) >>> 0) + i;
    }
    this.index = N;
  }

  _initByArray(key) {
    const mt = this.mt;
    this._initGenrand(19650218);
    let i = 1, j = 0;
    const len = key.length;
    for (let k = N > len ? N : len; k; k--) {
      const prev = mt[i - 1] ^ (mt[i - 1] >>> 30);
      mt[i] = ((mt[i] ^ Math.imul(prev, 1664525)) >>> 0) + key[j] + j; // Uint32Array store wraps mod 2^32
      i++; j++;
      if (i >= N) { mt[0] = mt[N - 1]; i = 1; }
      if (j >= len) j = 0;
    }
    for (let k = N - 1; k; k--) {
      const prev = mt[i - 1] ^ (mt[i - 1] >>> 30);
      mt[i] = ((mt[i] ^ Math.imul(prev, 1566083941)) >>> 0) - i;
      i++;
      if (i >= N) { mt[0] = mt[N - 1]; i = 1; }
    }
    mt[0] = 0x80000000;
  }

  /** genrand_uint32(): next raw 32-bit output. */
  _genrandUint32() {
    const mt = this.mt;
    if (this.index >= N) {
      let kk = 0, y;
      for (; kk < N - M; kk++) {
        y = (mt[kk] & UPPER_MASK) | (mt[kk + 1] & LOWER_MASK);
        mt[kk] = mt[kk + M] ^ (y >>> 1) ^ (y & 1 ? MATRIX_A : 0);
      }
      for (; kk < N - 1; kk++) {
        y = (mt[kk] & UPPER_MASK) | (mt[kk + 1] & LOWER_MASK);
        mt[kk] = mt[kk + (M - N)] ^ (y >>> 1) ^ (y & 1 ? MATRIX_A : 0);
      }
      y = (mt[N - 1] & UPPER_MASK) | (mt[0] & LOWER_MASK);
      mt[N - 1] = mt[M - 1] ^ (y >>> 1) ^ (y & 1 ? MATRIX_A : 0);
      this.index = 0;
    }
    let y = mt[this.index++];
    y ^= y >>> 11;
    y ^= (y << 7) & 0x9d2c5680;
    y ^= (y << 15) & 0xefc60000;
    y ^= y >>> 18;
    return y >>> 0;
  }

  /** random(): float in [0, 1) with 53 random bits. */
  random() {
    const a = this._genrandUint32() >>> 5, b = this._genrandUint32() >>> 6;
    return (a * 67108864.0 + b) * (1.0 / 9007199254740992.0);
  }

  /** getrandbits(k) as a Number (k <= 53). */
  getrandbits(k) {
    checkInt(k, 'k');
    if (k < 0) throw new RangeError('number of bits must be non-negative');
    if (k === 0) return 0;
    if (k <= 32) return this._genrandUint32() >>> (32 - k);
    if (k > 53) throw new RangeError('getrandbits(k > 53) does not fit a Number; use getrandbitsBig');
    const lo = this._genrandUint32();
    const hi = this._genrandUint32() >>> (64 - k);
    return hi * 4294967296 + lo;
  }

  /** getrandbits(k) as a BigInt (any k): 32-bit words from least to most significant, like CPython. */
  getrandbitsBig(k) {
    checkInt(k, 'k');
    k = Number(k);
    if (k < 0) throw new RangeError('number of bits must be non-negative');
    let out = 0n, shift = 0n;
    for (let left = k; left > 0; left -= 32, shift += 32n) {
      let r = this._genrandUint32();
      if (left < 32) r >>>= 32 - left;
      out |= BigInt(r) << shift;
    }
    return out;
  }

  /** _randbelow(n): int in [0, n) by rejection sampling on n.bit_length() bits. n: Number or BigInt, n > 0. */
  _randbelow(n) {
    if (typeof n === 'bigint' || n > Number.MAX_SAFE_INTEGER) {
      const big = BigInt(n);
      if (big <= 0n) throw new RangeError('_randbelow needs n > 0');
      const k = big.toString(2).length;
      let r = this.getrandbitsBig(k);
      while (r >= big) r = this.getrandbitsBig(k);
      return typeof n === 'bigint' ? r : Number(r);
    }
    if (!(n > 0)) throw new RangeError('_randbelow needs n > 0'); // Python would loop forever on 0
    const k = bitLength(n);
    let r = this.getrandbits(k);
    while (r >= n) r = this.getrandbits(k);
    return r;
  }

  /** randrange(start, stop?, step?) exactly as CPython 3.13. */
  randrange(start, stop = null, step = 1) {
    checkInt(start, 'start');
    if (stop === null || stop === undefined) {
      if (step !== 1) throw new TypeError('Missing a non-None stop argument');
      if (start > 0) return this._randbelow(start);
      throw new RangeError('empty range for randrange()');
    }
    checkInt(stop, 'stop'); checkInt(step, 'step');
    const width = stop - start;
    if (step === 1) {
      if (width > 0) return start + this._randbelow(width);
      throw new RangeError(`empty range in randrange(${start}, ${stop})`);
    }
    let n;
    if (step > 0) n = Math.floor((width + step - 1) / step);
    else if (step < 0) n = Math.floor((width + step + 1) / step);
    else throw new RangeError('zero step for randrange()');
    if (n <= 0) throw new RangeError(`empty range in randrange(${start}, ${stop}, ${step})`);
    return start + step * this._randbelow(n);
  }

  /** randint(a, b): int in [a, b]. */
  randint(a, b) {
    return this.randrange(a, b + 1);
  }

  /** choice(seq): one element of a non-empty array / string / pyRange. */
  choice(seq) {
    const n = seqLen(seq);
    if (!n) throw new RangeError('Cannot choose from an empty sequence');
    return seqAt(seq, this._randbelow(n));
  }

  /** shuffle(x): in-place Fisher-Yates, same swaps as CPython. Returns undefined like Python's None. */
  shuffle(x) {
    for (let i = x.length - 1; i > 0; i--) {
      const j = this._randbelow(i + 1);
      const tmp = x[i]; x[i] = x[j]; x[j] = tmp;
    }
  }

  /** sample(population, k, counts?): k unique picks, CPython's algorithm (pool for small populations, set otherwise). */
  sample(population, k, counts = null) {
    if (population === null || population === undefined || typeof population.length !== 'number') {
      throw new TypeError('Population must be a sequence.  For dicts or sets, use sorted(d).');
    }
    const n = seqLen(population);
    if (counts !== null && counts !== undefined) {
      const cum = [];
      let acc = 0;
      for (const c of counts) { acc += c; cum.push(acc); }
      if (cum.length !== n) throw new RangeError('The number of counts does not match the population');
      const total = cum.length ? cum.pop() : 0;
      if (!Number.isInteger(total)) throw new TypeError('Counts must be integers');
      if (total < 0) throw new RangeError('Counts must be non-negative');
      const selections = this.sample(pyRange(total), k);
      return selections.map((s) => {
        let lo = 0, hi = cum.length; // bisect_right(cum, s)
        while (lo < hi) { const mid = (lo + hi) >> 1; if (s < cum[mid]) hi = mid; else lo = mid + 1; }
        return seqAt(population, lo);
      });
    }
    checkInt(k, 'k');
    if (!(k >= 0 && k <= n)) throw new RangeError('Sample larger than population or is negative');
    const result = new Array(k);
    let setsize = 21;
    if (k > 5) setsize += 4 ** Math.ceil(Math.log(k * 3) / Math.log(4));
    if (n <= setsize) {
      const pool = population.isRange ? Array.from({ length: n }, (_, i) => population.at(i)) : Array.from(population);
      for (let i = 0; i < k; i++) {
        const j = this._randbelow(n - i);
        result[i] = pool[j];
        pool[j] = pool[n - i - 1];
      }
    } else {
      const selected = new Set();
      for (let i = 0; i < k; i++) {
        let j = this._randbelow(n);
        while (selected.has(j)) j = this._randbelow(n);
        selected.add(j);
        result[i] = seqAt(population, j);
      }
    }
    return result;
  }

  /** uniform(a, b) = a + (b - a) * random(). */
  uniform(a, b) {
    return a + (b - a) * this.random();
  }

  /** gauss(mu, sigma): Box-Muller with the second value cached in gauss_next (same draw order as CPython). */
  gauss(mu = 0.0, sigma = 1.0) {
    let z = this.gauss_next;
    this.gauss_next = null;
    if (z === null) {
      const x2pi = this.random() * TWOPI;
      const g2rad = Math.sqrt(-2.0 * Math.log(1.0 - this.random()));
      z = Math.cos(x2pi) * g2rad;
      this.gauss_next = Math.sin(x2pi) * g2rad;
    }
    return mu + z * sigma;
  }

  /** normalvariate(mu, sigma): Kinderman-Monahan, as CPython. */
  normalvariate(mu = 0.0, sigma = 1.0) {
    let z;
    for (;;) {
      const u1 = this.random();
      const u2 = 1.0 - this.random();
      z = NV_MAGICCONST * (u1 - 0.5) / u2;
      const zz = z * z / 4.0;
      if (zz <= -Math.log(u2)) break;
    }
    return mu + z * sigma;
  }

  /** getstate(): {version: 3, state: [624 words..., index], gauss_next}. */
  getstate() {
    return { version: 3, state: [...this.mt, this.index], gauss_next: this.gauss_next };
  }

  /** setstate(s): restore a state from getstate() (also accepts Python's (3, tuple, gauss_next) as an array). */
  setstate(s) {
    const st = Array.isArray(s) ? { version: s[0], state: s[1], gauss_next: s[2] } : s;
    if (st.version !== 3 || st.state.length !== N + 1) throw new RangeError('state vector is the wrong size');
    for (let i = 0; i < N; i++) this.mt[i] = st.state[i];
    const idx = st.state[N];
    if (idx < 0 || idx > N) throw new RangeError('invalid state');
    this.index = idx;
    this.gauss_next = st.gauss_next ?? null;
  }
}
