/* Jarvis Trading Portal - client. Every number shown comes from the local server; nothing is invented here.
   Voice: browser speech recognition (Chrome/Edge) + browser speech synthesis. Paper trading only. */
'use strict';
const TOKEN = document.querySelector('meta[name="jarvis-token"]').content;
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const C = { up: css('--up'), down: css('--down'), s3: css('--s3'), ref: css('--ref'), glow: css('--glow'), glow2: css('--glow2'),
  ink: css('--ink'), ink2: css('--ink2'), muted: css('--muted'), good: css('--good'), warn: css('--warn'), crit: css('--crit'), serious: css('--serious') };

async function api(path, body) {
  const r = await fetch(path, { method: body ? 'POST' : 'GET', headers: { 'X-Jarvis-Token': TOKEN, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({ error: 'bad response' }));
  if (!r.ok && !j.error) j.error = `HTTP ${r.status}`;
  return j;
}
const fmt = {
  usd: x => x == null ? '—' : (x < 0 ? '-' : '') + '$' + Math.abs(x).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  sgn: x => x == null ? '—' : (x >= 0 ? '+' : '-') + '$' + Math.abs(x).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  pct: (x, d = 2) => x == null ? '—' : (x >= 0 ? '+' : '') + Number(x).toFixed(d) + '%',
  px: x => { if (x == null || isNaN(x)) return '—'; const a = Math.abs(x);
    return a >= 1000 ? x.toLocaleString('en-US', { maximumFractionDigits: 2 }) : a >= 1 ? x.toFixed(a >= 100 ? 2 : 3) : x.toPrecision(4); },
  r: x => x == null ? '—' : (x >= 0 ? '+' : '') + Number(x).toFixed(2) + 'R',
  num: (x, d = 2) => x == null ? '—' : Number(x).toFixed(d),
  dur: s => { s = Math.max(0, s | 0); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; },
  mt: t => new Date(t * 1000).toLocaleString('en-US', { timeZone: 'America/Edmonton', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }),
  ago: iso => { if (!iso) return '—'; const s = (Date.now() - new Date(iso).getTime()) / 1000; return s < 90 ? `${s | 0}s ago` : s < 5400 ? `${(s / 60) | 0}m ago` : `${(s / 3600).toFixed(1)}h ago`; },
};
function hidpi(cv) { const r = cv.getBoundingClientRect(), d = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(r.width * d)), h = Math.max(1, Math.round(r.height * d));
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; } const g = cv.getContext('2d'); g.setTransform(d, 0, 0, d, 0, 0); return [g, r.width, r.height]; }

const S = { st: null, lib: null, sym: 'BTC-USD', bar: '1h', strat: '', uiSince: 0, noteSince: -1, session: null, sheet: null, chart: null, families: null };
try { S.session = localStorage.getItem('jarvis_session'); S.sym = localStorage.getItem('jarvis_sym') || 'BTC-USD'; } catch (e) { }

/* ============================ background + reactor ============================ */
const bg = $('#bg'); const parts = Array.from({ length: 70 }, () => ({ x: Math.random(), y: Math.random(), v: .2 + Math.random() * .6, r: Math.random() * 1.4 + .3 }));
let mouse = { x: .5, y: .5 };
addEventListener('mousemove', e => { mouse = { x: e.clientX / innerWidth, y: e.clientY / innerHeight }; });
function drawBg(t) {
  const [g, w, h] = hidpi(bg); g.clearRect(0, 0, w, h);
  const grd = g.createRadialGradient(w / 2, h * .42, 0, w / 2, h * .42, Math.max(w, h) * .7);
  grd.addColorStop(0, 'rgba(14,52,110,.55)'); grd.addColorStop(1, 'rgba(2,10,26,0)'); g.fillStyle = grd; g.fillRect(0, 0, w, h);
  g.strokeStyle = 'rgba(64,160,255,.05)'; g.lineWidth = 1;
  for (let x = 0; x < w; x += 46) { g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke(); }
  for (let y = 0; y < h; y += 46) { g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }
  for (const p of parts) { p.y -= p.v * .0006; if (p.y < 0) { p.y = 1; p.x = Math.random(); }
    const x = (p.x + (mouse.x - .5) * .02 * p.v) * w, y = p.y * h; g.fillStyle = `rgba(143,227,255,${.25 + p.v * .4})`; g.beginPath(); g.arc(x, y, p.r, 0, 7); g.fill(); }
}
const orb = $('#orb'), bloom = $('#bloom');
const R = { level: 0, energy: .35, spin: 0 };
function drawOrb(t) {
  const [g, w, h] = hidpi(orb); const cx = w / 2, cy = h / 2, rad = Math.min(w, h) / 2 * .94; g.clearRect(0, 0, w, h);
  const st = V.state, halted = document.body.classList.contains('halted');
  const target = st === 'SPEAKING' ? .9 : st === 'THINKING' || st === 'USING TOOLS' ? .75 : st === 'LISTENING' || st === 'FOLLOW-UP' ? .6 + R.level * .8 : .35;
  R.energy += (target - R.energy) * .06; R.spin += .004 + R.energy * .012;
  const col = a => halted ? `rgba(255,91,110,${a})` : `rgba(46,168,255,${a})`, col2 = a => halted ? `rgba(255,170,180,${a})` : `rgba(143,227,255,${a})`;
  // degree scale
  g.save(); g.translate(cx, cy);
  for (let i = 0; i < 120; i++) { const a = i / 120 * Math.PI * 2; const r1 = rad * .97, r2 = rad * (i % 10 ? .94 : .9);
    g.strokeStyle = col(i % 10 ? .25 : .6); g.lineWidth = 1; g.beginPath(); g.moveTo(Math.cos(a) * r1, Math.sin(a) * r1); g.lineTo(Math.cos(a) * r2, Math.sin(a) * r2); g.stroke(); }
  // outer segmented ring
  g.rotate(R.spin * .5);
  for (let i = 0; i < 12; i++) { const a0 = i / 12 * Math.PI * 2 + .05, a1 = (i + 1) / 12 * Math.PI * 2 - .05;
    g.strokeStyle = col(.35 + .4 * Math.max(0, Math.sin(t / 600 + i))); g.lineWidth = rad * .07; g.beginPath(); g.arc(0, 0, rad * .8, a0, a1); g.stroke(); }
  g.rotate(-R.spin * 1.4);
  for (let i = 0; i < 8; i++) { const a0 = i / 8 * Math.PI * 2 + .12, a1 = (i + 1) / 8 * Math.PI * 2 - .12;
    g.strokeStyle = col2(.5 + .3 * R.energy); g.lineWidth = rad * .1; g.beginPath(); g.arc(0, 0, rad * .6, a0, a1); g.stroke();
    g.strokeStyle = 'rgba(2,10,26,.9)'; g.lineWidth = 2; g.beginPath(); g.arc(0, 0, rad * .6, a0 + .02, a1 - .02); g.stroke(); }
  g.rotate(R.spin * 2.2);
  g.strokeStyle = col(.7); g.lineWidth = 2; g.setLineDash([4, 7]); g.beginPath(); g.arc(0, 0, rad * .7, 0, 7); g.stroke(); g.setLineDash([]);
  // energy arcs (voice level)
  const lv = Math.min(1, R.level * 2.5);
  for (let k = 0; k < 3; k++) { g.strokeStyle = col2(.25 + .5 * lv); g.lineWidth = 2; const a = t / (400 + k * 170) + k * 2;
    g.beginPath(); g.arc(0, 0, rad * (.45 + k * .04 + lv * .05), a, a + .8 + lv * 1.5); g.stroke(); }
  g.restore();
  // core
  const core = rad * (.3 + .04 * Math.sin(t / 300) * R.energy + lv * .06);
  const cg = g.createRadialGradient(cx, cy, 0, cx, cy, core * 1.6);
  cg.addColorStop(0, halted ? 'rgba(255,240,240,1)' : 'rgba(235,250,255,1)'); cg.addColorStop(.25, col2(.95)); cg.addColorStop(.6, col(.55)); cg.addColorStop(1, col(0));
  g.fillStyle = cg; g.beginPath(); g.arc(cx, cy, core * 1.6, 0, 7); g.fill();
  // hex iris
  g.strokeStyle = 'rgba(2,10,26,.55)'; g.lineWidth = 2; g.beginPath();
  for (let i = 0; i <= 6; i++) { const a = i / 6 * Math.PI * 2 + R.spin; const x = cx + Math.cos(a) * core * .55, y = cy + Math.sin(a) * core * .55; i ? g.lineTo(x, y) : g.moveTo(x, y); }
  g.stroke();
  const [b] = hidpi(bloom); b.clearRect(0, 0, w, h); b.drawImage(orb, 0, 0, w, h);
}
function frame(t) { drawBg(t); drawOrb(t); drawScope(t); drawRadar(t); requestAnimationFrame(frame); }

/* ============================ polling + rendering ============================ */
async function refresh() {
  let st;
  try { st = await api(`/api/state?ui=${S.uiSince}&note=${Math.max(0, S.noteSince)}`); }
  catch (e) { $('#bOnline').className = 'badge crit'; $('#bOnline').innerHTML = '<i></i>STATUS: <b>OFFLINE</b>'; return; }
  if (st.error) { $('#bOnline').className = 'badge crit'; $('#bOnline').innerHTML = `<i></i>STATUS: <b>${esc(st.error)}</b>`; return; }
  S.st = st;
  renderHeader(st); renderTicker(st); renderPortfolio(st); renderWatch(st); renderVol(st); renderRisk(st);
  renderMinis(st); renderFamilies(st); renderMatrix(st); renderTarget(st);
  handleEvents(st);
  if (S.sheet && SHEETS[S.sheet].live) SHEETS[S.sheet].render(true);
}

