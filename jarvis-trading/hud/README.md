# Jarvis HUD - your own voice Jarvis (like the video), for trading and more

## Start it (about 5 minutes)
1. Install Python 3.10+ (python.org). On Windows, tick "Add to PATH".
2. Get an Anthropic API key at console.anthropic.com (pay-as-you-go; the key is the brain).
3. Open a terminal in this `hud` folder and run:
```
pip install anthropic tzdata openpyxl psutil
set ANTHROPIC_API_KEY=sk-ant-...          (Mac/Linux: export ANTHROPIC_API_KEY=sk-ant-...)
python server.py
```
4. Your browser opens the HUD. Use **Chrome or Edge** (they have free built-in speech).
5. Click the mic (or press Space) and talk. Or turn on **WAKE WORD** and say "Jarvus, ...".

Want demo numbers on screen? Copy `../kit/demo/report.json` into a new folder `hud/state/` (fake data, for testing).

No key? It still runs: "status", "report", "halt", "pause A073" work. Chat is off.

## What's on the screen (v2)
- Boot sequence, glitching logo, animated particle background that follows your mouse
- Orb with a voice ring that reacts to your mic while you talk and moves while Jarvus speaks
- Live paper P&L (counts up), trades, paused bots, report age, and a cumulative P&L chart (from `state/paper_log.csv`)
- **Bot matrix:** all 99 bots as colored squares (hover for name), plus the quality-gate bar (0 / 99 passed)
- Activity log, list of websites Jarvus built, NYSE / New York / weekend-crypto clock
- Quick buttons, typed-out answers, sound effects (SFX button), keys: Space talk, W wake word, M mute, H halt
- **HALT turns the whole screen red** with a flashing banner until you clear the HALT file by hand

## Things to say
- "Status" / "How did today go?" (paper-trading numbers, read from the report file)
- "Halt everything" / "Pause A073" (fixed code, no AI involved)
- "Build a website for a barber shop in Edmonton" (Claude writes it, it opens in a new tab, saved in `sites/`)
- Any question: ideas, emails, explanations

## How it works
- Ears: the browser's speech recognition. Mouth: the browser's voice. Both free.
- Every sentence goes to `server.py` first. HALT/PAUSE/STATUS/REPORT are handled by fixed code.
- Everything else goes to Claude, with the master prompt and 4 tools: get_status, get_daily_report, get_bot, build_website.
- There are NO trading tools. Paper only. The server only listens on your own computer.

## Add "a lot more" (ask Claude Code to do these)
Each new ability = one tool in `TOOLS` + one line in `run_tool()` in `server.py`.
- Lead finder for ElevatedSteps (businesses with no website)
- Daily briefing (calendar, email) through a connector
- Open apps / files on your laptop (keep it to a fixed list; never "run any command")
- Better voice: ElevenLabs or another text-to-speech instead of the browser voice
- Phone access: Twilio or Bland in front of the same `/api/ask`

Rule: new tools may READ, BUILD or STOP things. Never give it a tool that moves money.

## Tests
`python test_hud.py` (8 tests; uses a fake Claude, no key needed)


## v3: hands-free usage
- Open the page once and click anywhere (or press any key) on the "ACTIVATE VOICE" overlay. Browsers need that one click before the mic works. Use Chrome or Edge.
- After that, just talk. Say "Jarvis, status" (wake word is ON by default). After he answers you have 10 seconds to follow up without saying "Jarvis" again.
- Press `?` for the help overlay. The SYSTEM panel shows FPS, ping, uptime, model, ears, voice.
- On HALT the whole screen turns red. Only a person at the keyboard can restart (delete the HALT file).
- Tests: `python test_hud.py` (8 tests). Live mic and API key were not tested by me.


## v4: reactor-style layout
- Left: system health, CPU, memory (real numbers; needs `pip install psutil`), paper portfolio, agents + 99-bot matrix.
- Centre: reactor core that reacts to your voice, GATE/WATCH/UPTIME/FLEET boxes, voice box with live waveform.
- Right: live crypto prices + BTC 15-minute chart (public Coinbase feed, read by your browser; shows NO FEED if blocked, never invented), risk monitor (real limits from `kit/policy.py`).
- `L` opens the log drawer (activity, websites built, system details). Everything stays blue, including HALT.
- System health % = share of checks passing (server up, report fresh, kill switch not tripped, fleet loaded).

## v5: effects and sound
- Power-up: panels slide in, reactor spins up with a flash and shockwave, rising sweep + chime + low drone (SFX button turns all sound off).
- Reactor reacts: shockwaves on listen/think/speak, sparks and lightning while talking, lens-flare streak, mouse parallax.
- Extras: scramble-text title, hologram flicker, panel light sweeps, shiny bars, click ripples, hover blips, typing ticks, random screen glitch, HALT = screen shake + flash + alarm.

## v6: new core
- Stick-man hologram removed.
- Core redrawn at higher resolution (1400px canvas) with bloom glow: bevelled 12-panel outer ring, 8-panel counter-rotating inner ring, notched ring, spinning hex iris, light rays, degree scale, energy flowing around the ring, glass highlight.
