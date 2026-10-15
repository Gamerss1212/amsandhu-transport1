# Jarvis Trading Portal - honest status

Written 2026-10-15, at the end of the first build. Do not overstate anything here.

**Bottom line**
- **0 of 99 bots have passed the quality gate.** Every live answer is **WAIT** until a role passes the Role Test.
- **Nothing has shown a profit.** Not in paper, not in a gate-approved test.
- **Paper trading only.** No real-money path exists in the code, by design.

## Status words

| Word | Meaning |
|---|---|
| `specified` | Written down in the docs/spec, not built |
| `implemented` | Code exists; not proven by a test or a real run |
| `tested_with_mocks` | Proven with fakes: simulated prices, a fake AI, a fake phone sender, temporary folders |
| `tested_with_real_integration` | Proven against the real outside service or the owner's real files during the build |
| `blocked` | Cannot be finished without something from the owner or the outside world |
| `not_tested` | Built, but never run in the way the owner will use it |

`tested_with_mocks` rows rely on the automated tests (`python -m unittest discover -s tests`, plus the kit's 42 tests),
which were being written in the same build. If a test for a row fails on your PC, treat that row as `implemented`.

## Capability table

### Market data

| Capability | Status | Evidence / notes |
|---|---|---|
| Crypto bars from Coinbase (public, no key) | tested_with_real_integration | 2026-10-15: 3,000 closed BTC-USD 1h bars in about 2 s. A live fleet cycle fetched all 30 crypto/meme bots with 0 errors. Crypto prices are Coinbase USD pairs, even when fees are modelled on NDAX (which trades in CAD). |
| US stock bars from Yahoo | tested_with_real_integration | AAPL 1h: 5,081 bars. Live cycle: 44 stock bots, 0 errors. **Prototype only**: unofficial, may change or rate-limit; split adjustment not checked; 15m history is about 60 days. |
| Closed-bar-only + stale check (fail closed) | tested_with_mocks | Stale = last closed bar older than bar size + 15 min. Never fills in a missing price. |
| Live price in the ticker (display only) | implemented | Decisions never use it. Not checked on its own. |
| Demo mode (`--demo`, simulated, labelled DEMO) | tested_with_mocks | Server started in demo mode; an offline price answer said "DEMO (simulated)". Demo and real caches are kept apart. |
| NYSE holidays and early closes 2025-2027 | implemented | Typed into `engine/data_feed.py`. **Not verified** against nyse.com. |
| FOMC blackout (11 am - 1 pm MT, 2026 dates) | implemented | Dates in `engine/fleet.py`. **Not verified** against federalreserve.gov. |
| Earnings calendar for stocks | blocked | No source. The fleet passes "no earnings calendar", so **every stock bot WAITs** even if its role passes. |

### Strategies, backtests, gate

| Capability | Status | Evidence / notes |
|---|---|---|
| Library loader (xlsx -> `data/library.json`) | tested_with_real_integration | Owner's real spreadsheets: 100 kept variants, 11 new, 12 do-not-build, 99 bots, 7 roles. |
| Strategy code: 24 kept generators + 7 new | implemented | All 100 kept variants and all 11 new items are runnable (9 as strategies; 2 overlays inside the backtester: the vol-gate on/off switch, and maker-limit entries, which exist in code but are not offered in the Backtest Lab or to Jarvis). Approximations: `attention_momentum` uses a volume spike as the "attention" signal (no social data); `orb.stocks_in_play` tests one stock at a time (no universe ranking). |
| Backtester (next-bar fills, stop-first, costs, cost rule, time rules) | tested_with_mocks | Rules listed in `docs/JARVIS_ARCHITECTURE.md` section 10. |
| Placebo, PBO (CSCV), buy-and-hold, trial counter | tested_with_mocks | Every try is counted in `state/trials.json`; the gate uses at least 22 tries. |
| Role Test (pooled train/test -> quality gate) | tested_with_real_integration | 2026-10-15, "Crypto majors: trend" on real Coinbase BTC/ETH/SOL (3,000 1h bars each, 7.8 s): chose `donchian_trend.adx.trail`; 15 test trades on 8 days; Deflated Sharpe 0.20; PBO 0.386; sign-flip p 0.148 -> **RESEARCH FURTHER**. Run in a scratch folder, so your `state/` still has no gate results. Other 6 roles not run. |
| Quality gate (kit) | tested_with_mocks | Owner's kit tests: `Ran 42 tests ... OK` (re-run 2026-10-15). |
| Volatility gate (walk-forward logistic, LOUD/NORMAL/QUIET) | tested_with_real_integration | Real BTC 1h, 3,000 bars: out-of-sample AUC **0.724 LOUD**, **0.76 QUIET** (1,988 out-of-sample bars). When it is confident: right 64% (LOUD) / 68% (QUIET). Predicts size of moves, never direction. BTC only was measured. |

### Fleet, paper broker, risk

| Capability | Status | Evidence / notes |
|---|---|---|
| Fleet cycle: 74 non-PARKED bots -> `policy.decide` | tested_with_real_integration | 2026-10-15 live cycle in a scratch folder: 74 bots in 31 s, 0 errors, 0 stale, **74 WAIT** (44 "Outside entry hours", 30 "Dead hours 7 pm-12 am MT" - it was 11 pm MT). |
| Fleet feed rows (heartbeat, correlation id) -> `state/fleet.json` | tested_with_mocks | |
| Paper broker (next-bar fill, 2% collar, stop/target/flat, daily limit) | tested_with_mocks | No paper trade has happened for real, because no role has passed. |
| Shadow log for unpassed bots | tested_with_mocks | Shown on the Positions & Journal page for a manual compare with the backtest. No automatic shadow-vs-backtest report. |
| Auto-pause rules + HALF_SIZE + PAUSE_NEW_ENTRIES | tested_with_mocks | Kit rules are tested; HALF_SIZE / PAUSE_NEW_ENTRIES now go through `state/flags.json`. |
| HALT create-only, pause files | tested_with_real_integration | Kit tests + "halt everything" in a live-data run created the HALT file. No code path deletes it. |
| Daily report `report.json` (14:15 MT weekdays + every cycle) | tested_with_mocks | |
| Daily report pushed to phone/Telegram (build step 9) | specified | `kit/jarvus_reporter.py` can send, but the portal does not call it. The portal shows a desktop notification only. |
| Swing mode (96 h) | blocked | The fleet passes `swing_approved=False`, so swing entries WAIT. Turning it on is a code change you make at the keyboard. |

### Jarvis Core

| Capability | Status | Evidence / notes |
|---|---|---|
| Fixed commands STATUS / REPORT / HALT / PAUSE (no AI) | tested_with_real_integration | Kit tests + live-data run: "status" and "halt everything" answered by fixed code. |
| Offline mode (no key) | tested_with_mocks | Live server answered a price question in offline mode, labelled. |
| Claude brain (strong/fast routing, tool loop, effort) | tested_with_mocks | **No API key in the build environment.** Never called the real API. Model IDs `claude-opus-5-5` / `claude-sonnet-5-5` not confirmed on your account. |
| Web search/fetch + citation URL filter | tested_with_mocks | Never searched the real web through Jarvis. |
| Refusal fallback (beta `server-side-fallback-2026-07-01`) + retry without it | not_tested | Needs the real API. Falls back to a plain call if the option is rejected. |
| Tool gateway: tiers, strict schemas, channel checks, audit | tested_with_mocks | 27 tools, no order tool. |
| Sessions (optimistic concurrency), memory, tool audit (SQLite) | tested_with_mocks | |
| Background tasks (2 workers, progress, cancel) | tested_with_mocks | |
| Alerts and reminders (typed rules, Decimal, crossing, stale check, dedupe, quiet hours) | tested_with_mocks | A phone alert in quiet hours is **not** called later; you get a desktop note instead. |

### Portal, voice, phone, packaging

| Capability | Status | Evidence / notes |
|---|---|---|
| Server security (127.0.0.1 only, token, Host + Origin checks, CSP) | tested_with_real_integration | Live server 2026-10-15: no token -> 403, wrong Host -> 403, other-site Origin -> 403, `/static/../` -> 404, phone webhooks -> 404 while off. |
| Portal screen (HUD panels, chart, matrix, dock pages) | not_tested | Built in the same session (`web/index.html`, `app.css`, `app.js`); not checked in a real browser when this was written. Check it yourself in Chrome/Edge. |
| Browser voice (mic, speech-to-text, voice output, wake word) | not_tested | Uses the browser's built-in speech. Never tried with a real microphone. |
| Phone outbound (Twilio, caps, generic message, outbox states) | tested_with_mocks | Fake sender only. **Never placed a real call.** |
| Phone inbound (signature check, keypad PIN, lockout, speech turns) | tested_with_mocks | Fake requests only. Also needs a public tunnel to this PC. **Never answered a real call.** |
| Windows launcher `START_JARVIS.bat` | not_tested | Written on Linux; never run on Windows. |
| Mac/Linux launcher `start_jarvis.sh` | tested_with_real_integration | Ran on Linux 2026-10-15: made `.venv`, installed requirements, started the server in demo mode, health check OK. Not tried on a Mac. |
| Legacy HUD (`hud/`) | tested_with_mocks | Owner's original 8 tests pass (re-run on a copy 2026-10-15). Same port 8765, so do not run both at once. |

## Known issues (found 2026-10-15 while writing these docs; fix pending)

1. **Short price requests can satisfy long ones from the cache.** `DataService.get()` treats a cached series as fresh when it
   has at least `min(n, 50)` bars. Right after start the watchlist loop caches 60 bars and the fleet 400, so a 3,000-bar
   request (volatility gate, fleet volatility states) can get the short series. Seen in the live-data run: after one cycle
   the BTC volatility gate showed no AUC (the simple ATR rule, not the model). It fails toward WAIT, not toward risk,
   and clears once a long request meets an old cache, which can take a while.
2. **Offline price wording.** Offline mode formats prices with `{:,.4g}`, so BTC reads as "6.612e+04".
3. `owner_name` and `timezone` in the config are not read by the code (Mountain Time is built in).

## What the owner still needs to provide

1. **An Anthropic API key** (pay-as-you-go), and a check that both model IDs work on your account.
2. **A Windows 11 try-out**: double-click `START_JARVIS.bat`, open the page in Chrome or Edge, click once, try the mic.
   Send the exact error text if anything fails.
3. **Verify the calendars**: NYSE holidays/early closes in `engine/data_feed.py`, FOMC dates in `engine/fleet.py`.
4. **An earnings calendar source** for US stocks. Until then every stock bot WAITs by rule.
5. **A real stock data feed** (Yahoo is prototype-only), with 6+ months of 15-minute bars for the two 15m stock roles.
6. **Your real crypto fee tier** (NDAX or Kraken Pro) -> `crypto_venue` in the config.
7. **Run the Role Tests** (Backtest Lab, or ask Jarvis) and accept the verdicts. Every try is counted and makes the gate
   stricter, so do not re-run the same test hoping for a better answer.
8. **Phone (optional)**: Twilio account and number, your own number, a PIN (`python server.py --set-pin`), and a decision
   about a tunnel for inbound calls.
9. **Swing mode approval** (code change at the keyboard) if you want the 96-hour BTC swing to ever enter.
10. **Your OK before**: going live (not built), adding any key, changing limits, or installing third-party add-ons.