function renderHeader(st) {
  $('#bOnline').className = 'badge good'; $('#bOnline').innerHTML = '<i></i>STATUS: <b>ONLINE</b>';
  const ny = st.nyse; const b = $('#bNyse');
  b.className = 'badge ' + (ny.open ? 'good' : ny.holiday ? 'warn' : ''); b.innerHTML = `<i></i>NYSE ${ny.open ? 'OPEN' : ny.holiday ? 'HOLIDAY' : 'CLOSED'}${ny.early_close ? ' · EARLY CLOSE' : ''}`;
  const c = $('#bCrypto'); c.className = 'badge ' + (st.weekend ? 'warn' : 'good');
  c.innerHTML = `<i></i>CRYPTO ${st.weekend ? 'WEEKEND · NO ENTRIES' : 'WEEKDAY'}`;
  const d = $('#bData'); d.className = 'badge ' + (st.data_mode === 'demo' ? 'demo warn' : 'good');
  d.innerHTML = `<i></i>DATA: <b>${st.data_mode === 'demo' ? 'DEMO (SIMULATED)' : 'LIVE FEEDS'}</b>`;
  const br = $('#bBrain'); br.className = 'badge ' + (st.brain.ready ? 'good' : 'warn');
  br.innerHTML = `<i></i>AI: <b>${st.brain.ready ? 'ONLINE' : 'OFFLINE MODE'}</b>`;
  br.title = st.brain.ready ? `strong: ${st.brain.models.strong} · fast: ${st.brain.models.fast}${st.brain.web_research ? ' · web research on' : ''}` : st.brain.reason;
  $('#clock').innerHTML = `<b>${esc(st.mt)}</b> · ${esc(st.et)}`;
  document.body.classList.toggle('halted', !!st.halt);
  $('#killState').textContent = st.halt ? 'HALT FLAG ON' : 'KILL SWITCH ARMED';
  $('#killState').style.color = st.halt ? C.crit : '';
}
function renderTicker(st) {
  const items = st.quotes.filter(q => q.ok).map(q => { const p = q.live ?? q.price, ch = q.live_change_pct ?? q.change_pct;
    return `${esc(q.symbol.replace('-USD', ''))} <em class="${ch >= 0 ? 'u' : 'd'}">${fmt.px(p)} ${ch >= 0 ? '▲' : '▼'}${Math.abs(ch).toFixed(2)}%</em>`; });
  const extra = [`FLEET ${st.totals.active ?? '—'} ACTIVE`, `GATE ${(st.totals.gate_passed_roles || []).length}/7 ROLES PASSED`, `PAPER EQUITY ${fmt.usd(st.portfolio.equity)}`,
    `SHADOW TRADES ${st.shadow.closed_trades}`, st.data_mode === 'demo' ? 'DEMO DATA — SIMULATED PRICES' : 'LIVE DATA · DECISIONS USE CLOSED BARS', 'PAPER TRADING ONLY'];
  const html = items.concat(extra.map(x => `${x}<em></em>`)).join(' ');
  if ($('#tickerText').dataset.h !== html) { $('#tickerText').innerHTML = html; $('#tickerText').dataset.h = html; }
}
function healthChecks(st) {
  const cycleAge = st.last_cycle ? (Date.now() - new Date(st.last_cycle).getTime()) / 1000 : 1e9;
  return [['Server', true], ['Fleet cycle fresh', cycleAge < 600], ['No bot errors', !(st.totals.errors > 0)], ['No stale data', !(st.totals.stale_bots > 0)],
    ['AI brain ready', st.brain.ready], ['Kill switch not tripped', !st.halt]];
}
function renderPortfolio(st) {
  const p = st.portfolio;
  $('#pfEq').textContent = fmt.usd(p.equity);
  $('#pfDay').innerHTML = `<span class="${p.pnl_today >= 0 ? 'pos' : 'neg'}">${fmt.sgn(p.pnl_today)}</span>`;
  $('#pfReal').innerHTML = `<span class="${p.realized >= 0 ? 'pos' : 'neg'}">${fmt.sgn(p.realized)}</span>`;
  $('#pfUnr').innerHTML = `<span class="${p.unrealized >= 0 ? 'pos' : 'neg'}">${fmt.sgn(p.unrealized)}</span>`;
  $('#pfOpen').textContent = `${p.open_positions} · ${p.pending_orders} pending`;
  $('#pfShadow').textContent = `${st.shadow.open_positions} open · ${st.shadow.closed_trades} closed`;
  $('#pfAsOf').textContent = st.last_cycle ? 'CYCLE ' + fmt.ago(st.last_cycle).toUpperCase() : 'STARTING…';
  const hc = healthChecks(st), pct = Math.round(100 * hc.filter(x => x[1]).length / hc.length);
  const ring = $('#healthRing'); ring.title = 'System health: ' + hc.map(x => `${x[1] ? '✓' : '✗'} ${x[0]}`).join(' · ');
  const g = ring.getContext('2d'); g.clearRect(0, 0, 120, 120); g.lineWidth = 10; g.strokeStyle = 'rgba(120,180,255,.15)'; g.beginPath(); g.arc(60, 60, 48, 0, 7); g.stroke();
  g.strokeStyle = pct >= 80 ? C.glow : pct >= 50 ? C.warn : C.crit; g.shadowColor = g.strokeStyle; g.shadowBlur = 10;
  g.beginPath(); g.arc(60, 60, 48, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * pct / 100); g.stroke(); g.shadowBlur = 0;
  g.fillStyle = '#fff'; g.font = '800 26px Orbitron, sans-serif'; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(pct + '%', 60, 56);
  g.fillStyle = C.ink2; g.font = '600 12px Rajdhani, sans-serif'; g.fillText('HEALTH', 60, 80);
  spark($('#eqSpark'), (st.equity_curve || []).map(x => x[1]), C.up, true);
}
function spark(cv, ys, color, area) {
  const [g, w, h] = hidpi(cv); g.clearRect(0, 0, w, h);
  if (!ys || ys.length < 2) { g.fillStyle = C.muted; g.font = '600 11px Rajdhani'; g.fillText('collecting…', 4, h / 2 + 4); return; }
  let lo = Math.min(...ys), hi = Math.max(...ys); if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
  const X = i => 2 + i / (ys.length - 1) * (w - 4), Y = v => h - 3 - (v - lo) / (hi - lo) * (h - 6);
  g.beginPath(); ys.forEach((v, i) => i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v)));
  g.strokeStyle = color; g.lineWidth = 1.6; g.lineJoin = 'round'; g.stroke();
  if (area) { g.lineTo(X(ys.length - 1), h); g.lineTo(X(0), h); g.closePath(); const gr = g.createLinearGradient(0, 0, 0, h);
    gr.addColorStop(0, 'rgba(31,159,224,.28)'); gr.addColorStop(1, 'rgba(31,159,224,0)'); g.fillStyle = gr; g.fill(); }
}
function renderWatch(st) {
  const box = $('#watch'); const anyDemo = st.quotes.some(q => (q.source || '').startsWith('DEMO'));
  $('#mwSrc').textContent = anyDemo ? 'DEMO · SIMULATED' : 'LIVE · 1H CLOSED BARS';
  box.innerHTML = st.quotes.map(q => {
    if (!q.ok) return `<div class="mrow"><span class="sym">${esc(q.symbol.replace('-USD', ''))}</span><span class="dim">${esc(q.error || 'unavailable')}</span><span></span><span></span></div>`;
    const p = q.live ?? q.price, ch = q.live_change_pct ?? q.change_pct;
    return `<div class="mrow ${q.symbol === S.sym ? 'sel' : ''}" data-sym="${esc(q.symbol)}" title="${esc(q.source)} · last closed bar ${esc(q.as_of)}${q.stale ? ' · STALE' : ''}${q.live != null ? ' · live price for display only' : ''}">
      <span class="sym">${esc(q.symbol.replace('-USD', ''))}</span>
      <canvas data-spark="${esc(q.symbol)}"></canvas><span class="px">${fmt.px(p)}</span><span class="chg ${ch >= 0 ? 'u' : 'd'}">${ch >= 0 ? '▲' : '▼'}${Math.abs(ch).toFixed(2)}%</span></div>`;
  }).join('');
  st.quotes.forEach(q => { const cv = box.querySelector(`canvas[data-spark="${CSS.escape(q.symbol)}"]`); if (cv && q.spark) spark(cv, q.spark, q.change_pct >= 0 ? C.up : C.down); });
  box.querySelectorAll('.mrow[data-sym]').forEach(el => el.onclick = () => selectSymbol(el.dataset.sym));
}
function renderVol(st) {
  const v = st.vol || {}; const syms = Object.keys(v);
  if (!syms.length) return;
  $('#volgate').innerHTML = syms.map(s => { const x = v[s]; const pl = (x.p_loud ?? 0) * 100, pq = (x.p_quiet ?? 0) * 100;
    const icon = x.state === 'LOUD' ? '◆' : x.state === 'QUIET' ? '○' : '●';
    return `<div class="vg" title="P(loud) ${pl.toFixed(0)}% · P(quiet) ${pq.toFixed(0)}% · method ${x.method}">
      <span class="sym">${esc(s.replace('-USD', ''))}</span><span class="tag ${x.state}">${icon} ${x.state}</span>
      <div><div class="meter"><i style="width:${pq.toFixed(0)}%;background:${C.muted}"></i><i style="flex:1;background:rgba(31,159,224,.25)"></i><i style="width:${pl.toFixed(0)}%;background:${C.serious}"></i></div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:${C.ink2}"><span>quiet ${pq.toFixed(0)}%</span><span>loud ${pl.toFixed(0)}%</span></div></div></div>`; }).join('');
  const b = v['BTC-USD'];
  $('#vgfoot').innerHTML = b ? `Walk-forward, out-of-sample on ${b.oos_samples} bars: AUC loud <b>${b.auc_loud_oos ?? '—'}</b>, quiet <b>${b.auc_quiet_oos ?? '—'}</b>. QUIET = no new trades · LOUD = 0.6× size, wider stop.` : '';
}
function riskRow(name, used, max, label, invert) {
  const frac = max ? Math.min(1, Math.abs(used) / Math.abs(max)) : 0; const cls = frac >= 1 ? 'crit' : frac >= .66 ? 'warn' : '';
  return `<div class="row"><span class="n">${name}</span><div class="bar ${cls}"><div style="width:${(frac * 100).toFixed(0)}%"></div></div><span class="v">${label}</span></div>`;
}
function renderRisk(st) {
  const L = st.limits, t = st.totals, pos = st.positions || [];
  const openStock = pos.filter(p => !p.symbol.endsWith('-USD')).length, openCrypto = pos.filter(p => p.symbol.endsWith('-USD')).length;
  const day = t.day_pnl_pct ?? 0, week = t.week_pnl_pct ?? 0;
  $('#risk').innerHTML = riskRow('Trades today', st.trades_today, L.max_trades_per_day, `${st.trades_today}/${L.max_trades_per_day}`) +
    riskRow('Open stock', openStock, L.max_open_stock, `${openStock}/${L.max_open_stock}`) +
    riskRow('Open crypto', openCrypto, L.max_open_crypto, `${openCrypto}/${L.max_open_crypto}`) +
    riskRow('Day loss', Math.min(0, day), L.day_loss_pct, `${day.toFixed(2)}%`) +
    riskRow('Week loss', Math.min(0, week), L.week_loss_pct, `${week.toFixed(2)}%`) +
    riskRow('Crypto+meme', t.crypto_meme_risk_share || 0, .4, `${((t.crypto_meme_risk_share || 0) * 100).toFixed(0)}%`) +
    `<div class="note" style="margin-top:6px">Cost rule: ≤${L.cost_ok_r}R ok · ${L.cost_ok_r}–${L.cost_max_r}R half size · >${L.cost_max_r}R WAIT. Paused bots: <b>${st.paused.length}</b>${st.paused.length ? ' (' + st.paused.slice(0, 6).map(esc).join(', ') + ')' : ''}.</div>`;
}
function renderMinis(st) {
  const t = st.totals;
  $('#mFleet').textContent = t.active ?? '—'; $('#mFleetSub').textContent = `${st.paused.length} paused · ${t.errors || 0} errors`;
  const passed = (t.gate_passed_roles || []).length; $('#mGate').textContent = `${passed}/7`;
  $('#mGateSub').textContent = passed ? 'roles paper-trading' : 'all answers WAIT';
  $('#mUp').textContent = fmt.dur(st.uptime_s);
  $('#mCycle').textContent = st.fleet_running ? `cycle running ${st.fleet_progress.done}/${st.fleet_progress.total}` : `cycle #${st.cycles} · ${fmt.ago(st.last_cycle)}`;
  $('#mSig').textContent = t.signals_now ?? 0; $('#mBuy').textContent = `BUY answers ${t.buy_answers ?? 0}`;
}
const FAMILY = [
  ['Mean reversion', ['rsi2_reversion', 'connors_rsi_rev', 'ibs_reversion', 'zscore_reversion', 'bollinger_reversion', 'keltner_reversion', 'gap_fade', 'pairs_zscore', 'vwap_reversion'], '<path d="M3 12c3-6 6 6 9 0s6 6 9 0"/>'],
  ['Trend following', ['donchian_trend', 'variance_ratio_switch', 'vwap_trend_pullback', 'btc_led_alt_trend'], '<path d="M3 18l6-6 4 4 8-9M15 7h6v6"/>'],
  ['Breakouts', ['nr7_breakout', 'vol_breakout', 'squeeze_breakout', 'volume_breakout', 'prior_session_breakout', 'failed_breakout', 'sweep_reclaim'], '<path d="M3 15h7l3-8 3 8h5"/><path d="M3 9h18" stroke-dasharray="2 3"/>'],
  ['Opening range', ['orb', 'active_orb', 'initial_balance_breakout', 'orb_stocks_in_play', 'crypto_session_orb'], '<rect x="4" y="8" width="6" height="8"/><path d="M10 10l10-6M14 4h6v6"/>'],
  ['Momentum', ['first_hour_momentum', 'noise_band_momentum', 'gap_continuation', 'attention_momentum', 'crypto_weekend'], '<path d="M4 20L20 4M9 4h11v11"/>'],
  ['Vol-gate swing', ['vol_gate_swing'], '<circle cx="12" cy="12" r="8"/><path d="M12 4v4M12 16v4M4 12h4M16 12h4"/>'],
];
function genOf(id) { const g = id.split('.')[0]; return { orb: id.includes('stocks_in_play') ? 'orb_stocks_in_play' : 'orb', vol_gate_swing: 'vol_gate_swing' }[g] || g; }
function renderFamilies(st) {
  if (!S.lib) return;
  const all = S.lib.keep.concat(S.lib.new.filter(n => n.runnable));
  $('#stratCount').textContent = `${S.lib.keep.length} KEPT · ${S.lib.new.length} NEW`;
  const live = {}; (st.fleet || []).forEach(r => { if (r.strategy) live[genOf(r.strategy)] = (live[genOf(r.strategy)] || 0) + 1; });
  $('#families').innerHTML = FAMILY.map(([name, gens, icon]) => {
    const n = all.filter(s => gens.includes(genOf(s.id))).length; const bots = gens.reduce((a, g) => a + (live[g] || 0), 0);
    return `<div class="fam" data-fam="${esc(name)}" title="${esc(gens.join(', '))}"><svg viewBox="0 0 24 24">${icon}</svg><span>${name}<br><span class="dim" style="font-size:11px">${bots} bots running it</span></span>
      <span class="c">${n}</span><span class="tag ${bots ? 'NORMAL' : 'NONE'}">${bots ? 'ONLINE' : 'STANDBY'}</span></div>`; }).join('');
  $$('#families .fam').forEach(el => el.onclick = () => openSheet('strategies', { family: el.dataset.fam }));
}
const STATUS_CLS = s => (s || '').startsWith('ACTIVE') ? 'ACTIVE' : (s || '').split(' ')[0];
function renderMatrix(st) {
  if (!S.lib) return;
  const feed = Object.fromEntries((st.fleet || []).map(r => [r.bot_id, r]));
  const paused = new Set(st.paused);
  const bots = S.lib.bots.slice().sort((a, b) => a.bot_id.localeCompare(b.bot_id));
  $('#matrix').innerHTML = bots.map(b => { const r = feed[b.bot_id] || {}; const s = r.status || b.status;
    return `<i class="${STATUS_CLS(s)} ${r.last_signal === 'LONG' ? 'sig' : ''} ${paused.has(b.bot_id) ? 'paused' : ''}" data-bot="${b.bot_id}" data-sym="${esc(b.symbol)}"
      title="${b.bot_id} · ${esc(b.symbol)} · ${esc(s)}${r.decision ? ' · ' + r.decision + ': ' + esc(r.reason) : ''}"></i>`; }).join('');
  $$('#matrix i').forEach(el => el.onclick = () => selectSymbol(el.dataset.sym));
  const counts = {}; bots.forEach(b => { const s = (feed[b.bot_id] || {}).status || b.status; counts[s] = (counts[s] || 0) + 1; });
  const colors = { WATCH: C.warn, RETRAIN: '#2f7fe0', 'NEEDS DATA': '#4a5f8a', 'MEME SHADOW': C.s3, PARKED: '#13213d', 'ACTIVE-PAPER': C.good };
  $('#mlegend').innerHTML = Object.entries(counts).map(([k, n]) => `<span><i style="background:${colors[k] || '#1a2d52'}"></i>${esc(k)} ${n}</span>`).join('') +
    '<span><i style="outline:1.5px solid #fff;outline-offset:-1px;background:none"></i>signal</span><span><i style="box-shadow:inset 0 0 0 2px var(--crit)"></i>paused</span>';
  const t = st.totals;
  $('#fleetKv').innerHTML = `<span>Heartbeats</span><span>${t.active ? `${(st.fleet || []).filter(r => (r.heartbeat_age_s ?? 1e9) < 900).length}/${t.active} fresh` : '—'}</span>
    <span>Data errors / stale</span><span>${t.errors ?? '—'} / ${t.stale_bots ?? '—'}</span>
    <span>Shadow open / closed</span><span>${t.shadow_open ?? 0} / ${t.shadow_closed ?? 0}</span>
    <span>Backtest tries counted</span><span>${st.tries}</span>`;
}
function renderTarget(st) {
  const rows = (st.fleet || []).filter(r => (r.symbol || '').toUpperCase() === S.sym.toUpperCase());
  $('#lockSym').textContent = 'LOCKED ON ' + S.sym;
  const r = rows[0];
  if (!r) { $('#bullets').textContent = '- WAIT\n- No bot for this symbol'; $('#targetKv').innerHTML = ''; $('#targetNote').textContent = 'Pick a symbol a bot trades (Market Watch or the matrix).'; return; }
  $('#bullets').innerHTML = esc(r.decision === 'BUY' ? '- BUY ' + S.sym : '- WAIT') + '\n' + esc('- ' + (r.reason || 'Need fresh prices'));
  $('#bullets').style.color = r.decision === 'BUY' ? C.good : '#fff';
  $('#targetKv').innerHTML = `<span>Bot</span><span>${esc(r.bot_id)} <span class="tag ${STATUS_CLS(r.status)}">${esc(r.status)}</span></span>
    <span>Strategy</span><span title="${esc(r.strategy)}">${esc((r.strategy || '').split('.')[0])}</span>
    <span>Gate verdict</span><span>${esc(r.verdict)}</span>
    <span>Signal (closed bar)</span><span><span class="tag ${r.last_signal}">${esc(r.last_signal || '—')}</span></span>
    <span>Volatility</span><span><span class="tag ${r.vol_state}">${esc(r.vol_state || '—')}</span></span>
    <span>Cost in R</span><span>${r.cost_r != null ? fmt.num(r.cost_r) + 'R' : '—'}</span>`;
  $('#targetNote').textContent = r.error ? 'Error: ' + r.error : r.would_pause ? 'Risk engine would pause: ' + r.would_pause : 'Answer from policy.decide (code). The AI cannot change it.';
}
function drawRadar(t) {
  const cv = $('#radar'); if (!cv) return; const g = cv.getContext('2d'); const w = cv.width, h = cv.height, cx = w / 2, cy = h / 2, r = w / 2 - 4;
  g.clearRect(0, 0, w, h); g.strokeStyle = 'rgba(64,160,255,.35)'; g.lineWidth = 1.2;
  [1, .66, .33].forEach(k => { g.beginPath(); g.arc(cx, cy, r * k, 0, 7); g.stroke(); });
  g.beginPath(); g.moveTo(cx - r, cy); g.lineTo(cx + r, cy); g.moveTo(cx, cy - r); g.lineTo(cx, cy + r); g.stroke();
  const a = t / 900 % (Math.PI * 2); const gr = g.createConicGradient ? g.createConicGradient(a - .9, cx, cy) : null;
  if (gr) { gr.addColorStop(0, 'rgba(46,168,255,0)'); gr.addColorStop(.14, 'rgba(46,168,255,.35)'); gr.addColorStop(.15, 'rgba(46,168,255,0)'); g.fillStyle = gr; g.beginPath(); g.arc(cx, cy, r, 0, 7); g.fill(); }
  const fl = (S.st && S.st.fleet) || [];
  fl.forEach(b => { const hsh = [...b.bot_id].reduce((x, c) => x * 31 + c.charCodeAt(0), 7); const ang = (hsh % 360) * Math.PI / 180, dist = r * (.25 + (hsh % 70) / 100);
    const sig = b.last_signal === 'LONG', sel = (b.symbol || '').toUpperCase() === S.sym.toUpperCase();
    g.fillStyle = sel ? '#fff' : sig ? C.good : 'rgba(143,227,255,.35)'; g.beginPath(); g.arc(cx + Math.cos(ang) * dist, cy + Math.sin(ang) * dist, sel ? 4 : sig ? 3.5 : 1.8, 0, 7); g.fill();
    if (sel) { g.strokeStyle = '#fff'; g.beginPath(); g.arc(cx + Math.cos(ang) * dist, cy + Math.sin(ang) * dist, 9 + Math.sin(t / 200) * 2, 0, 7); g.stroke(); } });
}

