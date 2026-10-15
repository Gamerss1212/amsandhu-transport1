# Jarvis Trading Portal

Your own "Jarvis" on your own computer: a reactor-style control screen, a Jarvis you can talk to or type to,
a fleet of 99 trading bots that **paper trade** (pretend money), and every strategy from your spreadsheet,
ready to backtest.

> **PAPER TRADING ONLY.** There is no broker key, no real money, and no code that can place a real order.
> The code decides BUY or WAIT. Jarvis (the AI) explains, researches and reminds. It can only **cut** risk (HALT / PAUSE).

---

## Read this first: honest status

- **0 of 99 bots have passed the quality gate.** So every live answer is **WAIT** until a bot group ("role") passes the Role Test.
- **Nothing here has shown a profit.** Backtest numbers are research, not a promise.
- A Role Test was run on real BTC, ETH and SOL data during the build: verdict **RESEARCH FURTHER**
  (15 test trades, the gate needs 30+). That is a normal, honest answer.
- The AI brain has only been tested with fake answers (no API key was available during the build).
- The Windows launcher and the browser microphone have not been tried on a real Windows PC yet.
- Full table: [`docs/JARVIS_STATUS.md`](docs/JARVIS_STATUS.md). How it works inside: [`docs/JARVIS_ARCHITECTURE.md`](docs/JARVIS_ARCHITECTURE.md).

---

## What you get

- **The portal** - one screen with your paper account, live prices, the volatility gate, the risk engine,
  a price chart with strategy signals, the 99-bot matrix and the BUY/WAIT "targeting" panel.
- **Jarvis** - talk (Chrome or Edge) or type. One brain for the screen and (optionally) the phone.
- **The fleet** - 99 bots from `data/JARVUS_3_BOTS.xlsx`. 74 are checked about every 2 minutes; 25 are PARKED (switched off in the sheet).
- **The strategy library** - 100 kept strategy variants + 11 new ones from `data/JARVUS_2_STRATEGIES.xlsx`,
  all runnable in the Backtest Lab with costs, a placebo test, an overfit test (PBO) and the quality gate.

---

## 5-minute start on Windows 11

1. **Install Python** (the language Jarvis is written in).
   - Go to <https://www.python.org/downloads/> and click **Download Python**.
   - Run the installer. On the **first screen, tick "Add python.exe to PATH"**. Then click **Install Now**.
2. **Put this folder somewhere easy**, for example `Documents\jarvis-trading`.
3. **Double-click `START_JARVIS.bat`** in that folder.
   - If Windows says "Windows protected your PC", click **More info**, then **Run anyway** (it is your own file).
   - The first run installs a few add-ons (about a minute). After that it starts straight away.
4. Your browser opens **http://127.0.0.1:8765** (127.0.0.1 means "this computer"). Use **Chrome or Edge**.
5. **Click once anywhere** on the page (browsers need one click before they allow sound and the mic).
6. Say **"Jarvis, ..."** (the wake word), or press **Space** and talk, or type in the box at the bottom and press Enter.
   After Jarvis answers you have about 12 seconds to follow up without saying "Jarvis" again.

To stop Jarvis: click the black window and press **Ctrl+C**, or just close that window.

**Prefer typing commands?** Open the folder in File Explorer, click the address bar, type `cmd`, press Enter, then:

```
python -m pip install -r requirements.txt
python server.py
```

**Mac or Linux:** open a terminal in the folder and run `./start_jarvis.sh`.

---

## Add your API key (optional, but needed for full conversation)

The **API key** is like a password that lets Jarvis use Claude, the AI. It is pay-as-you-go: each question costs a little.

1. Go to <https://console.anthropic.com>, sign in, open **API Keys**, click **Create Key**, and copy it (it starts with `sk-ant-`).
2. Open **Command Prompt** (Start menu, type `cmd`) and run (paste your own key):
   ```
   setx ANTHROPIC_API_KEY "sk-ant-your-key-here"
   ```
3. **Close every Command Prompt / black window**, then start Jarvis again. (`setx` only reaches new windows.)
4. Check: the black window prints `brain: ONLINE ...` and the BRAIN badge at the top of the screen shows online.

Just for one window instead: `set ANTHROPIC_API_KEY=sk-ant-...` then `python server.py` in that same window.
Mac/Linux: `export ANTHROPIC_API_KEY=sk-ant-...`

Keep the key secret: never type it into the Jarvis chat, a config file, or code. Jarvis never asks for it.

