# JARVIS Trading Terminal

A working local research and **paper-trading prototype** inspired by your three videos and the JARVIS, OpenClaw and NOFX repositories. It is an original implementation, not a fork or an installation of those projects. It is not a validated profitable trading system or a finished live brokerage product.

## Run it

Requires Node.js 24 or newer. No npm dependencies or build step are required.

1. Double-click `Start-Jarvis.cmd`, or run `npm start` from this directory.
2. Open **http://127.0.0.1:4317**.
3. The initial account contains **$100,000 simulated USD**, synthetic prices, and paused autopilot.

Try **Brief me**, place a **Simulate buy**, inspect **Council**, or run **Lab → Try synthetic example**. Real-money execution is not implemented, and there is no hidden live-mode switch. Start / stop the paper autopilot explicitly. It pauses after every server restart.

Run `npm test` for the automated checks. Stop a foreground server with Ctrl+C. This build does not install a Windows service or guarantee operation while your computer sleeps.

## What works and what needs configuration

| Capability | Current state |
| --- | --- |
| Jarvis dashboard, charts, watchlist, portfolio, journal | Working locally; 14 initial USD assets, not the entire market |
| Manual paper orders and rule-based autopilot | Working; long-only, local simulated market fills |
| Browser voice commands and spoken replies | Implemented using browser speech APIs; microphone and browser support required; audio not end-to-end tested here |
| 25-role analyst council | 25 deterministic checks work immediately; each role can run as a separate local-model request after Ollama setup |
| Open-ended AI chat | Implemented; requires a downloaded local Ollama model |
| Incoming and outgoing telephone conversations | Implemented through Twilio; requires account, number, PIN and HTTPS webhook; no real call was placed during development |
| Proactive owner calls | Opt-in risk/price alerts, quiet hours, daily attempt limit, five-minute spacing; disabled initially |
| External crypto quotes | Coinbase public ticker adapter; a BTC-USD request returned a valid recent quote during verification |
| US stock quotes | Optional Alpaca IEX adapter; credentials and eligible data access required; not tested with credentials |
| Meme coins | DOGE, SHIB, PEPE and BONK in the paper watchlist; exact-contract DEX research; no on-chain trading |
| Backtesting | Strict OHLCV CSV import, chronological evaluation, next-open fills, fees/slippage, stops, benchmark and drawdown |
| OpenClaw integration | Authenticated read-only context endpoint; OpenClaw itself is not installed |
| Broker execution, wallets, CAD ledger, TSX universe | Not implemented |
| News, social sentiment, earnings calendar, book depth, holder/contract checks | Not connected; explicitly marked missing |
| General desktop control / website building | Not implemented; external content cannot execute operating-system commands |

The 25 roles are conductor, trend, momentum, mean reversion, breakout, volatility, volume, liquidity, spread, correlation, portfolio, sizing, stops, drawdown, execution, equities, crypto, meme coins, on-chain, news, sentiment, calendar, validation, audit and security. More model calls do not establish better investment decisions. The conductor makes one request; a full council uses 25 with concurrency limited to two. No models run automatically in the background.

## Data and execution boundaries

All balances are **USD**, regardless of the operator's Canadian location. Demo prices are generated examples, not current market prices. External data must be selected deliberately in **Connect**. Switching feeds requires no open positions, resets price history and pauses autopilot. Unavailable data does not silently fall back to synthetic prices.

Coinbase is polled every 30 seconds. The source timestamp is the latest trade timestamp; an old trade can conservatively block a fill even if the quote itself changed. The stock adapter uses IEX, which is a partial-market feed. External charts accumulate sampled observations from connection time; their bars and zero volume are **not** exchange historical OHLCV. Do not use them as historical training data. The autopilot waits for 26 completed observed minute bars.

The order engine checks quote freshness (90 seconds), finite positive prices, cash including fees, order size, position concentration, total exposure, combined meme exposure, stop-risk budgets and daily loss. It forbids short sales and leverage. Duplicate client order IDs cannot spend funds twice. Quotes for existing holdings must also be fresh before additional entry. Orders and source timestamps persist in SQLite.

The daily loss baseline rolls at **00:00 UTC**. Initial defaults: $2,500 maximum order, 15% position concentration, 60% gross exposure, 2% combined meme exposure, 2% daily loss cap, 0.5% estimated stop risk per position, and five times that budget for aggregate stop risk. Stops default to 2%, targets to 4%; modeled fees are 10 bps and extra slippage 5 bps per side. The observed bid/ask spread is charged as well. These are engineering defaults, not personalized recommendations.

**Emergency stop blocks entries; it does not liquidate positions.** Local stop/target monitoring continues only while the server runs and fresh prices are available. Stops can fill worse after gaps and cannot guarantee a maximum loss. Exit orders remain available during a halt. There is no brokerage reconciliation because no brokerage orders are sent.