/* ============================ chart ============================ */
async function loadChart() {
  $('#chTitle').textContent = `${S.sym} · ${S.bar.toUpperCase()}`;
  const d = await api(`/api/bars?symbol=${encodeURIComponent(S.sym)}&bar=${S.bar}&n=150${S.strat ? '&strategy=' + encodeURIComponent(S.strat) : ''}`);
  S.chart = d; $('#chSrc').textContent = d.ok ? `${d.source}${d.stale ? ' · STALE' : ''} · closed bars` : '';
  drawChart();
}
function drawChart() {
  const cv = $('#chart'), d = S.chart; const [g, w, h] = hidpi(cv); g.clearRect(0, 0, w, h);
  if (!d || !d.ok) { g.fillStyle = C.muted; g.font = '600 13px Rajdhani'; g.fillText(d ? 'No data: ' + (d.error || '') : 'Loading…', 12, 24); return; }
  const n = d.c.length, padR = 64, padB = 18, top = 6, W = w - padR, H = h - padB - top;
  let lo = Math.min(...d.l), hi = Math.max(...d.h); const pad = (hi - lo) * .06 || hi * .01; lo -= pad; hi += pad;
  const X = i => (i + .5) / n * W, Y = v => top + (hi - v) / (hi - lo) * H, cw = Math.max(1, W / n * .62);
  S.chartGeom = { X, Y, n, W, H, top, padR, lo, hi };
  g.font = '600 10.5px ' + css('--mono'); g.fillStyle = C.ink2; g.strokeStyle = 'rgba(64,160,255,.10)'; g.lineWidth = 1;
  for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4, y = Y(v); g.beginPath(); g.moveTo(0, y); g.lineTo(W, y); g.stroke(); g.fillText(fmt.px(v), W + 6, y + 3); }
  const step = Math.max(1, Math.round(n / 5));
  for (let i = Math.floor(step / 2); i < n; i += step) { const lbl = fmt.mt(d.t[i]), tw = g.measureText(lbl).width; g.fillText(lbl, Math.max(0, Math.min(X(i) - tw / 2, W - tw)), h - 4); }
  // candles: up = filled blue, down = hollow amber (shape + colour)
  for (let i = 0; i < n; i++) { const up = d.c[i] >= d.o[i], col = up ? C.up : C.down, x = X(i);
    g.strokeStyle = col; g.lineWidth = 1; g.beginPath(); g.moveTo(x, Y(d.h[i])); g.lineTo(x, Y(d.l[i])); g.stroke();
    const y0 = Y(Math.max(d.o[i], d.c[i])), y1 = Y(Math.min(d.o[i], d.c[i])), bh = Math.max(1, y1 - y0);
    if (up) { g.fillStyle = col; g.fillRect(x - cw / 2, y0, cw, bh); } else { g.fillStyle = '#06132b'; g.fillRect(x - cw / 2, y0, cw, bh); g.lineWidth = 1.2; g.strokeRect(x - cw / 2 + .5, y0 + .5, cw - 1, Math.max(0, bh - 1)); } }
  const line = (arr, col, dash, wdt) => { g.strokeStyle = col; g.lineWidth = wdt; g.setLineDash(dash); g.beginPath(); let s = false;
    arr.forEach((v, i) => { if (v == null) { s = false; return; } s ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v)); s = true; }); g.stroke(); g.setLineDash([]); };
  line(d.ema20, C.s3, [], 1.6); line(d.vwap, C.ref, [5, 4], 1.3);
  const sig = new Set(d.signals || []); g.fillStyle = C.good;
  d.t.forEach((t, i) => { if (!sig.has(t)) return; const x = X(i), y = Y(d.l[i]) + 10; g.beginPath(); g.moveTo(x, y - 6); g.lineTo(x - 5, y + 3); g.lineTo(x + 5, y + 3); g.closePath(); g.fill(); });
  const last = d.c[n - 1], yl = Y(last); g.fillStyle = last >= d.o[n - 1] ? C.up : C.down; g.fillRect(W + 2, yl - 8, padR - 4, 16);
  g.fillStyle = '#fff'; g.font = '700 10.5px ' + css('--mono'); g.fillText(fmt.px(last), W + 6, yl + 4);
}
function chartHover(e) {
  const d = S.chart, G = S.chartGeom; const cv = $('#chartHover'); const [g, w, h] = hidpi(cv); g.clearRect(0, 0, w, h); const tip = $('#chTip');
  if (!d || !d.ok || !G || !e) { tip.style.display = 'none'; return; }
  const rect = cv.getBoundingClientRect(), mx = e.clientX - rect.left, my = e.clientY - rect.top;
  if (mx > G.W) { tip.style.display = 'none'; return; }
  const i = Math.max(0, Math.min(G.n - 1, Math.floor(mx / G.W * G.n))), x = G.X(i);
  g.strokeStyle = 'rgba(201,214,234,.45)'; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(x, 0); g.lineTo(x, G.top + G.H); g.moveTo(0, my); g.lineTo(G.W, my); g.stroke(); g.setLineDash([]);
  g.fillStyle = '#fff'; g.beginPath(); g.arc(x, G.Y(d.c[i]), 3.5, 0, 7); g.fill(); g.strokeStyle = '#06132b'; g.lineWidth = 2; g.stroke();
  const ch = (d.c[i] / d.o[i] - 1) * 100;
  tip.innerHTML = `<b>${esc(fmt.mt(d.t[i]))} MT</b><br>O ${fmt.px(d.o[i])}  H ${fmt.px(d.h[i])}<br>L ${fmt.px(d.l[i])}  C ${fmt.px(d.c[i])} <span class="${ch >= 0 ? 'pos' : 'neg'}">${fmt.pct(ch)}</span><br>EMA20 ${fmt.px(d.ema20[i])} · VWAP ${fmt.px(d.vwap[i])}${(d.signals || []).includes(d.t[i]) ? '<br>▲ strategy signal on this closed bar' : ''}`;
  tip.style.display = 'block'; const tw = tip.offsetWidth; tip.style.left = (mx + 14 + tw > rect.width ? mx - tw - 14 : mx + 14) + 'px'; tip.style.top = Math.max(0, my - 40) + 'px';
}
function selectSymbol(sym) {
  S.sym = sym; try { localStorage.setItem('jarvis_sym', sym); } catch (e) { }
  if ($('#chSym').value !== sym) { if (![...$('#chSym').options].some(o => o.value === sym)) $('#chSym').add(new Option(sym, sym)); $('#chSym').value = sym; }
  if (sym.endsWith('-USD') && S.bar === '15m') setBar('1h'); else loadChart();
  if (S.st) { renderTarget(S.st); renderWatch(S.st); }
}
function setBar(b) { S.bar = b; $$('#barSeg button').forEach(x => x.classList.toggle('on', x.dataset.b === b)); loadChart(); }