**No key? Jarvis still runs, in a clearly labelled OFFLINE mode.** Offline it can do: status, report, halt, pause a bot,
prices, the paper portfolio, the bots, the volatility gate, the risk engine, the time, and the code's BUY/WAIT answer for a symbol.
It cannot do general questions, web research, or set reminders/backtests by voice (use the **Alerts** and **Backtest Lab** pages instead).

---

## Demo mode (fake prices for trying things out)

```
START_JARVIS.bat --demo          (from a Command Prompt in this folder)
python server.py --demo
```

- Prices are **simulated** and labelled **DEMO** everywhere (screen badge, answers, chart). They are never real.
- No internet is needed for prices. (The AI brain still needs internet if you added a key.)
- You can also set `"data_mode": "demo"` in `config/jarvis_config.json`.

---

## What you can say to Jarvis

| You say | What happens | Works without a key? |
|---|---|---|
| "status" | Fixed code reads the report file: paper P&L, paused bots, HALT flag | Yes |
| "how did today go?" or "report" | Fixed code reads the daily paper report | Yes |
| "halt everything" | Creates the HALT flag. Nothing trades until you delete it by hand | Yes |
| "pause A073" | Creates a pause file for bot A073 | Yes (say exactly "pause A073" or "pause bot A073") |
| "what are my agents doing?" | Fleet summary: bots by status, signals, BUY answers, gate results | Yes (short version) |
| "should I buy BTC?" | The **code's** BUY/WAIT answer and reason, in bullets | Yes |
| "run a backtest of rsi2 on ETH" | Starts a background backtest; the result shows in the Backtest Lab and as a notification | Needs key (or use Backtest Lab) |
| "remind me at 3pm to call the bank" | Creates a reminder; Jarvis reads it back so you can check it | Needs key (or use Alerts) |
| "what's the news on Nvidia today?" | Web search; Jarvis says where it came from | Needs key |
| "explain what ATR means" (any question) | Normal assistant answer | Needs key |
| "remember I like short answers" | Saved to Memory (you can see, fix or delete it on the Memory page) | Needs key (or use Memory) |

