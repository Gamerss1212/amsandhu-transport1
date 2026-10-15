# Jarvus - build brief for Claude Code

Owner: Abhi (Edmonton, Mountain Time). Beginner-friendly explanations, short bullets, no jargon.

## What we are building
An AI day-trading system for US stocks, crypto and meme coins. **Jarvus** (an LLM) is the boss/reporter of 99 deterministic bots.
**Principle: the AI proposes, code decides.** The LLM can only reduce risk (pause/halt). Hard limits live in code.

## Non-negotiable rules
- **Paper trading only.** No real orders, no broker/exchange keys in the code, no function that places an order.
- Long only, spot only, hard stop on every trade. No leverage/shorts/options/perps.
- HALT flag is create-only in code. Only the human deletes it, by hand.
- Voice/phone/text may only do STATUS, REPORT, HALT, PAUSE <bot>. Command matching is a fixed list, no LLM.
- Fail closed: any error or missing/stale data = WAIT.
- Never invent prices or numbers; speak only what is in the report file.
- Do not loosen the quality gate or cost rule (cost in R: <=0.20 ok, 0.20-0.33 half size, >0.33 WAIT).

## What is in this folder
- `docs/JARVUS_1_MASTER_PROMPT.md` - Jarvus system prompt (spec of behaviour; section 6 is the BUY/WAIT flow).
- `data/JARVUS_2_STRATEGIES.xlsx` - strategy library (Keep / New (to build) / Do not build / Cost in R / Measured (BTC)).
- `data/JARVUS_3_BOTS.xlsx` - the 99 bots, roles, fleet feed spec, auto-pause rules, next steps.
- `docs/JARVUS_4_VOICE_AND_LAPTOP_GUIDE.md` - voice/phone architecture and levels.
- `docs/JARVUS_5_RISK_CHECKLIST.md` - what can go wrong and the fix.
- `hud/` - **the voice Jarvis dashboard (start here for the "Jarvis" look)**: `server.py` + `hud.html`, Claude brain with tools, browser voice, 8 passing tests (`cd hud && python test_hud.py`). See `hud/README.md`.
- `kit/` - working starter code, 42 passing tests (`cd kit && pip install tzdata mcp && python test_kit.py`).
  policy.py (BUY/WAIT, kill switch, audit log, auto-pause), quality_gate.py, voice_router.py, tools.py, mcp_server.py, voice_loop_local.py, jarvus_reporter.py.

## Build order (do one step at a time, tests first, run tests after each)
1. Run the kit tests; read all of `kit/` and the docs. Do not rewrite what works; extend it.
2. Project structure: package, config file, `state/` dir, logging. Keep the kit's tests green.
3. Data service: bars from a real feed (US stocks 1m-1h, crypto 1h), split-adjusted, freshness check, exchange calendar (fill `Config.holidays`/`early_close_dates`). Yahoo is prototype-only.
4. Bot runner: read bots from `data/JARVUS_3_BOTS.xlsx`, strategies from `data/JARVUS_2_STRATEGIES.xlsx`. Signals on closed bars, next-bar fills, costs on, long only. Write the "Fleet feed" fields every cycle (incl. heartbeat, correlation id).
5. Backtest/test harness by ROLE (pooled), then feed trades to `quality_gate.run_gate`. Add PBO, random-direction placebo, buy-and-hold so the gate can open. Count every try.
6. Volatility gate (LOUD/NORMAL/QUIET) as a model with walk-forward validation; then the strategies in "New (to build)".
7. Paper broker simulator + risk engine wiring: `policy.decide` before every paper order; apply `auto_pause_actions` every cycle (also implement HALF_SIZE and PAUSE_NEW_ENTRIES, currently unapplied).
8. Shadow log for unpassed bots; compare to backtest.
9. Reporter on a schedule (daily ~2:15 pm MT) -> `report.json`; Telegram/Twilio to the owner's own number only.
10. Jarvus LLM layer: system prompt = master prompt; tools = `mcp_server.py` (read-only + HALT/PAUSE). Answers in bullets only (BUY/WAIT + hold time).
11. Voice: put speech-to-text/text-to-speech (Pipecat or LiveKit) around `voice_router.route(text, verified)`. Phone number only after the above works.
12. Grow the HUD (`hud/`): add tools one at a time (lead finder, daily briefing, laptop app launcher from a fixed list, better TTS). New tools may read, build or stop things; never move money.

