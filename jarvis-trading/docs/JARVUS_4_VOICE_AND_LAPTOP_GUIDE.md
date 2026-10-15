# JARVUS 4 — THE SPEAKING JARVIS (voice, phone, laptop) — version 5

Separate from the prompt, strategies and bots. Written 2026-10-14. Paper trading only.

## What the "viral Jarvis" really is
Every version is the same 5 parts:
- **Ears:** speech-to-text (Whisper, Deepgram, or the browser's built-in speech)
- **Brain:** an AI model (Claude, OpenAI, or a local model)
- **Hands:** tools it can call (usually through MCP, a standard plug-in format for AI tools)
- **Mouth:** text-to-speech (Piper, ElevenLabs, Fish Audio, or your phone company's voice)
- **Trigger:** a wake word ("Jarvis"), a hotkey, or a phone number you call

Real examples I found:
- **isair/jarvis (local, free):** Whisper for ears, Piper for mouth, Ollama models for brain, MCP tools. Windows, Mac and Linux. Everything stays on your machine by default. Needs a decent graphics card for the better models. [GitHub](https://github.com/isair/jarvis)
- **Claude Code "Jarvis" voice:** Chrome speech → a small local server → a running `claude` process → Fish Audio voice. Mac only, needs a Claude subscription and a Fish Audio key. [Listing](https://www.everydev.ai/tools/jarvis-claude-code-voice)
- **"Hermes Jarvis":** a dashboard with voice, wake word and app control. The page I read is a sales page for a paid community with almost no technical detail, so I can't confirm how it works or that it's "viral." [Page](https://aisuccesslabjuliangoldie.com/blog/hermes-voice-agent/)
- **OpenClaw:** chat-app assistant with scheduled briefings. The use-case list I read shows voice as voice *messages* in chat, not phone calls (the phone-call plugin is covered in the earlier guide). [Use cases](https://fast.io/resources/openclaw-use-cases/)

## The safe pattern for a TRADING Jarvis
One builder who made an AI trading agent put it in one line: **"The AI may propose. Policy owns the final authority."** [Source](https://www.indiehackers.com/post/i-built-an-ai-trading-agent-where-the-ai-is-not-allowed-to-trade-9db91d9519). Their setup, and what I copied into this kit:
- The AI sends a **structured request**, never an order.
- A **code risk engine** and an **execution gate** sit between the AI and the broker.
- The AI's tools are **read-only**. Trading tools are left out on purpose.
- One **correlation id** follows each decision from input to result, and everything is saved as evidence.
- Two agents once collided on the same task, so give each job **one owner** (a lock).
- Open question from their comments: if permission is taken away *after* the gate says yes, who re-checks? **My answer in the kit:** the lowest layer re-checks. The HALT flag is read by the code that actually acts, not just by the AI.

## The 5 levels (simplest first)
| Level | What | Cost | Effort | Two-way talk? |
|---|---|---|---|---|
| 1 | Claude app voice mode (tap the sound-wave icon) | Free (counts toward usage limits) | 5 minutes | Yes, but it can't see your logs unless you connect something |
| 2 | `jarvus_reporter.py` calls or texts your own number | about US$2/month + US$1.15/month number | 30 minutes | No (it talks, you listen) |
| 3 | Hosted voice agent (Bland, Vapi, Retell, ElevenLabs) with a phone number | roughly US$0.10–0.23 per minute | A day | Yes |
| 4 | OpenClaw + Voice Call plugin | Twilio + model costs | A weekend, and hardening | Yes (security risks, see old guide) |
| 5 | **Your own build with the kit below** | Depends on parts; local = free | A week | Yes |

Sources for costs and options: [Claude voice mode](https://support.claude.com/en/articles/11101966) · [voice platforms](https://speechify.ai/blog/best-voice-agent-platforms-2026) · [Pipecat](https://github.com/pipecat-ai/pipecat) · [LiveKit](https://livekit.com/blog/build-your-first-ai-voice-agent-python) · [OpenAI Realtime pricing](https://www.forasoft.com/article/openai-realtime-api-pricing)

## The advanced design (Level 5)
```
You (voice / phone / text)
   │
[Ears: speech-to-text]
   │  text
[voice_router.py]  ← fixed list, NO AI: STATUS, REPORT, HALT, PAUSE <bot>. Everything else refused.
   │
   ├─ read-only answers  ← tools.py / mcp_server.py ← report.json, fleet.json (written by the fleet)
   └─ HALT / PAUSE       ← policy.py (HALT file, pause files, audit log)
   │
[Mouth: text-to-speech]

Separate machine, separate job:
Bots → signals → policy.decide() (BUY/WAIT) → paper broker      Gate program → verdicts (Jarvus can't edit)
```
Two ways to add the AI "brain" (for chatty answers and questions):
- **A. Keep the router in front.** The AI only phrases replies. Anything it says that is not a router command does nothing. Safest.
- **B. Let an AI app call `mcp_server.py`** (Claude Desktop, Claude Code, or a voice agent). It gets 5 tools: `get_status`, `get_daily_report`, `get_bot`, `halt_all`, `pause_bot`. No trading tools exist, so a tricked AI can read and stop things, not buy.

## Step by step with the kit
1. Install Python 3.10+. In a terminal: `pip install tzdata mcp`
2. Go to the `jarvus_kit` folder. Run `python test_kit.py`. You should see **42 tests, OK**.
3. Try the pipeline with fake data: `python voice_loop_local.py --report demo/report.json --state-dir state --verified`. Type `status`, `report`, `pause A073`, `halt`, then `buy bitcoin` (it will refuse).
4. Make the daily report from your real paper log: `python jarvus_reporter.py paper_log.csv --json state/report.json`. Schedule it for about 2:15 pm MT (Windows Task Scheduler).
5. Run the read-only tool server: set `JARVUS_DIR`, `JARVUS_REPORT`, `JARVUS_FLEET`, then `python mcp_server.py`.
6. Connect it to an AI app that supports local MCP servers (for Claude Desktop, add it to the app's MCP config; check Anthropic's current docs because menus move). **I did not test whether Claude's voice mode can call a local MCP server. Try it and see.**
7. For real audio, replace `listen()` and `speak()` in `voice_loop_local.py` with speech-to-text and text-to-speech (see Pipecat or LiveKit above). Leave the router and `execute()` alone.
8. For a phone number: let a voice platform (Bland, Vapi, Twilio + Pipecat) pass the caller's words and caller number to `voice_router.route(text, verified)`. `verified` must mean "the number matches yours." Caller ID can be faked, which is exactly why voice can only reduce risk.

## Rules that never change
- **Voice can only cut risk.** HALT and PAUSE yes. Resume, go live, raise limit, trade: never by voice or text.
- **No exchange or broker keys** on the voice machine. Ever.
- **Separate machine** (old laptop or mini-PC), not your trading computer.
- **No marketplace add-ons.** Write your own tiny tools or use the kit.
- **Numbers come from the report file.** If the file is old or missing, the voice says so and does not guess.
- **Calls go to your own number only.**
- **The HALT file is create-only in code.** You delete it by hand, at the keyboard.

## What is tested and what is not
| Part | Status |
|---|---|
| Decision engine (BUY/WAIT), kill switch, pauses, audit log, auto-pause rules | Tested (42 tests) |
| Quality gate math (Deflated Sharpe, 2x costs, folds, sign-flip) | Tested on made-up data. PBO, placebo and buy-and-hold must come from your bar-data tests |
| Voice command router, spoken report, read-only tools | Tested |
| MCP server | Tested: starts, lists exactly 5 tools, answers calls |
| Real microphone, speech-to-text, text-to-speech, wake word | **Not built or tested** |
| Phone number / webhook (Twilio, Bland, Vapi) | **Not built or tested** |
| Calendar data for holidays and early closes | Empty by default. You must fill it from the NYSE calendar |
| Anything about real profit | **Nothing here has made a profit. 0 of 99 bots have passed the gate.** |
