# Jarvis Trading Portal - architecture

How the pieces fit, following the JARVIS PRIME spec (section numbers are quoted in the code docstrings).
Everything below is read from the source as of 2026-10-15. **Paper trading only**: there is no broker connection,
no order tool, and no endpoint that clears HALT or resumes a bot.

**Principle: the AI proposes, code decides.** Jarvis (the language model) can read, explain, research, remember,
schedule alerts, start sandbox backtests and **cut** risk. Every BUY/WAIT comes from `kit/policy.py`.

---

## 1. Big picture

```
                    Browser (Chrome/Edge)                         Telephone (optional, Twilio)
          web/index.html: HUD panels, chart, mic (Web Speech)        caller -> PIN (keypad) -> speech
                       |  X-Jarvis-Token, Host/Origin checked                 |  Twilio signature checked
                       v                                                      v
   +--------------------------------------------- server.py (127.0.0.1:PORT) -----------------------------+
   |  GET /api/state /api/bars /api/fleet ...      POST /api/ask /api/halt /api/backtest ...   /phone/*    |
   +-------------------------------|------------------------------|------------------------------|-------+
                                   v                              v                              v
                         +----------------------------- jarvis/app.py  App ----------------------------+
                         |  JarvisCore (core.py)  --fixed commands-->  kit/voice_router.py (no AI)    |
                         |        |  LLM tool loop                                                     |
                         |        v                                                                    |
                         |  Brain (brain.py): AnthropicBrain strong/fast | offline mode | FakeBrain    |
                         |        |  tool_use                                                          |
                         |        v                                                                    |
                         |  Gateway (gateway.py): typed tools, tiers, channel + auth check, audit     |
                         |        |                                                                    |
                         |  Store (memory.py, SQLite)   TaskManager (tasks.py)   AlertEngine (alerts) |
                         |  OutboundCallManager / PinAuth / InboundCalls (phone.py)                    |
                         +--------|-------------------------------------------------------------------+
                                  v   read-only views + HALT/PAUSE + sandbox jobs
   +------------------------------------------ engine/ (source of truth) -----------------------------------+
   |  data_feed.py  Coinbase (crypto) | Yahoo (stocks, prototype) | demo_bars (simulated, labelled DEMO)    |
   |  strategies.py + indicators.py   library.py (xlsx -> library.json)   volatility.py (LOUD/NORMAL/QUIET) |
   |  fleet.py  -> kit/policy.decide -> paper_broker.Book("paper") or Book("shadow")                      |
   |  backtest.py + validation.py -> kit/quality_gate.run_gate                                              |
   +-------------------------------------------------------------------------------------------------------+
                                  v
                state/  HALT  paused/  audit.jsonl  fleet.json  report.json  *_book.json  jarvis.db ...
```

---

## 2. Who is responsible for what

| Jarvis Core (`jarvis/`) | Engine (`engine/` + `kit/`) |
|---|---|
| Conversation, sessions, short-term context | Market data, closed bars, freshness, NYSE calendar |
| Model routing, web research, citations | Strategy signals (closed bars only, long only) |
| Typed tool gateway with permission tiers | `policy.decide` BUY/WAIT (10 checks) - the only decision authority |
| Long-term memory the owner approved | Paper and shadow books, fills, stops, flat rules |
| Alert rules, reminders, notifications, phone calls | Auto-pause rules, HALT flag, pause files, hash-chain audit log |
| Background tasks (backtests, role tests) | Backtester, placebo, PBO, trial counter, quality gate |
| Speaking style (`speakable()`), offline mode | Daily report (`report.json`) |

Jarvis never computes a trading number itself. Tools return engine data with `source`, `as_of`, `retrieved_at` and `stale`.

---

## 3. Tool gateway (`jarvis/gateway.py`)

The **only** way Jarvis touches the app. Before every call, code checks: the tool exists, the session's level allows its
tier, the channel allows it, and the arguments pass a strict schema (`validate()`: types, required keys, enums,
bounds, patterns, **no extra keys**). Every call (allowed or refused) is written to the `tool_audit` table.
There is no shell, no file access, no arbitrary HTTP, no broker credentials, and no order tool.

Levels: `anonymous` -> T0 · `owner_read` -> T0, T1, RR · `owner_operate` -> T0, T1, RR, T2.