/* ============================ voice ============================ */
const V = { rec: null, on: false, muted: false, wake: true, voiceOut: true, speaking: false, state: 'DORMANT', followUntil: 0, epoch: 0, spoken: '', active: false, analyser: null, voice: null };
function setV(st, extra) { V.state = st; document.body.dataset.v = st; $('#vstateText').textContent = 'SYSTEM STATUS · ' + st + (extra ? ' · ' + extra : ''); $('#mic').classList.toggle('live', ['LISTENING', 'FOLLOW-UP'].includes(st)); }
function sub(text, heard) { const el = $('#sub'); el.classList.toggle('heard', !!heard); el.textContent = text; }
function pickVoice() { const vs = speechSynthesis.getVoices(); V.voice = vs.find(v => /UK English Male|Daniel|George|Ryan|Arthur|Brian|Thomas/i.test(v.name)) || vs.find(v => v.lang === 'en-GB') || vs.find(v => /^en/.test(v.lang)) || null; }
if ('speechSynthesis' in window) { speechSynthesis.onvoiceschanged = pickVoice; pickVoice(); }
const norm = s => s.toLowerCase().replace(/[^a-z0-9 ]/g, ' ').split(/\s+/).filter(w => w.length > 2);
function isEcho(t) { if (!V.speaking && Date.now() - (V.spokeAt || 0) > 1500) return false; const w = norm(t); if (!w.length) return true;
  const said = new Set(norm(V.spoken)); return w.filter(x => said.has(x)).length / w.length >= .6; }
function speak(text, ep) {
  if (!V.voiceOut || !('speechSynthesis' in window) || !text) { afterSpeech(ep); return; }
  speechSynthesis.cancel();
  const parts = text.match(/[^.!?]+[.!?]*/g) || [text]; V.spoken = text; V.speaking = true; setV('SPEAKING');
  parts.forEach((p, i) => { const u = new SpeechSynthesisUtterance(p.trim()); if (V.voice) u.voice = V.voice; u.rate = 1.04; u.pitch = .92;
    if (i === parts.length - 1) { u.onend = () => { if (ep === V.epoch) afterSpeech(ep); }; u.onerror = u.onend; }
    speechSynthesis.speak(u); });
}
function afterSpeech(ep) { V.speaking = false; V.spokeAt = Date.now(); if (ep !== V.epoch) return;
  if (V.on && !V.muted) { V.followUntil = Date.now() + 12000; setV('FOLLOW-UP'); setTimeout(() => { if (V.state === 'FOLLOW-UP' && Date.now() >= V.followUntil) setV('DORMANT'); }, 12200); }
  else setV('DORMANT'); }
function bargeIn() { if ('speechSynthesis' in window) speechSynthesis.cancel(); V.speaking = false; V.epoch++; V.followUntil = Date.now() + 12000; setV('LISTENING', 'INTERRUPTED'); }
const WAKE = /\b(?:hey |ok |okay )?(jarvis|jarvus|jervis|javis|jarviss)\b[,.!?]?/i;
function onFinal(t) {
  if (V.muted) return;
  if (V.speaking) { if (isEcho(t)) return; bargeIn(); }
  const hasWake = WAKE.test(t), listening = V.state === 'LISTENING' || Date.now() < V.followUntil || !V.wake;
  if (!listening && !hasWake) return;
  let q = hasWake ? t.slice(t.search(WAKE)).replace(WAKE, '').trim() : t.trim();
  if (!q) { V.followUntil = Date.now() + 12000; setV('LISTENING'); sub('Yes?'); const ep = ++V.epoch; speak('Yes?', ep); return; }
  ask(q, 'desktop_voice');
}
function onInterim(t) {
  if (V.muted) return;
  if (V.speaking) { if (!isEcho(t) && norm(t).length >= 2) bargeIn(); return; }
  if (V.state === 'LISTENING' || V.state === 'FOLLOW-UP' || WAKE.test(t)) { sub(t, true); if (V.state === 'FOLLOW-UP') setV('LISTENING'); }
}
function startRec() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { setV('ERROR', 'NO SPEECH API'); sub('Voice needs Chrome or Edge. You can still type below.'); return false; }
  if (V.rec) return true;
  const rec = new SR(); rec.continuous = true; rec.interimResults = true; rec.lang = 'en-US';
  rec.onresult = e => { for (let i = e.resultIndex; i < e.results.length; i++) { const r = e.results[i], t = (r[0].transcript || '').trim(); if (!t) continue; r.isFinal ? onFinal(t) : onInterim(t); } };
  rec.onend = () => { if (V.on && !V.muted) setTimeout(() => { try { rec.start(); } catch (e) { } }, 250); };
  rec.onerror = e => { if (e.error === 'not-allowed' || e.error === 'service-not-allowed') { V.on = false; setV('ERROR', 'MIC BLOCKED'); sub('Microphone is blocked. Allow it in the address bar, then click the mic.'); } };
  V.rec = rec; V.on = true; try { rec.start(); } catch (e) { } return true;
}
async function startLevel() {
  try { const s = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    const ctx = new (window.AudioContext || window.webkitAudioContext)(); const src = ctx.createMediaStreamSource(s); const an = ctx.createAnalyser(); an.fftSize = 512; src.connect(an); V.analyser = an; V.buf = new Uint8Array(an.fftSize);
  } catch (e) { V.analyser = null; }
}
function drawScope(t) {
  const [g, w, h] = hidpi($('#scope')); g.clearRect(0, 0, w, h); g.lineWidth = 1.5; g.strokeStyle = V.muted ? C.muted : C.glow2; g.beginPath();
  let lvl = 0;
  if (V.analyser && !V.muted) { V.analyser.getByteTimeDomainData(V.buf); for (let i = 0; i < w; i++) { const v = (V.buf[Math.floor(i / w * V.buf.length)] - 128) / 128; lvl = Math.max(lvl, Math.abs(v)); i ? g.lineTo(i, h / 2 + v * h * .9) : g.moveTo(i, h / 2 + v * h * .9); } }
  else { const amp = V.state === 'SPEAKING' ? 6 : V.state === 'THINKING' || V.state === 'USING TOOLS' ? 3 : 1; for (let i = 0; i < w; i++) { const y = h / 2 + Math.sin(i / 9 + t / 120) * amp * Math.sin(i / w * Math.PI); i ? g.lineTo(i, y) : g.moveTo(i, y); } }
  g.stroke(); R.level += ((V.speaking ? .3 + .2 * Math.sin(t / 90) : lvl) - R.level) * .2;
}
function pushToTalk() {
  if (!V.active) activate();
  if (V.speaking) { bargeIn(); return; }
  if (V.muted) { toggleMute(); }
  if (!V.on) startRec();
  V.followUntil = Date.now() + 12000; setV('LISTENING'); sub('Listening…', true);
}
function toggleMute() { V.muted = !V.muted; $('#mic').classList.toggle('muted', V.muted);
  if (V.muted) { V.on = false; try { V.rec && V.rec.stop(); } catch (e) { } V.rec = null; if ('speechSynthesis' in window) speechSynthesis.cancel(); setV('MUTED'); sub('Microphone muted. Press M to unmute.'); }
  else { startRec(); setV('DORMANT'); sub('Listening for "Jarvis".'); } }
function activate() {
  if (V.active) return; V.active = true; $('#activate').style.display = 'none';
  if (startRec()) { startLevel(); setV('DORMANT'); sub('Online. Say "Jarvis" followed by anything — or press Space.'); }
  const ep = ++V.epoch; const h = new Date().getHours();
  speak(`${h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'}. Jarvis is online. Paper trading only.`, ep);
}

/* ============================ asking Jarvis ============================ */
async function ask(text, channel = 'gui') {
  text = (text || '').trim(); if (!text) return;
  const ep = ++V.epoch; if ('speechSynthesis' in window) speechSynthesis.cancel(); V.speaking = false;
  setV('THINKING'); sub('“' + text + '”', true); $('#sources').innerHTML = '';
  let r;
  try { r = await api('/api/ask', { text, channel, session_id: S.session }); }
  catch (e) { setV('ERROR', 'SERVER'); sub('I cannot reach the Jarvis server.'); return; }
  if (ep !== V.epoch) return;                                  // superseded by a newer turn / barge-in
  if (r.error) { setV('ERROR'); sub(r.error); return; }
  S.session = r.session_id; try { localStorage.setItem('jarvis_session', r.session_id); } catch (e) { }
  typeOut(r.reply);
  if (r.sources && r.sources.length) $('#sources').innerHTML = 'Sources: ' + r.sources.slice(0, 4).map(s => `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc((s.title || s.url).slice(0, 48))}</a>`).join('');
  else if (r.route === 'offline') $('#sources').textContent = 'Offline mode — add an Anthropic API key for full conversation.';
  else if (r.model) $('#sources').textContent = `${r.route} · ${r.model}${r.tools && r.tools.length ? ' · tools: ' + r.tools.slice(0, 5).join(', ') : ''}`;
  speak(r.speak, ep);
  refresh();
}
function typeOut(text) { const el = $('#sub'); el.classList.remove('heard'); el.textContent = ''; let i = 0; const id = ++typeOut.id;
  (function step() { if (id !== typeOut.id) return; i = Math.min(text.length, i + 3); el.textContent = text.slice(0, i); if (i < text.length) setTimeout(step, 12); })(); }
typeOut.id = 0;

/* ============================ notifications & ui events ============================ */
function toast(title, body, sev) { const el = document.createElement('div'); el.className = 'toast ' + (sev || ''); el.innerHTML = `<b>${esc(title)}</b>${esc(body)}`;
  $('#toasts').prepend(el); setTimeout(() => el.remove(), sev === 'critical' ? 15000 : 8000); }