## Configure local AI

1. Install [Ollama](https://docs.ollama.com/quickstart) and download a model that fits your computer using its official instructions.
2. Copy `.env.example` to `.env` in this directory.
3. Set `OLLAMA_MODEL` to the exact downloaded model name. `OLLAMA_URL` defaults to `http://127.0.0.1:11434`; this build accepts loopback hosts only.
4. Restart Jarvis and enable **Local AI** in the assistant, or use the council controls.

The model receives the selected question and bounded paper-account/council context. It receives no API credentials and has no tools, shell access, order endpoint or phone-call authority. Its text can still be wrong. App instructions distinguish source evidence from user commands; external content is treated as data. The rule-based autopilot remains separate from model commentary.

## Set up phone calls in both directions

Use a Twilio Voice account and a voice-capable phone number. Enter these values **locally** in `.env`, never in chat or source control:

```dotenv
TWILIO_ACCOUNT_SID=your_account_sid
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_FROM_NUMBER=your_twilio_number_in_E164_format
OWNER_PHONE_NUMBER=your_number_in_E164_format
PHONE_PIN=your_private_6_to_12_digit_pin
PUBLIC_VOICE_URL=https://your-voice-host.example
```

Use an HTTPS reverse proxy that routes **only `/voice/*`** to the local service. Keep `/api/*`, `/api/session`, the dashboard and `/bridge/*` off the public internet. The public origin must be exact, without a trailing slash or path. Use normal HTTPS port 443. The app listens on loopback and is not a multi-user remote application.

In Twilio, configure the number's incoming voice webhook as **POST `https://your-voice-host.example/voice/incoming`**. Restart Jarvis. Call the number, enter your PIN followed by `#`, and ask for a briefing. **Call me** requests an outgoing call to the configured owner number, then requires the same PIN before revealing account details. The app never accepts an arbitrary destination number from a browser request.

Callbacks require a valid Twilio HMAC signature, matching account SID, matching owner and service numbers, and a per-call PIN authorization. Three failed PIN attempts lock that call; sessions expire after five minutes. No call audio is recorded by this app. The phone provider processes speech for its transcription service. Basic phone commands work without AI; open-ended phone replies use Ollama if configured. Long model responses use TwiML pauses/redirects to avoid holding one webhook open.

For proactive calls, first test a manual call. Then use **Risk → Proactive Jarvis calls**, choose your actual timezone, quiet hours and maximum attempts. Turning on a trigger authorizes chargeable calls to your owner number. Calls are disabled by default, limited to five attempts per day at most, spaced by at least five minutes, and never automatically retried after failure. Quiet-hour and throttled events are skipped, not queued. Alerts do not wake a sleeping computer. Calls begin with a PIN prompt; account information is not left in voicemail.

Real telephony and model inference require end-to-end verification after your setup. Unit and local HTTP tests do not establish that a carrier, microphone, model, proxy or paid account works.

## Strategy evaluation

CSV header:

```csv
time,open,high,low,close,volume
2026-01-05T14:30:00Z,100,101,99,100.5,10000
```

Supply 60–50,000 ascending rows and timestamps with an explicit timezone, or epoch milliseconds. The importer rejects empty fields, duplicate/out-of-order timestamps, nonfinite values, future dates, negative volume and impossible OHLC ranges. It reports large time gaps but cannot distinguish market closures from missing observations without an exchange calendar.

The fixed SMA/RSI rule is evaluated on the final 30%; earlier data provides indicator warmup. No parameter optimization occurs, so this is a chronological evaluation, not a walk-forward optimizer. Signals see only prior completed bars; entries/exits use the next open with adverse slippage and fees. Position size is 10% of available cash. Stops assume the worse outcome when stop and target both fall within the same bar; stop gaps use the open. Remaining positions close at evaluation end. The benchmark is all-capital buy-and-hold after the same modeled costs.

This lab is intentionally separate from the portfolio engine: it does not reproduce every portfolio exposure limit or the autopilot's cross-asset selection. It omits market impact, order queue position, partial fills, taxes, CAD/USD FX, dividends, borrow costs and authoritative exchange sessions. Trade counts can be too small for inference. Historical source accuracy, corporate-action adjustments and survivorship bias require independent validation. Synthetic results demonstrate software behavior only.

## Canadian deployment considerations

Canada is known; the province, account type, broker and permissions are not. A data API is not proof that a resident may trade the same products.

[IBKR's documented TWS API limitations](https://www.interactivebrokers.com/docs/tws-api/doc/notes-limitations/tws-api-limitations) state that IBC does not permit users' own applications to submit API orders for Canadian-exchange/marketplace products. Do not assume its general API offering covers those products. Any proposed non-Canadian-market route needs broker confirmation for the specific account and product.

Use the [CSA authorized crypto-platform list](https://www.securities-administrators.ca/crypto-platforms-regulation-and-enforcement-actions/crypto-platforms-authorized-to-do-business-with-canadians/) and the platform's province/product terms when choosing a crypto execution provider. NOFX's exchange list and tokenized stock perpetuals are not a substitute for Canadian eligibility or ownership of listed shares.

## Research and improvements

Your silent videos establish visual and interaction references: multi-panel trading displays, a glowing central assistant, and telephone briefings. Their captions and performance claims are not verified investment evidence.

- [Gladiator07/JARVIS](https://github.com/Gladiator07/JARVIS): useful reference for voice commands and an assistant interface; its README describes a Python desktop assistant, not a trading engine.
- [OpenClaw](https://github.com/openclaw/openclaw): useful reference for extensible personal-agent channels. Its [security guidance](https://docs.openclaw.ai/gateway/security/prompt-injection) motivates separating external content, model output and executable authority. A separately configured OpenClaw instance can read `GET /bridge/context` with `Authorization: Bearer <BRIDGE_TOKEN>`; use a random token of at least 32 characters. There is no write bridge.
- [NOFX](https://github.com/NoFxAiOS/nofx): useful reference for strategy/execution separation, risk checks and trade attribution. Its README identifies AGPL-3.0 and derivatives-oriented markets; no code was copied or bundled here.
- [Coinbase ticker API](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker), [Alpaca market data](https://docs.alpaca.markets/us/docs/historical-api), [DEX Screener API](https://docs.dexscreener.com/api/reference), [Ollama chat API](https://docs.ollama.com/api/chat), and [Twilio security](https://www.twilio.com/docs/usage/security) / [speech Gather](https://www.twilio.com/docs/voice/twiml/gather) informed the adapters.

Highest-value next improvements, in order:

1. **A permitted broker connection:** establish province, product universe and broker permissions; implement a paper-account adapter with order acknowledgments, partial fills, cancels, reconnect recovery and continuous account reconciliation. No direct transition from this local ledger to live money.
2. **Trustworthy historical data:** exchange sessions, corporate actions, delisted symbols, symbol mapping, quote/depth feeds and durable timestamped ingestion. Add licensed news/calendar sources with provenance and freshness checks.
3. **Strategy evidence:** compare several simple baselines across distinct market regimes; perform walk-forward evaluation, cost stress tests and multiple-testing controls; collect a substantial forward paper record. Promote only against predeclared evaluation criteria. Do not train on the synthetic examples.
4. **Meme-coin controls:** exact chain/contract identity, on-chain authorities, holder concentration, LP provenance, sell simulations and transaction simulation. Fail closed when a critical check is unknown; paper experimentation does not establish sellability.
5. **Operational reliability:** durable event/order tables, decimal accounting, external watchdog, backups, market-calendar-aware freshness, broker-side protective orders, separate services and security review. The current ledger is one SQLite snapshot, uses JavaScript floating-point arithmetic, and is intended for local prototyping.
6. **Voice quality:** end-to-end microphone/carrier tests, faster interruption-capable audio streaming, tested caller authentication, call delivery statuses and accessible hands-free controls. Keep spoken trade intent as a reviewable proposal.
7. **Cost-aware agents:** route only relevant evidence to specialist models, cache unchanged analyses, measure useful disagreement, version prompts/strategies and use deterministic validation before any future order proposal. Twenty-five simultaneous voices alone do not create an edge.

## Verification and files

Development verification: **31 automated checks passed**. Covered order accounting, partial exits, replay protection, invalid input, stale data, emergency and daily-loss gates, stop gaps, CSV quality, temporal leakage, transaction costs, all 25 roles, published Twilio signature vector, PIN lockout, owner restrictions, local origin/session controls, notification limits and SQLite restart persistence. Browser checks covered dashboard rendering, briefing, council and strategy evaluation. Public BTC ticker retrieval succeeded. No live orders, on-chain transactions, real calls or AI inference were performed.

`server.mjs` owns HTTP and lifecycle. `lib/core.mjs` owns the ledger and limits; `lib/backtest.mjs` the CSV evaluator; `lib/agents.mjs` the role catalog and AI adapter; `lib/providers.mjs` market data and voice; `lib/store.mjs` SQLite persistence; `public/` the interface; `test/` the test suite.

Local state is in `data/jarvis.sqlite`. Preserve it to retain the journal; back it up while the server is stopped. `.env` and `data/` are ignored by Git. The distributable ZIP excludes credentials and runtime state. The local API trusts processes running as the same operating-system user; do not expose it publicly. There are no profit, completeness or “best in the world” guarantees.