| Tier | Meaning | Channels | Tools |
|---|---|---|---|
| **T0** | Public info | gui, desktop_voice, telephone | `get_time`, `get_quote`, `get_market_overview`, `get_volatility_gate` |
| **T1** | Owner read | gui, desktop_voice, telephone | `get_system_status`, `get_portfolio`, `get_daily_report`, `list_agents`, `get_agent`, `get_fleet_summary`, `get_risk_status`, `get_trade_decision`, `search_strategies`, `get_recent_activity`, `get_shadow_log`, `get_tasks`, `list_alert_rules`, `recall_memories` |
| **RR** | Risk-reducing | gui, desktop_voice, telephone | `halt_fleet` (creates HALT), `pause_agent` (creates a pause file) |
| **T2** | Owner operate (desktop only) | gui, desktop_voice | `run_backtest`, `run_role_test`, `create_alert_rule`, `disable_alert_rule`, `remember`, `forget_memory`, `open_panel` |

27 app tools in total. With `internet_research: true` the model also gets Anthropic's server-side
`web_search` (max 4 uses per call) and `web_fetch` (max 3). The model is shown only the tools its session may call
(`for_session()`), and `call()` re-checks anyway, so a model that "asks" for a forbidden tool gets `UNAUTHORIZED` or
`CHANNEL_DENIED`. Error codes: `UNKNOWN_TOOL`, `UNAUTHORIZED`, `CHANNEL_DENIED`, `BAD_ARGUMENTS`, `NOT_FOUND`, `UNAVAILABLE`.

---

## 4. Sessions and authentication per channel

| Channel | How you get in | Level given by the server | Can do |
|---|---|---|---|
| `gui` (typed in the portal) | Page on 127.0.0.1 + per-run token | `owner_operate` | T0, T1, RR, T2 |
| `desktop_voice` (mic in the portal) | Same as gui | `owner_operate` | T0, T1, RR, T2 |
| `telephone` (Twilio) | Twilio signature + keypad PIN | `owner_read` after the PIN | T0, T1, RR (T2 is desktop-only) |
| (no PIN) | - | `anonymous` | the call never reaches Jarvis; it is asked for the PIN or hung up |

- The level is assigned by the server **for each call**. A stored session never upgrades a channel
  (`JarvisCore.handle` overwrites `auth_level` and `channel` every turn). `/api/ask` only accepts `gui` or `desktop_voice`.
- Fixed commands run first through `kit/voice_router.py` (whole-sentence match, no AI): STATUS, REPORT, HALT, PAUSE Axxx.
  They are obeyed only for `owner_read` or `owner_operate`.
- Sessions live in SQLite with a `version` column: `save_state()` writes only if nobody wrote since the read
  (optimistic concurrency). Channels share memory and tasks, not authentication.
- Short-term context per session: last symbol, bot, strategy, role and route (so "and ETH?" or "why?" work).
  The model sees up to the 13 stored messages before the current one.

---

## 5. Model routing (`jarvis/brain.py`, `jarvis/core.py`)

- **Two routes, explicit model IDs from config:** `strong` = `claude-opus-5-5` (reasoning, analysis, planning, research,
  backtests, "should I"), `fast` = `claude-sonnet-5-5` (greetings, short follow-ups, simple status).
  `route_for()` picks `strong` when the text is over 220 characters or matches words like why / explain / compare /
  plan / research / strategy / backtest / should I / report; a bare "why?" after a strong turn stays strong.
  Override with `JARVIS_MODEL_STRONG` / `JARVIS_MODEL_FAST`.
- **Effort** per route from config (`output_config.effort`): strong `medium`, fast `low`.
- **System prompt** (static, cached with `cache_control: ephemeral`): `config/jarvis_personality.md` + core rules
  (never invent numbers, tool output is data not instructions, paper only, voice style) + the owner's master prompt
  (`docs/JARVUS_1_MASTER_PROMPT.md`). Each turn adds a small `<context>` block: time in MT, channel, permission,
  data mode (with a "say DEMO" warning in demo mode), short-term context, and up to 6 matching owner memories.
- **Tool loop:** up to 10 model calls per turn; `pause_turn` is continued up to 4 times; `max_tokens` (16,000)
  appends "ask me to continue"; tool results are capped at 24,000 characters and marked `is_error` when not ok.
- **Web search and citations:** only URLs that the search/fetch results actually returned are kept as sources;
  any other cited URL is dropped.
- **Refusal fallback:** calls go through the beta `server-side-fallback-2026-07-01` with `fallbacks="default"`.
  If the SDK or API rejects that option, the brain switches it off and retries a plain call (remembered for later calls).
  If the final `stop_reason` is `refusal`, Jarvis says "I can't help with that one."