"halt", "stop everything", "emergency stop", "pause A073", "status" and "report" are matched by **fixed code, no AI**
(the kit's `voice_router.py`), so nobody can talk Jarvis into anything with clever wording.

Jarvis will **never**: place, change or cancel an order; un-pause a bot; clear HALT; change limits; or loosen the quality gate.
No such tool exists.

---

## The portal screen

**Header (top bar)** - market status (NYSE open/closed, crypto), the **data source badge** (LIVE or DEMO),
the AI brain badge (online/offline), the clock in Mountain Time, and a price ticker.

**Left column**
- **Paper Portfolio** - paper equity, today's paper P&L, realised/unrealised, open positions, shadow trades.
- **Market Watch** - your watchlist prices (crypto from Coinbase, stocks from Yahoo).
- **Volatility Gate** - LOUD / NORMAL / QUIET for the next 12 hours. It predicts **how much** price may move, never which way.
- **Risk Engine** - the kill switch (HALT) state, hard limits, and how much of each limit is used.

**Center**
- **JARVIS reactor** - the glowing orb. Click it or press Space to talk. Around it: bots active, roles passed, uptime, signals.
- **System status voice box** - what Jarvis is doing and saying, plus sources when it searched the web.
- **Price chart** - 5m / 15m / 1h bars, EMA 20 (a trend line), VWAP (average price weighted by volume),
  and **strategy signal markers**: pick a strategy in the dropdown to see where it would have signalled.

**Right column**
- **Strategy Systems** - the strategy families in your library.
- **Fleet Diagnostics** - the 99-bot matrix: one square per bot, coloured by status (white outline = signal now, red ring = paused).
- **Targeting System** - the BUY/WAIT bullets for the symbol in focus, with the reason.

**Bottom dock (pages)** - Strategy Library, Backtest Lab, Fleet, Positions & Journal, Alerts & Reminders, Memory,
Activity & Audit, Help. Plus the ask bar (mic, type box, SEND) and the WAKE, VOICE and **HALT** buttons.

**Keyboard** - **Space** talk / interrupt, **M** mute the microphone, **V** voice output on/off, **W** wake word on/off,
**H** HALT (asks you to confirm first), **1-8** open the dock pages, **Esc** close a page, **?** help.

When HALT is on, a red banner says so on every page until you clear it by hand.

---

## Safety model (how you are protected)

- **Paper only.** No broker or exchange keys, no order function. The "broker" is a simulator in `engine/paper_broker.py`.
- **Code decides.** Every BUY comes from `kit/policy.py` (`policy.decide`, 10 checks: HALT, market hours, fresh data,
  gate passed, volatility, events, stop width, cost, signal, risk limits). The AI cannot override it.
- **A bot only paper-trades if its whole role passed the quality gate.** Others go to the **shadow log** (signals tracked with no money).
- **Every trade has a hard stop.** Fills happen at the next bar's open, never at the signal bar. A fill more than 2% off is refused.
- **Fail closed.** Missing data, an error, or stale data (last closed bar too old) = WAIT. Prices are never made up.
- **HALT is create-only.** The HALT button, "halt everything", or an automatic rule (fleet down 3% in a day or 6% in a week)
  creates the file `state\HALT`. Open paper positions are closed on the next cycle.
  **To clear it:** open the `state` folder in File Explorer and delete the file named `HALT`. No code can delete it.
- **Pausing a bot** creates `state\paused\A073` (one file per bot). **To un-pause:** delete that file. Jarvis cannot.
- **Automatic risk cuts every cycle:** bot errors, stale data, 3 losses in a row or cost over 0.33R pause a bot;
  cost over 0.20R halves its size; too much crypto/meme risk pauses new entries.
  (Bots that cannot trade yet only show "would pause".)
- **Audit trail.** BUY/WAIT decisions on new signals, voice/typed commands and automatic risk actions go to
  `state/audit.jsonl` (a hash chain: tampering shows up).
  Every Jarvis tool call is logged; see the **Activity & Audit** page.
- **The phone can only read and cut risk**, and only after your PIN.

---

## Phone (optional, not tested with a real phone company)

The phone part is built and tested with fakes only. It has **never** made or answered a real call. It is OFF until you set it up.
It needs a Twilio account (a phone service; costs money).

1. In Twilio, get a phone number. Then set these in Command Prompt, one line at a time, and open a new window after.
   `TWILIO_FROM_NUMBER` is your Twilio number. `JARVUS_MY_NUMBER` is **your own cell** - the only number Jarvis will ever call.
   ```
   setx TWILIO_ACCOUNT_SID "AC..."
   setx TWILIO_AUTH_TOKEN "your-twilio-auth-token"
   setx TWILIO_FROM_NUMBER "+1780..."
   setx JARVUS_MY_NUMBER "+1780..."
   ```
2. Set a phone PIN (4-10 digits). Only a salted hash is saved (`state/phone_auth.json`), never the PIN:
   ```
   python server.py --set-pin
   ```
3. **Jarvis calling you:** make an alert with the **phone** channel. Jarvis calls only `JARVUS_MY_NUMBER` and says only
   "You have a new notification. Please call back and enter your PIN" (voicemail can be heard by anyone).
   Limits: 2 calls an hour, 6 a day, no calls in quiet hours (10 pm - 7 am MT, you get a desktop note instead).
   Turn all calls off with `"calls_off": true` in the config.
4. **You calling Jarvis (inbound):** the portal only listens on your own PC, so this also needs a public web address
   that forwards to it (a "tunnel"). Ask before setting one up. Then:
   ```
   setx JARVIS_PHONE_INBOUND 1
   setx JARVIS_PUBLIC_URL "https://your-public-address"
   ```
   In Twilio, set the number's "A call comes in" webhook to `https://your-public-address/phone/voice` (HTTP POST).
   Call, enter your PIN and #, then speak. After the PIN you get read-only answers plus HALT and PAUSE. Nothing else.
   3 wrong PINs hang up; 5 wrong in 15 minutes locks the PIN for 15 minutes.

---

## Settings: `config/jarvis_config.json`

Edit with Notepad, save, then restart Jarvis.

| Field | What it does | Default |
|---|---|---|
| `owner_name`, `timezone` | Labels. (Not read by the code yet; Mountain Time is built in.) | Abhi, America/Edmonton |
| `paper_capital_usd` | Starting paper money. Used only when the paper account is first created | 10000 |
| `data_mode` | `live` (real prices) or `demo` (simulated) | live |
| `crypto_venue` | Which fee model for crypto: `ndax`, `kraken_taker`, `kraken_maker`, `maker_10bp`. Prices still come from Coinbase | ndax |
| `watchlist` | Symbols on Market Watch | BTC, ETH, SOL, DOGE, AAPL, NVDA, TSLA, JPM |
| `fleet_cycle_seconds` | How often the 74 bots are checked | 120 |
| `alerts_every_seconds` | How often alert rules are checked | 30 |
| `daily_report_time_mt` | Weekday daily report time (writes `state/report.json`, shows a notification) | 14:15 |
| `models.strong`, `models.fast` | AI models: strong for thinking/research, fast for quick replies | claude-opus-5-5, claude-sonnet-5-5 |
| `effort.strong`, `effort.fast` | How hard the AI thinks (more = slower and costs more) | medium, low |
| `internet_research` | Let Jarvis search and read the web | true |
| `quiet_hours_mt` | No phone calls in this window | 22:00 - 07:00 |
| `retention_days` | Chat history and tool logs older than this are deleted at start | 30 |
| `phone` | `max_calls_per_hour`, `max_calls_per_day`, `calls_off` | 2, 6, false |
| `port` | Web address port: http://127.0.0.1:PORT | 8765 |

**Switches you can add when starting:** `--demo` (fake prices), `--no-browser` (don't open the browser), `--set-pin` (phone PIN).

**Environment variables** (set with `setx NAME "value"`, then open a new window): `ANTHROPIC_API_KEY`, `JARVIS_DATA_MODE`
(live/demo), `JARVIS_PORT`, `JARVIS_MODEL_STRONG`, `JARVIS_MODEL_FAST`, `JARVUS_DIR` (use a different state folder),
and the phone ones above.

---

## Running the tests

From the `jarvis-trading` folder (tests use demo data and temporary folders; no internet):

```
python -m unittest discover -s tests
```

The owner's original kit (expect `Ran 42 tests ... OK`; the MCP test is skipped if `mcp` is not installed):

```
cd kit
python test_kit.py
```

---

## Folder map

```
jarvis-trading/
  START_JARVIS.bat     double-click this on Windows
  start_jarvis.sh      the same for Mac/Linux
  server.py            the portal web server (this is what runs)
  requirements.txt     the Python add-ons
  config/              jarvis_config.json (settings), jarvis_personality.md (how Jarvis talks)
  engine/              trading engine: prices, strategies, backtests, quality-gate evidence, paper broker, fleet
  jarvis/              Jarvis Core: AI brain, tools, memory, alerts, background tasks, phone
  web/                 the portal screen
  tests/               tests for engine/ and jarvis/
  kit/                 your original tested code (BUY/WAIT rules, gate, voice commands) - left unchanged
  hud/                 the old HUD (legacy; still works on its own, uses the same port 8765)
  data/                your strategy and bot spreadsheets + library.json (a saved copy)
  docs/                JARVUS_1, 4, 5 (rulebook, voice guide, risk checklist), JARVIS_ARCHITECTURE.md, JARVIS_STATUS.md
  state/               made on first run - YOUR data:
                         HALT, paused/          kill switch and paused bots (delete by hand to clear)
                         paper_book.json, paper_log.csv, shadow_book.json, shadow_log.csv
                         fleet.json, report.json, audit.jsonl, flags.json, gate_results.json, trials.json
                         jarvis.db (chats, memories, alerts, tasks, tool log), bars/ (price cache)
```

---

## Troubleshooting

- **"python is not recognized"** - Python is missing or not on PATH. Re-run the Python installer, choose **Modify**,
  and tick "Add Python to environment variables". Or install again with "Add python.exe to PATH" ticked.
- **The browser did not open** - open <http://127.0.0.1:8765> yourself. Other addresses are blocked on purpose.
- **The page shows "forbidden" or stops updating** - the page is from an old run (a new secret code is made every start).
  Press **F5** to reload.
- **The mic does nothing** - use **Chrome or Edge** (Firefox has no built-in speech). Click once on the page first.
  Allow the microphone when the browser asks (or click the mic icon in the address bar). In Windows:
  Settings > Privacy & security > Microphone > let apps and your browser use it.
- **"WinError 10048" / "address already in use"** - Jarvis or the old HUD is already running. Close the other window,
  or change `port` in the config.
- **"No module named ..." or "ZoneInfoNotFoundError"** - run `python -m pip install -r requirements.txt`.
- **Black window says `brain: OFFLINE (SETUP_REQUIRED...)`** - no API key in this window. Use `setx`, then open a new window.
- **"My AI brain is unavailable right now (...)"** - AUTH = key rejected; RATE_LIMIT = too many requests, wait;
  NETWORK = no internet; MODEL = that model is not on your account. Jarvis falls back to offline answers meanwhile.
- **Everything says WAIT** - expected. No role has passed the gate. Also: stocks only enter 9:35 am - 2:30 pm ET on 1h bars
  (3:30 pm on shorter bars); crypto never enters on Sat/Sun or 7 pm - midnight MT; stock bots also WAIT until an
  earnings calendar is added.
- **"stale" next to a price** - the data is too old, so the answer is WAIT. Check your internet. Stock prices come from
  Yahoo, which is **prototype-only** (unofficial, can break or limit you). Crypto comes from Coinbase.
- **The HALT banner will not go away** - by design. Delete `state\HALT` by hand.
- **Start the paper account over** - stop Jarvis, then delete `state\paper_book.json` and `state\paper_log.csv`.