function handleEvents(st) {
  const notes = (st.notifications || []).slice().reverse();
  if (S.noteSince < 0) { S.noteSince = notes.length ? notes[notes.length - 1].id : 0; }
  else for (const n of notes) { if (n.id <= S.noteSince) continue; S.noteSince = n.id; toast(n.title, n.body, n.severity);
    const worthSaying = n.severity !== 'info' || /alert|remind|report/i.test(n.title);
    if (V.active && V.voiceOut && !V.speaking && !['THINKING', 'USING TOOLS'].includes(V.state) && worthSaying) { const ep = ++V.epoch; speak(`${n.title}. ${n.body}`, ep); } }
  for (const e of st.ui_events || []) { S.uiSince = Math.max(S.uiSince, e.id);
    if (e.type === 'tool' && (V.state === 'THINKING' || V.state === 'USING TOOLS')) setV('USING TOOLS', e.name.replace(/_/g, ' ').toUpperCase());
    if (e.type === 'open_panel') { if (e.symbol) selectSymbol(e.symbol); if (e.panel === 'chart' || e.panel === 'core') closeSheet(); else if (SHEETS[e.panel]) openSheet(e.panel); else if (e.panel === 'risk') openSheet('help'); }
    if (e.type === 'task' && S.sheet === 'backtest') SHEETS.backtest.tasks(); }
}

/* ============================ overlay pages ============================ */
const SHEETS = {};
function openSheet(name, opts) { S.sheet = name; S.sheetOpts = opts || {}; $('#sheet').classList.add('open'); $('#sheetTitle').textContent = SHEETS[name].title;
  $$('.dk').forEach(d => d.classList.toggle('on', d.dataset.p === name)); SHEETS[name].render(false); }
function closeSheet() { S.sheet = null; $('#sheet').classList.remove('open'); $$('.dk').forEach(d => d.classList.remove('on')); }
const body = () => $('#sheetBody');
const verdictTag = v => `<span class="tag ${v === 'PAPER TEST' ? 'pass' : v === 'REJECT' ? 'fail' : 'WAIT'}">${esc(v || '—')}</span>`;