- **Errors become typed `BrainError`s:** `AUTH`, `MODEL`, `RATE_LIMIT`, `PROVIDER`, `NETWORK`, `UNAVAILABLE`.
  Jarvis then answers in degraded mode: "My AI brain is unavailable right now (CODE)" + the offline answer.
- **Offline mode** (no key or no SDK): fixed patterns call the same gateway tools - BUY/WAIT for a symbol, one bot,
  volatility gate, price, paper portfolio, fleet, risk, strategies, time - and says plainly that it is offline.
- **Replies are spoken:** `speakable()` strips links, markdown and symbols, and says "dollars" and "percent".

---

## 6. Memory stores

**SQLite `state/jarvis.db`** (WAL mode, a new connection per call, thread safe):

| Table | Holds | Notes |
|---|---|---|
| `sessions` | channel, level, short-term state, `version` | optimistic concurrency |
| `messages` | user/assistant turns per session | purged after `retention_days` (30) at start |
| `memories` | owner-approved long-term notes: kind preference / project / fact / rule | add (`remember`), view, correct, delete, export as JSON (Memory page) |
| `tasks` | background jobs: status, progress, result, error | queued/running jobs are marked `interrupted` on restart |
| `alert_rules` | typed rules (JSON), version, enabled, evaluation state | |
| `outbox` | one row per delivery: key `rule:version:occurrence:channel` | dedupe; phone states below |
| `tool_audit` | every gateway call: tool, tier, args, ok, error, ms | purged after `retention_days` |
| `notifications` | desktop notes (info / warning / critical) | shown as toasts in the portal |

**Never stored:** API keys, PINs (only a salted PBKDF2 hash in `state/phone_auth.json`), raw audio, or balances/prices
as "truth" (those are always read live from the engine).

**Engine files in `state/`:** `HALT`, `paused/<bot>`, `audit.jsonl` (kit hash chain), `fleet.json` (Fleet feed),
`fleet_totals.json`, `flags.json` (HALF_SIZE / PAUSE_NEW_ENTRIES), `report.json`, `paper_book.json`, `shadow_book.json`,
`paper_log.csv`, `shadow_log.csv`, `gate_results.json`, `trials.json`, `bars/` (price cache).

---

## 7. Alerts pipeline (`jarvis/alerts.py`)

The AI only turns a request into a **typed rule** and reads it back. **Code** decides when it fires. No free-form expressions.

```
"tell me if BTC goes above 70k"  ->  model calls create_alert_rule (T2, desktop only)
   -> validate(): kind, metric, operator, Decimal threshold, channels, times      (bad -> BAD_ARGUMENTS)
   -> describe(): readback "Notify by desktop when it crosses price of BTC-USD > 70000 (data no older than 90 min)."
   -> alert_rules table
alert_loop every alerts_every_seconds (30):
   evaluate(): expired? -> fetch metric (value, observed time) -> stale? (default 90 min) skip
            -> Decimal compare -> crossing logic -> cooldown (600 s) -> occurrence key
   _deliver(): outbox INSERT OR IGNORE key "rule:version:occurrence:channel"  (each occurrence delivered once)
            desktop -> notification (shown as info during quiet hours)
            phone   -> quiet hours? no call, desktop note instead -> OutboundCallManager.request()
```

Condition kinds: `metric_threshold` (`price:<SYMBOL>`, `paper_pnl_today`, `paper_equity`, `open_positions`, `paused_bots`;
operators `> >= < <=`; `initial_true_policy` = `wait_for_crossing` (default) or `trigger`), `vol_state`
(LOUD/NORMAL/QUIET), `any_signal` (a bot printed a new long signal), `task_completed`, `scheduled`
(`once` with a date - fires within 6 hours of the time, then disables - or `daily`). Reminders use `scheduled`.
The portal's Alerts page calls the same `create_alert_rule` tool through `/api/alerts`.

---

## 8. Phone flows (`jarvis/phone.py`) - built, tested with fakes, never with a real carrier

**Outbound (Jarvis calls you)**
1. Only from an alert rule with the `phone` channel. Only to `JARVUS_MY_NUMBER`.
2. `status()` needs `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `JARVUS_MY_NUMBER`; `calls_off` is a kill switch.
3. Durable outbox row per occurrence key -> caps (2/hour, 6/day) -> state `dispatching` -> Twilio REST call with a
   **generic** message ("You have a new notification. Please call back and enter your PIN").
4. States: `pending` -> `dispatching` -> `confirmed`, or `failed_before_send` (not set up, capped, error), or
   `unknown_outcome` (timeout: maybe placed, so never retried blindly).

**Inbound (you call Jarvis)** - off unless `JARVIS_PHONE_INBOUND=1`, `TWILIO_AUTH_TOKEN` is set and a PIN exists.
```
Twilio POST /phone/voice   (signature = HMAC-SHA1 of JARVIS_PUBLIC_URL + path + sorted params; checked on every webhook)
   -> "Enter your PIN, then press pound"           (call state expires in 10 min)