Quick win before step 2: get the HUD running for the owner (`hud/README.md`) so they can see and talk to Jarvus while the rest is built.

## Honest status (do not overstate)
0 of 99 bots have passed the gate; every answer is WAIT. Nothing here has shown profit. BTC study numbers are BTC-only, hourly, and were not re-run.
Ask the owner before: going live, adding any key, changing limits, or installing third-party agents/marketplace add-ons.

## Jarvis Trading Portal (built 2026-10-15)
The new main app. Start it with `START_JARVIS.bat` (Windows), `./start_jarvis.sh` (Mac/Linux) or `python server.py`
(`--demo` = simulated prices labelled DEMO). Owner guide: `README.md`. Inside: `docs/JARVIS_ARCHITECTURE.md`.
Honest status table: `docs/JARVIS_STATUS.md` (keep it updated; use only its status words).

New folders and files
- `engine/` - trading engine, paper only: `data_feed.py` (Coinbase crypto, Yahoo stocks = prototype, demo bars, NYSE calendar),
  `indicators.py`, `strategies.py` (24 kept generators + 7 new), `library.py` (xlsx -> `data/library.json`), `volatility.py`,
  `backtest.py`, `validation.py` (placebo, PBO, trial counter, role test -> `quality_gate`), `paper_broker.py` (paper + shadow
  books), `fleet.py` (74 non-PARKED bots -> `policy.decide` every cycle). `engine/__init__.py` puts `kit/` on `sys.path`.
- `jarvis/` - Jarvis Core: `core.py` (fixed commands via `voice_router` first, then the Claude tool loop or labelled offline
  mode), `brain.py` (strong/fast models, web search), `gateway.py` (typed tools, tiers T0/T1/RR/T2, audit; no order tools),
  `memory.py` (SQLite `state/jarvis.db`), `tasks.py`, `alerts.py`, `phone.py` (Twilio, PIN; off by default), `app.py` (wiring + loops).
- `server.py` - local web server on 127.0.0.1 only; per-run token + Host/Origin checks on every `/api/*` call.
- `web/` - the portal screen (HUD panels, chart, 99-bot matrix, dock pages, browser voice).
- `tests/` - tests for `engine/` and `jarvis/`: `python -m unittest discover -s tests`. No network (demo bars or hand-built
  Bars), temp folders only, never the real `state/`.
- `config/` - `jarvis_config.json` (settings), `jarvis_personality.md` (how Jarvis talks).
- `requirements.txt`, `START_JARVIS.bat`, `start_jarvis.sh`.
- `hud/` is now the **legacy** HUD (still works on its own, same port 8765; do not run both). `kit/` and `hud/` are not modified.

Build order - where things stand
- Done: 1 (kit tests green, 42), 2 (packages, config, `state/`, audit/tool logs), 3 (data service; stock feed is
  prototype-only, calendar typed in but not verified), 4 (bot runner + Fleet feed with heartbeat and correlation id),
  5 (role harness + PBO + placebo + buy-and-hold + trial counter -> `run_gate`), 6 (walk-forward volatility gate, BTC
  out-of-sample AUC 0.724 LOUD / 0.76 QUIET; all 11 "New (to build)" items built), 7 (paper broker, `policy.decide` before
  every paper order, auto-pause each cycle incl. HALF_SIZE and PAUSE_NEW_ENTRIES via `state/flags.json`),
  10 (Jarvis LLM layer: master prompt in the system prompt, gateway tools instead of `mcp_server.py`; tested with a fake
  brain only).
- Partly done: 8 (shadow log yes, shown in the portal for a manual check; no automatic shadow-vs-backtest comparison), 9 (`report.json` on schedule yes; Telegram/Twilio
  push of the report not wired), 11 (browser voice + Twilio phone built; mic and phone never tested for real; no
  Pipecat/LiveKit), 12 (the portal replaces the HUD; the HUD's website builder is not ported; lead finder and app launcher never built).
- Remaining for the owner: API key, Windows try-out, verify NYSE/FOMC dates, earnings calendar, real stock feed (6+ months of
  15m bars), real fee tier, run the Role Tests. Full list in `docs/JARVIS_STATUS.md`.
- Still true: 0 of 99 bots passed the gate; every live answer is WAIT. A role test on real BTC/ETH/SOL (crypto majors: trend)
  gave RESEARCH FURTHER (15 test trades, needs 30+).