SHEETS.strategies = { title: 'STRATEGY LIBRARY · ALL STRATEGIES', render() {
  const L = S.lib; if (!L) { body().textContent = 'Loading…'; return; }
  const tab = S.sheetOpts.tab || 'keep', q = (S.sheetOpts.q || '').toLowerCase(), fam = S.sheetOpts.family;
  const famGens = fam ? (FAMILY.find(f => f[0] === fam) || [0, []])[1] : null;
  let rows = tab === 'keep' ? L.keep : tab === 'new' ? L.new : [];
  if (famGens) rows = rows.filter(r => famGens.includes(genOf(r.id)));
  if (q) rows = rows.filter(r => JSON.stringify(r).toLowerCase().includes(q));
  const tabs = [['keep', `KEPT (${L.keep.length})`], ['new', `NEW (${L.new.length})`], ['dnb', `DO NOT BUILD (${L.do_not_build.length})`], ['measured', 'MEASURED (BTC)'], ['rules', 'DAY-TRADE RULES'], ['cost', 'COST IN R']];
  let html = `<div class="tabs">${tabs.map(([k, l]) => `<button data-tab="${k}" class="${tab === k ? 'on' : ''}">${l}</button>`).join('')}
    <input id="stQ" placeholder="search…" value="${esc(S.sheetOpts.q || '')}" style="margin-left:auto;background:rgba(0,0,0,.4);border:1px solid var(--line2);padding:3px 8px">
    ${fam ? `<button data-clear="1">FAMILY: ${esc(fam)} ✕</button>` : ''}</div>`;
  if (tab === 'keep' || tab === 'new') {
    const sel = S.sheetOpts.sel && rows.find(r => r.id === S.sheetOpts.sel) || rows[0];
    html += `<div class="split"><div style="overflow:auto"><table class="t"><thead><tr><th>ID</th>${tab === 'keep' ? '<th>TIER</th><th>GRADE</th>' : '<th>FAMILY</th><th>STATUS</th>'}<th>JOB</th><th>BARS</th></tr></thead><tbody>` +
      rows.map(r => `<tr class="click" data-id="${esc(r.id)}"><td class="num" style="text-align:left;font-size:11.5px">${r.starter ? '★ ' : ''}${esc(r.id)}</td>${tab === 'keep' ? `<td>${esc(r.tier)}</td><td>${esc(r.jarvus_grade)}</td>` : `<td>${esc(r.family)}</td><td>${r.built ? '<span class="tag pass">BUILT</span>' : '<span class="tag na">TODO</span>'}</td>`}<td>${esc(r.job)}</td><td>${esc(r.bars)}</td></tr>`).join('') +
      `</tbody></table>${rows.length ? '' : '<p class="dim">No match.</p>'}</div><div>${sel ? stratDetail(sel, tab) : ''}</div></div>`;
  } else if (tab === 'dnb') html += `<table class="t"><thead><tr><th>IDEA</th><th>WHY NOT</th></tr></thead><tbody>${L.do_not_build.map(r => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`).join('')}</tbody></table>`;
  else { const T = tab === 'measured' ? L.measured : tab === 'rules' ? L.day_trade_rules : { header: L.cost_in_r[0] ? [] : [], rows: L.cost_in_r };
    html += `<table class="t">${T.header && T.header.length ? `<thead><tr>${T.header.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead>` : ''}<tbody>${T.rows.map(r => `<tr>${r.map(c => `<td>${esc(typeof c === 'number' ? (Math.abs(c) < 10 ? c.toFixed(2) : c) : c)}</td>`).join('')}</tr>`).join('')}</tbody></table>`; }
  body().innerHTML = html;
  body().querySelectorAll('.tabs button[data-tab]').forEach(b => b.onclick = () => { S.sheetOpts.tab = b.dataset.tab; S.sheetOpts.sel = null; this.render(); });
  const c = body().querySelector('[data-clear]'); if (c) c.onclick = () => { S.sheetOpts.family = null; this.render(); };
  const qi = $('#stQ'); qi.oninput = () => { S.sheetOpts.q = qi.value; clearTimeout(this.t); this.t = setTimeout(() => { this.render(); const n = $('#stQ'); n.focus(); n.setSelectionRange(n.value.length, n.value.length); }, 250); };
  body().querySelectorAll('tr[data-id]').forEach(tr => tr.onclick = () => { S.sheetOpts.sel = tr.dataset.id; this.render(); });
  body().querySelectorAll('[data-bt]').forEach(b => b.onclick = () => openSheet('backtest', { strategy: b.dataset.bt, symbol: b.dataset.sym }));
  body().querySelectorAll('[data-show]').forEach(b => b.onclick = () => { S.strat = b.dataset.show; ensureStratOption(S.strat); $('#chStrat').value = S.strat; closeSheet(); loadChart(); });
} };
function stratDetail(r, tab) {
  const isStock = /stock/.test(r.markets || '') && !/crypto/.test(r.markets || ''), sym = isStock ? 'JPM' : 'BTC-USD';
  const f = tab === 'keep' ? [['Job', r.job], ['Grades', `Jarvus ${r.jarvus_grade} · catalog ${r.catalog_grade}`], ['Evidence', r.evidence], ['Caveat', r.caveat], ['Bars / markets', `${r.bars} · ${r.markets}`],
    ['Entry', r.entry], ['Exit', r.exit_rule], ['Stop', r.stop], ['Settings', r.settings], ['v5 crypto override', r.crypto_override], ['Your BTC test', r.btc_test]]
    : [['Job', r.job], ['Evidence', r.evidence], ['Rules', r.rules], ['Bars', r.bars], ['Status', r.status]];
  const runnable = tab === 'keep' || r.runnable;
  return `<div class="card"><h4>${esc(r.id)}</h4>${f.filter(x => x[1]).map(([k, v]) => `<div style="margin-bottom:6px"><div class="lab">${k}</div><div class="note">${esc(v)}</div></div>`).join('')}
    ${runnable ? `<div style="display:flex;gap:8px;margin-top:8px"><button class="btn" data-bt="${esc(r.id)}" data-sym="${sym}">BACKTEST ON ${sym}</button><button class="btn" data-show="${esc(r.id)}">SHOW SIGNALS ON CHART</button></div>`
      : `<div class="note">${r.overlay ? 'Overlay: applied inside the backtester (vol gate toggle / maker execution), not a stand-alone signal.' : ''}</div>`}
    <p class="note warn" style="margin-top:8px">Keeping a strategy does not make it profitable. Nothing here has shown profit after costs.</p></div>`;
}

SHEETS.backtest = { title: 'BACKTEST LAB · QUALITY GATE', live: true, render(poll) {
  if (poll && !this.watch) return;
  const L = S.lib; if (!L) return;
  if (!poll) {
    const o = S.sheetOpts; const ids = L.keep.map(r => r.id).concat(L.new.filter(n => n.runnable).map(n => n.id));
    const syms = Array.from(new Set(L.bots.map(b => b.symbol).concat(['BTC-USD', 'ETH-USD', 'SOL-USD']))).sort();
    body().innerHTML = `<div class="split"><div>
      <div class="card"><h4>RUN A BACKTEST (SANDBOX · HISTORICAL BARS · COSTS ON · NEXT-BAR FILLS)</h4>
        <div class="form">
          <label>Strategy</label><select id="btS">${ids.map(i => `<option ${i === o.strategy ? 'selected' : ''}>${esc(i)}</option>`).join('')}</select>
          <label>Symbol</label><input id="btSym" list="btSyms" value="${esc(o.symbol || 'BTC-USD')}"><datalist id="btSyms">${syms.map(s => `<option>${esc(s)}</option>`).join('')}</datalist>
          <label>Bar size</label><select id="btBar"><option>1h</option><option>15m</option><option>5m</option></select>
          <label>Venue / fees</label><select id="btVenue"><option value="">default (NDAX crypto · stock assumption)</option>${Object.entries(L.venues).map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join('')}</select>
          <label>Volatility gate</label><label><input type="checkbox" id="btVol" checked style="width:auto"> skip QUIET, LOUD = 0.6× size</label>
        </div>
        <div style="margin-top:10px;display:flex;gap:8px"><button class="btn" id="btRun">RUN BACKTEST</button><span class="note" id="btMsg"></span></div>
      </div>
      <div id="btResult"></div>
    </div><div>
      <div class="card"><h4>ROLE TEST (BUILD STEP 5 · OPENS THE GATE ONLY ON A FULL PASS)</h4>
        <div class="note">Picks ONE candidate on pooled train data, tests it once on unseen data, then runs the quality gate (trades, Deflated Sharpe, 2× costs, walk-forward, sign-flip, PBO, placebo, buy-and-hold). Only a PAPER TEST verdict lets that role's bots paper-trade.</div>
        <div class="form" style="margin-top:8px"><label>Role</label><select id="rtRole">${L.roles.map(r => `<option>${esc(r.role)}</option>`).join('')}</select></div>
        <div style="margin-top:10px"><button class="btn" id="rtRun">RUN ROLE TEST</button></div>
        <div style="margin-top:8px">${Object.entries(S.st.gate_results || {}).map(([k, v]) => `<div class="note">${esc(k)}: ${verdictTag(v.verdict)} <span class="dim">${esc(v.chosen)} · ${v.test_trades} test trades · ${esc(v.run_at)}</span></div>`).join('') || '<div class="note dim">No role has been tested yet.</div>'}</div>
      </div>
      <div class="card"><h4>RECENT RUNS</h4><div id="btTasks"></div></div>
    </div></div>`;
    $('#btRun').onclick = async () => { $('#btMsg').textContent = 'starting…';
      const r = await api('/api/backtest', { strategy_id: $('#btS').value, symbol: $('#btSym').value.trim().toUpperCase(), bar: $('#btBar').value, ...($('#btVenue').value ? { venue: $('#btVenue').value } : {}), vol_gate: $('#btVol').checked });
      if (!r.ok) { $('#btMsg').textContent = (r.error && r.error.message) || r.error || 'failed'; return; }
      this.watch = r.data.task_id; $('#btMsg').textContent = 'queued'; this.poll(); };
    $('#rtRun').onclick = async () => { const r = await api('/api/role_test', { role: $('#rtRole').value }); if (r.ok) { this.watch = r.data.task_id; this.poll(); } else toast('Role test', (r.error && r.error.message) || 'failed', 'warning'); };
    if (o.strategy && o.autorun) $('#btRun').click();
    this.tasks();
    if (this.watch) this.poll();
  }
}, async tasks() {
  const t = await api('/api/tasks'); const el = $('#btTasks'); if (!el) return;
  el.innerHTML = (t.tasks || []).filter(x => x.kind === 'backtest' || x.kind === 'role_test').slice(0, 12).map(x => `<div class="fam" data-task="${x.id}" style="grid-template-columns:1fr 70px"><span>${esc(x.title)}<br><span class="dim" style="font-size:11px">${new Date(x.created_at * 1000).toLocaleString()}</span></span><span class="tag ${x.status === 'done' ? 'pass' : x.status === 'failed' ? 'fail' : 'WAIT'}">${esc(x.status.toUpperCase())}</span></div>`).join('') || '<div class="note dim">No runs yet.</div>';
  el.querySelectorAll('[data-task]').forEach(d => d.onclick = () => { this.watch = d.dataset.task; this.poll(); });
}, async poll() {
  const id = this.watch; if (!id || S.sheet !== 'backtest') return;
  const t = await api('/api/task?id=' + encodeURIComponent(id)); const el = $('#btResult'); if (!el || t.error) return;
  if (t.status === 'done') { this.watch = null; showResult(t); this.tasks(); return; }
  if (t.status === 'failed' || t.status === 'cancelled' || t.status === 'interrupted') { this.watch = null; el.innerHTML = `<div class="card"><h4>${esc(t.title)}</h4><p class="note warn">${esc(t.status)}: ${esc((t.error || '').split('\n')[0])}</p></div>`; this.tasks(); return; }
  el.innerHTML = `<div class="card"><h4>${esc(t.title)}</h4><div class="progress"><div style="width:${(t.progress * 100).toFixed(0)}%"></div></div><div class="note">${esc(t.status)} · ${esc(t.detail || '')}</div><button class="btn sm" id="btCancel">CANCEL</button></div>`;
  $('#btCancel').onclick = () => api('/api/task/cancel', { id });
  setTimeout(() => this.poll(), 1200);
} };
function gateTable(g) {
  return `<table class="t"><thead><tr><th>CHECK</th><th>VALUE</th><th>NEED</th><th>RESULT</th></tr></thead><tbody>${g.checks.map(c => `<tr><td>${esc(c.name)}</td><td class="num">${esc(c.value ?? '—')}</td><td>${esc(c.need)}</td><td>${c.passed === null ? '<span class="tag na">NOT COMPUTED</span>' : c.passed ? '<span class="tag pass">PASS</span>' : '<span class="tag fail">FAIL</span>'}</td></tr>`).join('')}</tbody></table>`;
}
function showResult(t) {
  const r = t.result, el = $('#btResult'); if (!r) return;
  if (t.kind === 'role_test') {
    if (!r.ok) { el.innerHTML = `<div class="card"><p class="note warn">${esc(r.error)}</p></div>`; return; }
    el.innerHTML = `<div class="card"><h4>ROLE: ${esc(r.role)} · VERDICT ${verdictTag(r.gate.verdict)}</h4>
      <div class="note">Chosen on train data: <b>${esc(r.chosen)}</b> · ${r.symbols.length} symbols · source ${esc(r.source)} · ${r.n_trials_counted} tries counted${r.missing.length ? ' · missing: ' + esc(r.missing.join(', ')) : ''}</div>
      <table class="t" style="margin:8px 0"><thead><tr><th>CANDIDATE</th><th>TRAIN TRADES</th><th>TRAIN AVG R</th><th>TEST TRADES</th><th>TEST AVG R</th></tr></thead><tbody>${Object.entries(r.candidates).map(([k, v]) => `<tr><td>${k === r.chosen ? '★ ' : ''}${esc(k)}</td><td class="num">${v.train.trades}</td><td class="num">${fmt.r(v.train.avg_r_net)}</td><td class="num">${v.test.trades}</td><td class="num">${fmt.r(v.test.avg_r_net)}</td></tr>`).join('')}</tbody></table>
      ${gateTable(r.gate)}</div>`; return;
  }
  const s = r.stats;
  el.innerHTML = `<div class="card"><h4>${esc(r.strategy)} · ${esc(r.symbol)} ${esc(r.bar)} · GATE ${verdictTag(r.gate.verdict)}</h4>
    <div class="note">${esc(r.source)} · ${r.bars_tested} bars ${fmt.mt(r.period[0])} → ${fmt.mt(r.period[1])} MT · ${esc(r.venue_label)} · stop ${r.stop_atr_used} ATR · vol gate ${r.vol_gate ? 'on' : 'off'}${r.data_note ? ' · ' + esc(r.data_note) : ''}</div>
    <div class="tiles" style="margin-top:8px">
      <div class="tile"><div class="lab">Trades</div><div class="big">${s.trades}</div></div>
      <div class="tile"><div class="lab">Hit rate</div><div class="big">${s.hit_rate == null ? '—' : (s.hit_rate * 100).toFixed(0) + '%'}</div></div>
      <div class="tile"><div class="lab">Avg R (net)</div><div class="big ${s.avg_r_net >= 0 ? 'pos' : 'neg'}">${fmt.r(s.avg_r_net)}</div></div>
      <div class="tile"><div class="lab">Avg cost</div><div class="big">${s.avg_cost_r == null ? '—' : s.avg_cost_r + 'R'}</div></div>
      <div class="tile"><div class="lab">Profit factor</div><div class="big">${s.profit_factor ?? '—'}</div></div>
      <div class="tile"><div class="lab">Max drawdown</div><div class="big neg">${s.max_dd_pct}%</div></div>
    </div>
    <div class="chartbox"><canvas id="btChart" aria-label="Backtest equity curve"></canvas><div class="tip" id="btTip"></div></div>
    <div class="legend" style="margin:4px 0 10px"><span><i style="background:var(--up)"></i>Paper equity (1% risk per trade)</span><span><i style="background:var(--ref);height:0;border-top:2px dashed var(--ref)"></i>Starting capital</span></div>
    <div class="tiles">
      <div class="tile"><div class="lab">Every-bar benchmark</div><div class="big ${(r.every_bar.avg_r_net || 0) >= 0 ? 'pos' : 'neg'}">${fmt.r(r.every_bar.avg_r_net)}</div><div class="note">${r.every_bar.trades} trades · ${s.avg_r_net != null && r.every_bar.avg_r_net != null ? (s.avg_r_net > r.every_bar.avg_r_net ? 'setup beats "just be long"' : 'setup does NOT beat "just be long"') : ''}</div></div>
      <div class="tile"><div class="lab">Buy & hold (test part)</div><div class="big">${fmt.pct(r.buy_hold.return_pct)}</div><div class="note">daily Sharpe ${r.buy_hold.sharpe_daily ?? '—'} vs strategy ${r.strategy_sharpe_daily_test ?? '—'}</div></div>
      <div class="tile"><div class="lab">Random-entry placebo</div><div class="big">${r.placebo_pct == null ? '—' : r.placebo_pct + 'th'}</div><div class="note">percentile (need 95+)</div></div>
      <div class="tile"><div class="lab">PBO (overfit odds)</div><div class="big">${r.pbo ?? '—'}</div><div class="note">across ${r.pbo_variants.length} sibling variants</div></div>
    </div>
    <h4 style="font:700 11px var(--head);color:var(--glow2);letter-spacing:.12em">QUALITY GATE ON UNSEEN DATA (${r.test.trades} test trades · ${r.n_trials_counted} tries counted)</h4>${gateTable(r.gate)}
    <h4 style="font:700 11px var(--head);color:var(--glow2);letter-spacing:.12em;margin-top:12px">SIGNALS SKIPPED BY THE RULES</h4>
    <div>${Object.entries(r.skipped).map(([k, v]) => `<span class="pill">${esc(k)}: ${v}</span>`).join('') || '<span class="note dim">none</span>'}</div>
    <h4 style="font:700 11px var(--head);color:var(--glow2);letter-spacing:.12em;margin-top:12px">LAST TRADES</h4>
    <table class="t"><thead><tr><th>ENTRY (MT)</th><th>ENTRY</th><th>EXIT</th><th>REASON</th><th>GROSS</th><th>COST</th><th>NET</th></tr></thead><tbody>${r.trades.slice(-40).reverse().map(x => `<tr><td>${esc(fmt.mt(x.entry_t))}</td><td class="num">${fmt.px(x.entry)}</td><td class="num">${fmt.px(x.exit)}</td><td>${esc(x.reason)}</td><td class="num">${fmt.r(x.r_gross)}</td><td class="num">${x.cost_r}R</td><td class="num ${x.r_net >= 0 ? 'pos' : 'neg'}">${fmt.r(x.r_net)}</td></tr>`).join('')}</tbody></table>
    <p class="note warn" style="margin-top:8px">A backtest is not a promise. The best of many tries is always flattered — every try is counted in the Deflated Sharpe.</p></div>`;
  equityChart($('#btChart'), r.equity || [], 10000, $('#btTip'));
}
function equityChart(cv, pts, base, tip) {
  const draw = (hover) => { const [g, w, h] = hidpi(cv); g.clearRect(0, 0, w, h);
    if (pts.length < 2) { g.fillStyle = C.muted; g.font = '600 12px Rajdhani'; g.fillText('No trades', 10, 20); return; }
    const ys = pts.map(p => p[1]); let lo = Math.min(base, ...ys), hi = Math.max(base, ...ys); const pd = (hi - lo) * .08 || 1; lo -= pd; hi += pd;
    const padR = 62, W = w - padR, X = i => i / (pts.length - 1) * W, Y = v => 6 + (hi - v) / (hi - lo) * (h - 24);
    g.font = '600 10.5px ' + css('--mono'); g.fillStyle = C.ink2; g.strokeStyle = 'rgba(64,160,255,.1)';
    for (let k = 0; k <= 3; k++) { const v = lo + (hi - lo) * k / 3; g.beginPath(); g.moveTo(0, Y(v)); g.lineTo(W, Y(v)); g.stroke(); g.fillText('$' + Math.round(v).toLocaleString('en-US'), W + 4, Y(v) + 3); }
    g.strokeStyle = C.ref; g.setLineDash([5, 4]); g.lineWidth = 1.2; g.beginPath(); g.moveTo(0, Y(base)); g.lineTo(W, Y(base)); g.stroke(); g.setLineDash([]);
    g.strokeStyle = C.up; g.lineWidth = 2; g.lineJoin = 'round'; g.beginPath(); pts.forEach((p, i) => i ? g.lineTo(X(i), Y(p[1])) : g.moveTo(X(i), Y(p[1]))); g.stroke();
    g.fillStyle = C.ink2; g.fillText(fmt.mt(pts[0][0]), 0, h - 4); const lt = fmt.mt(pts[pts.length - 1][0]); g.fillText(lt, W - g.measureText(lt).width, h - 4);
    if (hover != null) { const i = hover, x = X(i); g.strokeStyle = 'rgba(201,214,234,.5)'; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h - 18); g.stroke(); g.setLineDash([]);
      g.fillStyle = '#fff'; g.beginPath(); g.arc(x, Y(pts[i][1]), 4, 0, 7); g.fill(); g.strokeStyle = '#06132b'; g.lineWidth = 2; g.stroke(); } };
  draw(null);
  cv.onmousemove = e => { const rect = cv.getBoundingClientRect(), W = rect.width - 62; const i = Math.max(0, Math.min(pts.length - 1, Math.round((e.clientX - rect.left) / W * (pts.length - 1))));
    draw(i); if (tip && pts[i]) { tip.style.display = 'block'; tip.innerHTML = `<b>${esc(fmt.mt(pts[i][0]))} MT</b><br>Equity ${fmt.usd(pts[i][1])} (${fmt.pct((pts[i][1] / base - 1) * 100)})`; tip.style.left = Math.min(e.clientX - rect.left + 12, rect.width - 170) + 'px'; tip.style.top = '8px'; } };
  cv.onmouseleave = () => { draw(null); if (tip) tip.style.display = 'none'; };
}

SHEETS.fleet = { title: 'FLEET · 99 BOTS · LIVE FEED', live: true, render(poll) {
  const st = S.st; if (!st) return; const o = S.sheetOpts;
  const feed = Object.fromEntries(st.fleet.map(r => [r.bot_id, r])); const paused = new Set(st.paused);
  let rows = S.lib.bots.map(b => ({ ...b, ...(feed[b.bot_id] || {}), status: (feed[b.bot_id] || {}).status || b.status }));
  if (o.group) rows = rows.filter(r => r.group === o.group); if (o.signal) rows = rows.filter(r => r.last_signal === 'LONG');
  if (o.status) rows = rows.filter(r => r.status === o.status);
  const groups = ['US stocks', 'Crypto', 'Meme coins'], statuses = Array.from(new Set(S.lib.bots.map(b => b.status)));
  const scroll = poll ? body().scrollTop : 0;
  body().innerHTML = `<div class="tabs"><button data-g="" class="${!o.group ? 'on' : ''}">ALL</button>${groups.map(g => `<button data-g="${g}" class="${o.group === g ? 'on' : ''}">${g.toUpperCase()}</button>`).join('')}
    <span style="width:12px"></span>${statuses.map(s => `<button data-s="${s}" class="${o.status === s ? 'on' : ''}">${s}</button>`).join('')}
    <button data-sig="1" class="${o.signal ? 'on' : ''}">SIGNALS ONLY</button></div>
    <p class="note">Answers come from <b>policy.decide</b> every cycle. Bots whose role has not passed the gate can only shadow-trade (no money). Pausing is risk-reducing; resuming is only possible at the keyboard (delete the file in state/paused/).</p>
    <table class="t"><thead><tr><th>BOT</th><th>SYMBOL</th><th>ROLE</th><th>STATUS</th><th>VERDICT</th><th>STRATEGY</th><th>SIGNAL</th><th>ANSWER</th><th>VOL</th><th>HEARTBEAT</th><th></th></tr></thead><tbody>
    ${rows.map(r => `<tr class="click" data-sym="${esc(r.symbol)}"><td>${r.bot_id}</td><td>${esc(r.symbol)}</td><td>${esc(r.role)}</td><td><span class="tag ${STATUS_CLS(r.status)}">${esc(r.status)}</span></td><td>${esc(r.verdict)}</td>
      <td style="font-size:11px">${esc((r.strategy || r.candidate_1 || '').replace(/\./g, ' · '))}</td><td><span class="tag ${r.last_signal || 'NONE'}">${esc(r.last_signal || '—')}</span></td>
      <td><span class="tag ${r.decision || 'WAIT'}">${esc(r.decision || '—')}</span> <span class="dim" style="font-size:11px">${esc(r.reason || (r.status === 'PARKED' ? 'parked (off)' : ''))}</span>${r.error ? `<br><span style="color:var(--crit);font-size:11px">${esc(r.error)}</span>` : ''}</td>
      <td>${r.vol_state ? `<span class="tag ${r.vol_state}">${r.vol_state}</span>` : '—'}</td><td class="num">${r.heartbeat_age_s != null ? r.heartbeat_age_s + 's' : '—'}</td>
      <td>${paused.has(r.bot_id) ? '<span class="tag fail">PAUSED</span>' : `<button class="btn sm" data-pause="${r.bot_id}">PAUSE</button>`}</td></tr>`).join('')}</tbody></table>`;
  body().scrollTop = scroll;
  body().querySelectorAll('[data-g]').forEach(b => b.onclick = () => { o.group = b.dataset.g; this.render(); });
  body().querySelectorAll('[data-s]').forEach(b => b.onclick = () => { o.status = o.status === b.dataset.s ? '' : b.dataset.s; this.render(); });
  body().querySelectorAll('[data-sig]').forEach(b => b.onclick = () => { o.signal = !o.signal; this.render(); });
  body().querySelectorAll('[data-pause]').forEach(b => b.onclick = async e => { e.stopPropagation(); if (!confirm(`Pause ${b.dataset.pause}? Only you can resume it, by hand.`)) return;
    const r = await api('/api/pause', { bot_id: b.dataset.pause }); toast('Pause', r.ok ? `${b.dataset.pause} paused` : 'failed', r.ok ? '' : 'warning'); refresh(); });
  body().querySelectorAll('tr[data-sym]').forEach(tr => tr.onclick = () => { selectSymbol(tr.dataset.sym); closeSheet(); });
} };

SHEETS.journal = { title: 'POSITIONS & JOURNAL', live: true, render(poll) {
  const st = S.st; if (!st) return;
  const pos = st.positions, sh = st.shadow_positions, cl = (st.paper_closed || []).slice().reverse(), scl = (st.shadow_closed || []).slice().reverse();
  const ptable = (rows, empty) => rows.length ? `<table class="t"><thead><tr><th>BOT</th><th>SYMBOL</th><th>ENTRY</th><th>STOP</th><th>TARGET</th><th>LAST</th><th>R NOW</th><th>FLAT BY</th><th>STRATEGY</th></tr></thead><tbody>${rows.map(p => `<tr><td>${esc(p.bot)}</td><td>${esc(p.symbol)}</td><td class="num">${fmt.px(p.entry)}</td><td class="num">${fmt.px(p.stop)}</td><td class="num">${fmt.px(p.target)}</td><td class="num">${fmt.px(p.last)}</td><td class="num ${p.r_now >= 0 ? 'pos' : 'neg'}">${fmt.r(p.r_now)}</td><td>${esc(p.flat_by)}</td><td style="font-size:11px">${esc(p.strategy)}</td></tr>`).join('')}</tbody></table>` : `<p class="note dim">${empty}</p>`;
  const ctable = (rows, empty) => rows.length ? `<table class="t"><thead><tr><th>DATE</th><th>BOT</th><th>SYMBOL</th><th>ENTRY</th><th>EXIT</th><th>REASON</th><th>NET R</th><th>P&L (PAPER)</th></tr></thead><tbody>${rows.map(c => `<tr><td>${esc(c.date)}</td><td>${esc(c.bot)}</td><td>${esc(c.symbol)}</td><td class="num">${fmt.px(c.entry)}</td><td class="num">${fmt.px(c.exit)}</td><td>${esc(c.reason)}</td><td class="num ${c.r >= 0 ? 'pos' : 'neg'}">${fmt.r(c.r)}</td><td class="num">${fmt.sgn(c.pnl)}</td></tr>`).join('')}</tbody></table>` : `<p class="note dim">${empty}</p>`;
  body().innerHTML = `<div class="card"><h4>PAPER POSITIONS (SIMULATED MONEY)</h4>${ptable(pos, 'No paper positions. Bots trade paper only after their role passes the quality gate.')}</div>
    <div class="card"><h4>PAPER TRADES</h4>${ctable(cl, 'No paper trades yet.')}</div>
    <div class="card"><h4>SHADOW POSITIONS (NO MONEY · BOTS THAT HAVE NOT PASSED)</h4>${ptable(sh, 'No shadow positions right now.')}</div>
    <div class="card"><h4>SHADOW TRADES · COMPARE WITH THE BACKTEST</h4>${ctable(scl, 'No shadow trades closed yet.')}<p class="note">Shadow summary: ${st.shadow.closed_trades} trades · win rate ${st.shadow.win_rate == null ? '—' : (st.shadow.win_rate * 100).toFixed(0) + '%'} · avg ${fmt.r(st.shadow.avg_r)}. A big gap from the backtest means the backtest lied.</p></div>`;
} };

SHEETS.alerts = { title: 'ALERTS & REMINDERS', render() {
  body().innerHTML = `<div class="split"><div><div class="card"><h4>NEW ALERT OR REMINDER</h4><div class="form">
      <label>Type</label><select id="alType"><option value="price_gt">Price goes above</option><option value="price_lt">Price goes below</option><option value="pnl_gt">Paper P&L today above</option><option value="pnl_lt">Paper P&L today below</option><option value="vol">Volatility gate turns</option><option value="signal">Any bot prints a signal</option><option value="once">Reminder (one time)</option><option value="daily">Reminder (every day)</option></select>
      <label>Symbol</label><input id="alSym" value="${esc(S.sym)}">
      <label>Value</label><input id="alVal" placeholder="number, or LOUD / QUIET">
      <label>Time (MT)</label><input id="alTime" type="time"><label>Date</label><input id="alDate" type="date">
      <label>Message</label><input id="alMsg" placeholder="for reminders">
      <label>Deliver to</label><span><label><input type="checkbox" id="alDesk" checked style="width:auto"> screen + voice</label> &nbsp; <label><input type="checkbox" id="alPhone" style="width:auto"> phone call (your number only)</label></span>
    </div><div style="margin-top:10px;display:flex;gap:8px;align-items:center"><button class="btn" id="alSave">SAVE RULE</button><span class="note" id="alMsgOut"></span></div>
    <p class="note">Rules are evaluated by code with exact decimals. "Above" means &gt;, never ≥. A phone call never says anything private — it asks you to call back and enter your PIN.</p></div>
    <div class="card"><h4>RULES</h4><div id="alRules">Loading…</div></div></div>
    <div><div class="card"><h4>PHONE</h4><div id="alPhoneSt" class="note"></div></div><div class="card"><h4>RECENT NOTIFICATIONS</h4><div id="alNotes"></div></div></div></div>`;
  const mt = new Date(Date.now() + 3600e3).toLocaleString('sv-SE', { timeZone: 'America/Edmonton' });   // 'YYYY-MM-DD HH:MM:SS' in MT
  $('#alDate').value = mt.slice(0, 10); $('#alTime').value = mt.slice(11, 16);
  $('#alSave').onclick = async () => { const ty = $('#alType').value, sym = $('#alSym').value.trim().toUpperCase(), val = $('#alVal').value.trim(); let cond, label;
    if (ty.startsWith('price')) { cond = { kind: 'metric_threshold', metric: 'price:' + sym, operator: ty === 'price_gt' ? '>' : '<', threshold: val, initial_true_policy: 'wait_for_crossing' }; label = `${sym} ${ty === 'price_gt' ? 'above' : 'below'} ${val}`; }
    else if (ty.startsWith('pnl')) { cond = { kind: 'metric_threshold', metric: 'paper_pnl_today', operator: ty === 'pnl_gt' ? '>' : '<', threshold: val, initial_true_policy: 'wait_for_crossing' }; label = `Paper P&L ${ty === 'pnl_gt' ? 'above' : 'below'} ${val}`; }
    else if (ty === 'vol') { cond = { kind: 'vol_state', symbol: sym, state: val.toUpperCase() || 'LOUD' }; label = `${sym} turns ${val.toUpperCase() || 'LOUD'}`; }
    else if (ty === 'signal') { cond = { kind: 'any_signal' }; label = 'Bot signal'; }
    else { cond = { kind: 'scheduled', local_time: $('#alTime').value, recurrence: ty === 'once' ? 'once' : 'daily', message: $('#alMsg').value || 'Reminder', ...(ty === 'once' ? { local_date: $('#alDate').value } : {}) }; label = 'Reminder'; }
    const ch = []; if ($('#alDesk').checked) ch.push('desktop'); if ($('#alPhone').checked) ch.push('phone');
    const r = await api('/api/alerts', { condition: cond, channels: ch.length ? ch : ['desktop'], label });
    $('#alMsgOut').textContent = r.ok ? r.data.readback : ((r.error && r.error.message) || r.error); if (r.ok) this.load(); };
  this.load();
}, async load() {
  const a = await api('/api/alerts'); const el = $('#alRules'); if (!el) return;
  el.innerHTML = (a.rules || []).map(r => `<div class="fam" style="grid-template-columns:1fr 70px 64px"><span>${esc(r.text)}<br><span class="dim" style="font-size:11px">${esc(r.state.note || '')}${r.state.last_value ? ' · last ' + esc(r.state.last_value) : ''}</span></span><span class="tag ${r.enabled ? 'pass' : 'na'}">${r.enabled ? 'ON' : 'OFF'}</span>${r.enabled ? `<button class="btn sm" data-off="${r.id}">TURN OFF</button>` : '<span></span>'}</div>`).join('') || '<p class="note dim">No rules yet. Try saying “Jarvis, remind me at 3 pm to check the report”.</p>';
  el.querySelectorAll('[data-off]').forEach(b => b.onclick = async () => { await api('/api/alerts/disable', { id: b.dataset.off }); this.load(); });
  const p = a.phone || {}; $('#alPhoneSt').innerHTML = p.ready ? 'Phone calls: <b>READY</b> (your own number only, capped, quiet hours respected).' : `Phone calls: <b>${esc(p.state || 'SETUP_REQUIRED')}</b>${p.missing ? '<br>Missing: ' + esc(p.missing.join(', ')) : ''}<br>See README → Phone setup.`;
  $('#alNotes').innerHTML = (S.st.notifications || []).slice(0, 15).map(n => `<div class="note" style="margin-bottom:6px"><b>${esc(n.title)}</b> · ${esc(n.body)} <span class="dim">${new Date(n.ts * 1000).toLocaleTimeString()}</span></div>`).join('') || '<p class="note dim">Nothing yet.</p>';
} };

SHEETS.memory = { title: 'MEMORY · WHAT JARVIS REMEMBERS', render() {
  body().innerHTML = `<div class="card"><h4>ADD</h4><div style="display:flex;gap:8px"><input id="memT" placeholder="e.g. I prefer short answers in the morning" style="flex:1;background:rgba(0,0,0,.4);border:1px solid var(--line2);padding:6px 8px">
    <select id="memK" style="background:rgba(0,0,0,.4);border:1px solid var(--line2)"><option>preference</option><option>project</option><option>fact</option><option>rule</option></select><button class="btn" id="memAdd">REMEMBER</button><button class="btn" id="memExp">EXPORT</button></div>
    <p class="note">Jarvis never stores passwords, PINs, keys, raw audio, or balances as "current truth" — those are always read live. Conversation history is kept ${'30'} days.</p></div><div class="card"><h4>MEMORIES</h4><div id="memL">Loading…</div></div>`;
  $('#memAdd').onclick = async () => { const t = $('#memT').value.trim(); if (!t) return; await api('/api/memory', { text: t, kind: $('#memK').value }); $('#memT').value = ''; this.load(); };
  $('#memExp').onclick = async () => { const r = await fetch('/api/memory/export', { headers: { 'X-Jarvis-Token': TOKEN } }); const b = await r.blob(); const a = document.createElement('a'); a.href = URL.createObjectURL(b); a.download = 'jarvis_memory.json'; a.click(); };
  this.load();
}, async load() {
  const m = await api('/api/memory'); const el = $('#memL'); if (!el) return;
  el.innerHTML = (m.memories || []).map(x => `<div class="fam" style="grid-template-columns:90px 1fr 60px 60px"><span class="tag NORMAL">${esc(x.kind)}</span><span>${esc(x.text)}<br><span class="dim" style="font-size:11px">${esc(x.source)} · ${new Date(x.updated_at * 1000).toLocaleString()}${x.corrected ? ' · corrected' : ''}</span></span><button class="btn sm" data-ed="${x.id}">EDIT</button><button class="btn sm halt" data-del="${x.id}">FORGET</button></div>`).join('') || '<p class="note dim">Nothing remembered yet. Say “Jarvis, remember that …”.</p>';
  el.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => { await api('/api/memory/delete', { id: b.dataset.del }); this.load(); });
  el.querySelectorAll('[data-ed]').forEach(b => b.onclick = async () => { const t = prompt('Correct this memory:'); if (t) { await api('/api/memory/correct', { id: b.dataset.ed, text: t }); this.load(); } });
} };

SHEETS.activity = { title: 'ACTIVITY · WHAT THE SYSTEM AND THE AI ACTUALLY DID', render() {
  const tab = S.sheetOpts.tab || 'events';
  body().innerHTML = `<div class="tabs">${[['events', 'FLEET EVENTS'], ['tools', 'AI TOOL CALLS'], ['chat', 'CONVERSATION'], ['audit', 'AUDIT CHAIN']].map(([k, l]) => `<button data-tab="${k}" class="${tab === k ? 'on' : ''}">${l}</button>`).join('')}</div><div id="actB">Loading…</div>`;
  body().querySelectorAll('[data-tab]').forEach(b => b.onclick = () => { S.sheetOpts.tab = b.dataset.tab; this.render(); });
  api('/api/activity').then(a => { const el = $('#actB'); if (!el) return;
    if (tab === 'events') el.innerHTML = `<table class="t"><thead><tr><th>TIME</th><th>BOT</th><th>EVENT</th><th>CORRELATION ID</th></tr></thead><tbody>${(a.events || []).map(e => `<tr><td>${esc(e.t)}</td><td>${esc(e.bot)}</td><td>${esc(e.event)}</td><td class="num">${esc(e.cid || '')}</td></tr>`).join('')}</tbody></table>${(a.events || []).length ? '' : '<p class="note dim">No events yet — they appear when bots print signals, fill orders or get paused.</p>'}`;
    if (tab === 'tools') el.innerHTML = `<table class="t"><thead><tr><th>TIME</th><th>CHANNEL</th><th>TOOL</th><th>TIER</th><th>OK</th><th>ARGS / ERROR</th></tr></thead><tbody>${(a.tool_audit || []).map(t => `<tr><td>${new Date(t.ts * 1000).toLocaleTimeString()}</td><td>${esc(t.channel)}</td><td>${esc(t.tool)}</td><td>${esc(t.tier)}</td><td>${t.ok ? '<span class="tag pass">OK</span>' : '<span class="tag fail">DENIED</span>'}</td><td style="font-size:11px">${esc(t.ok ? t.args : t.error)}</td></tr>`).join('')}</tbody></table>`;
    if (tab === 'chat') el.innerHTML = (a.messages || []).map(m => `<div class="note" style="margin-bottom:8px"><b style="color:${m.role === 'user' ? 'var(--glow2)' : '#fff'}">${m.role === 'user' ? 'YOU' : 'JARVIS'}</b> <span class="dim">${esc(m.channel)} · ${new Date(m.created_at * 1000).toLocaleString()}</span><br>${esc(m.content)}</div>`).join('') || '<p class="note dim">No conversation yet.</p>';
    if (tab === 'audit') el.innerHTML = `<p class="note">The decision log (state/audit.jsonl) is a hash chain: editing any past line breaks it.</p><div class="big" style="margin:10px 0">${a.audit_chain_ok ? '<span class="tag pass" style="font-size:14px">CHAIN INTACT</span>' : '<span class="tag fail" style="font-size:14px">CHAIN BROKEN</span>'}</div>`; });
} };

SHEETS.help = { title: 'HELP · HOW TO USE JARVIS', render() {
  body().innerHTML = `<div class="split"><div>
    <div class="card"><h4>TALK TO JARVIS</h4><div class="note">Click once to activate, then say <b>“Jarvis, …”</b>. After he answers you have 12 seconds to follow up without the wake word. Talk over him to interrupt. Or press <kbd>Space</kbd> to talk, or type below.</div>
      <p class="note">Try: “What are my agents doing?” · “Why do people dream?” · “Should I buy Bitcoin?” · “How does the volatility gate work?” · “Backtest RSI-2 on ETH” · “Run the role test for crypto majors” · “Remind me at 2:30 pm to read the report” · “Tell me when Bitcoin goes above 90,000” · “What changed in AI news today?” · “Remember I prefer short answers” · “Halt everything” · “Pause A073”.</p></div>
    <div class="card"><h4>KEYBOARD</h4><div class="kv"><span><kbd>Space</kbd></span><span>talk / interrupt</span><span><kbd>M</kbd></span><span>mute microphone</span><span><kbd>V</kbd></span><span>voice output on/off</span><span><kbd>W</kbd></span><span>wake word on/off</span><span><kbd>H</kbd></span><span>HALT (asks first)</span><span><kbd>1</kbd>–<kbd>8</kbd></span><span>open pages</span><span><kbd>Esc</kbd></span><span>close page</span><span><kbd>?</kbd></span><span>this help</span></div></div>
  </div><div>
    <div class="card"><h4>SAFETY — WHAT CODE DECIDES</h4><div class="note">
      • <b>Paper trading only.</b> There is no order function, no broker key, and no endpoint that moves money.<br>
      • <b>The AI proposes, code decides.</b> BUY/WAIT comes from policy.decide; Jarvis only reports it.<br>
      • <b>Jarvis can only cut risk:</b> HALT and PAUSE. Nothing resumes by voice or text.<br>
      • <b>HALT</b> creates state/HALT. Only you clear it — delete that file by hand.<br>
      • <b>Fail closed:</b> missing, stale or odd data = WAIT.<br>
      • <b>Honest status:</b> 0 of 99 bots have passed the quality gate, so every live answer is WAIT until a role passes the Role Test. Nothing here has shown profit after costs.</div></div>
    <div class="card"><h4>WHAT THE PANELS MEAN</h4><div class="note">Portfolio = simulated money. Market Watch = live display prices (decisions use closed bars). Volatility Gate = size of the next 12h move, never direction. Risk Engine = hard limits from code. Strategy Systems = the library by family. Fleet Diagnostics = all 99 bots (white outline = signal now, red ring = paused). Targeting = the code's BUY/WAIT for the locked symbol.</div></div>
  </div></div>`;
} };

/* ============================ wiring ============================ */
function ensureStratOption(id) { if (![...$('#chStrat').options].some(o => o.value === id)) $('#chStrat').add(new Option('Signals: ' + id, id)); }
async function boot() {
  requestAnimationFrame(frame);
  S.lib = await api('/api/strategies');
  const syms = Array.from(new Set(['BTC-USD', 'ETH-USD', 'SOL-USD'].concat((S.lib.bots || []).map(b => b.symbol))));
  syms.forEach(s => $('#chSym').add(new Option(s, s))); if (!syms.includes(S.sym)) S.sym = 'BTC-USD'; $('#chSym').value = S.sym;
  S.lib.keep.concat(S.lib.new.filter(n => n.runnable)).forEach(s => $('#chStrat').add(new Option('Signals: ' + s.id, s.id)));
  $('#chSym').onchange = () => selectSymbol($('#chSym').value);
  $('#chStrat').onchange = () => { S.strat = $('#chStrat').value; loadChart(); };
  $$('#barSeg button').forEach(b => b.onclick = () => setBar(b.dataset.b));
  const hv = $('#chartHover'); hv.onmousemove = chartHover; hv.onmouseleave = () => chartHover(null);
  $('#q').addEventListener('keydown', e => { if (e.key === 'Enter') { ask($('#q').value, 'gui'); $('#q').value = ''; } });
  $('#send').onclick = () => { ask($('#q').value, 'gui'); $('#q').value = ''; };
  $('#mic').onclick = pushToTalk; $('#orbwrap').onclick = pushToTalk;
  $('#haltBtn').onclick = haltNow;
  $('#wakeBtn').onclick = () => { V.wake = !V.wake; $('#wakeBtn').textContent = 'WAKE: ' + (V.wake ? 'ON' : 'OFF'); };
  $('#sfxBtn').onclick = () => { V.voiceOut = !V.voiceOut; $('#sfxBtn').textContent = 'VOICE: ' + (V.voiceOut ? 'ON' : 'OFF'); if (!V.voiceOut && 'speechSynthesis' in window) speechSynthesis.cancel(); };
  $$('.dk').forEach(d => d.onclick = () => S.sheet === d.dataset.p ? closeSheet() : openSheet(d.dataset.p));
  $('#sheetClose').onclick = closeSheet;
  $('#activate').onclick = activate;
  addEventListener('keydown', e => {
    if (!V.active && e.key !== 'Tab') { activate(); }
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName)) { if (e.key === 'Escape') document.activeElement.blur(); return; }
    const k = e.key.toLowerCase();
    if (k === ' ') { e.preventDefault(); pushToTalk(); } else if (k === 'm') toggleMute(); else if (k === 'h') haltNow();
    else if (k === 'v') $('#sfxBtn').click(); else if (k === 'w') $('#wakeBtn').click(); else if (k === 'escape') closeSheet(); else if (k === '?') openSheet('help');
    else if (/^[1-8]$/.test(k)) { const p = $$('.dk')[+k - 1]; if (p) p.click(); }
  });
  addEventListener('resize', () => { drawChart(); });
  await refresh(); loadChart();
  setInterval(refresh, 2500); setInterval(loadChart, 60000);
}
async function haltNow() {
  if (!confirm('HALT EVERYTHING?\n\nCreates the HALT flag: no new trades, paper positions close next cycle. Only you can clear it by deleting state/HALT by hand.')) return;
  const r = await api('/api/halt', { reason: 'HALT button in portal' }); toast('HALT', r.ok ? 'HALT flag is ON.' : 'failed', 'critical'); refresh();
}
boot();