POST /phone/pin    -> PinAuth.verify (PBKDF2-SHA256, 200,000 rounds, constant-time compare)
   ok  -> authenticated for 15 min -> speech prompt
   bad -> retry; 3 bad tries hang up; 5 bad in 15 min lock the PIN for 15 min. Digits are never logged or sent to the AI.
POST /phone/speech -> JarvisCore.handle(text, channel="telephone", auth_level="owner_read")
   -> spoken reply (max 900 chars) -> "Anything else?"
```

---

## 9. One fleet cycle (`engine/fleet.py`), every `fleet_cycle_seconds` (120)

1. Take the cycle lock; read the HALT flag and `flags.json`.
2. Refresh BTC-USD 1h first (benchmark for altcoins). Order bots by oldest heartbeat. Spend the refresh budget
   (80 bar refreshes on the first cycle, then 25 per cycle) on bots whose last bar is due; stocks are not refreshed while NYSE is closed.
3. For each of the **74 non-PARKED bots** (`run_bot`, own correlation id):
   - get CLOSED bars (no data -> WAIT "Need fresh prices", error recorded);
   - strategy = the role's gate-chosen candidate, else candidate 1 from the bots sheet;
   - volatility state: walk-forward model for BTC/ETH/SOL when enough bars, else the ATR rule;
   - signal on the last closed bar; stop = max(strategy stop, minimum 2/3/4 ATR for stock/crypto/meme), x1.25 if LOUD;
     cost = venue taker fee x2 (`crypto_venue` for crypto, an assumed 0.03%/side for stocks);
   - `policy.Intent` -> **`policy.decide`** (audited when there is a new signal) -> BUY/WAIT + reason + bullets;
   - a new signal on a new bar: **BUY and role passed** -> `paper.place()` (queued for the next bar);
     otherwise -> `shadow.place()` (no money; "live answer WAIT");
   - `on_bars()` for both books: fill pending orders at the **next bar's open** (refused if more than 2% from the last
     close), then manage stop (gaps fill at the open), target, and flat time;
   - write the Fleet feed row: status, verdict, strategy, signal, decision, reason, vol state, cost R, stats,
     `heartbeat_time`, `correlation_id`, `data_stale`.
4. Heartbeat ages; if HALT is on, close all paper positions at the last prices.
5. Equity snapshot; fleet totals (day/week P&L %, open risk, crypto+meme risk share, meme capital %).
6. `_auto_pause()`: `policy.auto_pause_actions()`; fleet HALTs always apply; PAUSE/DEMOTE files only for bots that can trade
   (ACTIVE-PAPER or holding a position), others get a `would_pause` note; HALF_SIZE and PAUSE_NEW_ENTRIES go to
   `flags.json`, which `paper.place()` reads.
7. Write `fleet.json`, `fleet_totals.json`, `report.json`. The App forwards new signal/order events to the alert engine.

Hard-coded "not yet" inputs to `policy.decide` (so it fails closed): `earnings_calendar_supplied=False` (every stock bot
WAITs), `swing_approved=False` (swing mode WAITs), FOMC 11 am - 1 pm MT blackout on the 2026 dates in `fleet.py`.

---

## 10. Backtest and quality-gate pipeline (`engine/backtest.py`, `engine/validation.py`, `kit/quality_gate.py`)

**Single backtest** (Backtest Lab or `run_backtest`) - a background task (2 workers), never changes a bot's permission:
1. Load 6,000 (1h) or 8,000 bars. `backtest.run()`: signals on closed bars, next-bar-open fills, stop-first when a bar
   touches both stop and target, costs on every trade, cost rule (<=0.20R ok, 0.20-0.33R half size, >0.33R skip),
   minimum stop ATR, LOUD = 0.6x size and 1.25x stop, QUIET = skip, no Sat/Sun crypto entries, dead hours 7 pm - midnight MT,
   stock entries 9:35 am - 3:30 pm ET (1h bars until 2:30 pm), flat by 3:55 pm ET, crypto flat in 12 h (96 h swing).
2. `full_report()`: last 40% is out-of-sample; every-bar benchmark ("just be long"); buy-and-hold daily Sharpe;
   random-entry placebo (120 draws) percentile; PBO by CSCV across up to 6 sibling variants of the same generator.
3. The try is counted in `state/trials.json`; the gate uses `n_trials = max(22, total tries)` for the Deflated Sharpe.
4. `quality_gate.run_gate()` on the out-of-sample trades -> verdict + checks table.

**Role Test** (`run_role_test`, build-order step 5) - the only thing that can open paper trading for a role:
1. Up to 20 non-PARKED bots of the role, 3,000 bars each (more than 300 needed).
2. Each candidate (2-3 from the Roles sheet, plus `vol_gate_swing` for crypto majors) on pooled train data (first 60%).
3. Pick the best train mean R; test it **once** on pooled test data (last 40%).
4. PBO across candidates, pooled placebo (60 draws), average buy-and-hold Sharpe, Deflated Sharpe with tries counted.
5. Gate verdict saved to `state/gate_results.json`. Only **PAPER TEST** makes the role's bots ACTIVE-PAPER.

**Gate checks** (all must pass; missing evidence = RESEARCH FURTHER, fail closed): 30+ test trades; 30+ independent
days; Deflated Sharpe 0.95+; mean R above 0 after 2x costs; walk-forward share of profitable slices 0.5+;
sign-flip p 0.05 or less; PBO 0.2 or less; placebo percentile 95+; beats buy-and-hold.

---

## 11. Security controls in `server.py`

- Listens on **127.0.0.1 only**. Nothing on your network can reach it unless you add a tunnel.
- **Per-run token**: `secrets.token_urlsafe(24)`, injected into `web/index.html` (`{{JARVIS_TOKEN}}`). Every `/api/*`
  call except `/api/health` must send `X-Jarvis-Token`; compared in constant time. A new token every start.
- **Host check** (stops DNS-rebinding): Host must be `127.0.0.1:PORT` or `localhost:PORT`, also for the page itself.
- **Origin check** (stops other websites calling the API): if an Origin header is present it must be
  `http://127.0.0.1:PORT` or `http://localhost:PORT`.
- **Headers**: Content-Security-Policy on the page (`default-src 'self'`, scripts/styles self + inline, fonts from Google
  Fonts only, `connect-src 'self'`), `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
- Request bodies capped at 200 KB; bad JSON -> 400; `/static/` refuses `..` paths; errors return a short type + message, no traceback;
  request logging is off (so phone digits never land in a log).
- `/api/ask` forces `auth_level=owner_operate` and only the `gui` / `desktop_voice` channels.
- State-changing endpoints go through the gateway (validated and audited): backtest, role test, alerts, memory add/delete,
  halt, pause. (`/api/memory/correct`, `/api/task/cancel` and `/api/cycle` call the store / task manager / fleet directly.)
- **No** order endpoint, **no** HALT-clear, **no** resume endpoint. `/phone/*` returns 404 unless inbound phone is fully set up,
  and every webhook's Twilio signature is verified.
- Trust boundary: programs already running on this PC as you can still load the page and read the token. The token protects
  against websites and other machines, not against malware on the PC.

**Endpoints.** GET: `/api/health`, `/api/state`, `/api/bars`, `/api/fleet`, `/api/strategies`, `/api/task`, `/api/tasks`,
`/api/alerts`, `/api/memory`, `/api/memory/export`, `/api/activity`, `/api/vol`. POST: `/api/ask`, `/api/backtest`,
`/api/role_test`, `/api/alerts`, `/api/alerts/disable`, `/api/memory`, `/api/memory/delete`, `/api/memory/correct`,
`/api/halt`, `/api/pause`, `/api/task/cancel`, `/api/cycle`. Twilio: `/phone/voice`, `/phone/pin`, `/phone/speech`.

---

## 12. Background loops (`jarvis/app.py`)

| Loop | Every | Does |
|---|---|---|
| quote | 20 s | watchlist quotes from closed 1h bars + a display-only live price (decisions never use it) |
| fleet | `fleet_cycle_seconds` (120) after each cycle | one fleet cycle (section 9); errors become a warning notification |
| alerts | `alerts_every_seconds` (30) | evaluate alert rules; on weekdays at `daily_report_time_mt` (14:15) write the daily report + notification |
| volatility | 15 min | refresh the walk-forward volatility gate for BTC, ETH, SOL |

Data sources (`engine/data_feed.py`): Coinbase Exchange public candles for crypto (no key); Yahoo chart API for stocks
(**prototype only**: unofficial, short intraday history); simulated `demo_bars` in demo mode (labelled DEMO, kept apart
from real data in the cache). Only closed bars are returned; stale = last closed bar older than bar size + 15 minutes.
