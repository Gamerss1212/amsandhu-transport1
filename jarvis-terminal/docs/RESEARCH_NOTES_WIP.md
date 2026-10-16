# Research notes (work in progress, not yet acted on)

## research

I surveyed 2024-2026 LLM and multi-agent trading systems: TradingAgents v0.6 (Apache-2.0), virattt/ai-hedge-fund (MIT), NOFX (AGPL-3.0, so ideas only), FinRobot V2 (Apache-2.0), FinMem (MIT), FinAgent, FinCon, Alpha-GPT, RD-Agent(Q) (MIT), AlphaAgent and OpenBB agents (Apache-2.0/MIT). I also covered the 2025-2026 evaluation literature: Profit Mirage, FINSABER, StockBench, DeepFund, LiveTradeBench, Agent Market Arena, Alpha Arena, Kong et al. (2026) and The Alpha Illusion. Sources were READMEs and changelogs fetched with curl, arXiv abstracts and HTML, official Claude pricing and scheduled-deployment docs, and the OpenClaw scheduler docs.

**Main conclusion.** No study finds that an LLM placing trades end to end beats buy-and-hold once training-data leakage, survivorship bias, costs and sample size are controlled.
- Profit Mirage: returns fell 50-72% when tested after the model's training cutoff.
- FINSABER (20 years, 100+ symbols): the earlier LLM advantages disappear.
- StockBench (2025): the best model made +2.5% against +0.4% for buy-and-hold over 82 days. That gap is within noise.
- DeepFund and Alpha Arena: live trading lost money.

The pattern the strongest systems share is "the model proposes, the runtime disposes." The LLM handles explaining, triaging, monitoring and generating research ideas. Deterministic code with hard limits makes every decision.

**What Jarvis should do with this:**
- **Decision path:** keep the 100-strategy engine and quality gate in charge of all trades. The LLM may only submit a typed proposal, and a pure-code policy function checks it. Its numbers must be checked against code-computed facts.
- **Leakage:** never count backtest results for LLM-in-the-loop logic on dates before the model's knowledge cutoff.
- **Fail closed:** apply the bug fixes from TradingAgents' 2026 changelog. Examples: an unreadable decision becomes REVIEW, not a tradeable Hold; past lessons only become visible once their outcome is known; a data-vendor failure is never reported as "no data".
- **Debate:** use one bounded red-team critique, not multi-round debate. Research shows debate does not beat simple voting and can turn correct answers into wrong ones.
- **News and social text:** treat it as hostile input. Strip hidden characters and normalize text. News may only reduce or block exposure, never increase it.

**Cost (Claude prices checked 2026-10-16):**
- Haiku 5.5 ($0.10/$0.50 per million tokens) for heartbeats and triage.
- Sonnet 5.5 ($2/$10) for chat and agent runs.
- Opus 5.5 ($4/$20) only for weekly deep reviews, run through the Batch API (50% off).
- Cache the fixed system prompt: cache reads cost 0.05x on Opus and Sonnet 5.5, and prompts of 512+ tokens can be cached.
- Enforce a daily dollar cap in code that steps down to Haiku and then to plain templates. Trading never depends on the LLM.
- Call the LLM only when something happens, behind code-side condition checks, not on every bar.

**Always-on operation:**
- A zero-dependency scheduler in node:sqlite, based on OpenClaw's design and Claude's scheduled-deployment rules. It covers cron with the America/Edmonton time zone and daylight-saving handling, leases, retry backoff, auto-disable, run records and per-run/per-day budgets.
- Hard limits on agents Jarvis creates itself: at most 20, expiry after 14 days, no agent can create another agent, read-only tools by default, and HALT pauses everything.
- One idempotent `advance()` function for both backtest and paper, with a hash-chained ledger.
- A Windows logon task plus a supervisor process that restarts the app if it crashes.

**Avoid:** LLM autopilot trading loops, famous-investor persona agents in the decision path, and RL-tuned trading LLMs.

### Hard split: the LLM emits a typed Proposal; a pure-code policy engine decides ("the model proposes, the runtime disposes")

This is the strongest point of agreement across independent projects, and it matches Jarvis's hard rules exactly (AI proposes, code decides; paper only; long-only; hard stop). The evidence in the next finding shows that letting the LLM decide trades end to end is not supported. Evidence strength: high, from several independent production-grade projects plus a 2026 position paper.

How: brain/propose.js: call Claude with one client tool `submit_proposal` defined with `strict: true` and `additionalProperties:false`. Forced tool_choice any/tool returns a 400 on Opus 5.5 and Sonnet 5.5, so use tool_choice auto plus an instruction; no tool call means NO_PROPOSAL. Schema: {kind: enum['enter','exit','reduce','alert','run_backtest','create_agent'], botId|strategyId (enum generated from the registry), symbol (enum from the universe), side: const 'long', entryRef: const 'next_bar_open', reasonCodes: enum[], factRefs: string[]}. The LLM never supplies a size or price. Code computes size from stop distance and the risk budget, and the stop comes from the strategy spec. policy/decide.js is a pure function (proposal, snapshot) -> {verdict: 'ACCEPT'|'REJECT'|'REVIEW', ruleIds[]}. Rules: paperOnly, longOnly, hasHardStop, dataFresh(asOf age <= maxAge per timeframe), strategyPassedGate, notHalted, exposureCaps, throttle. Append every verdict to the decision ledger. The tool gateway exposes no order-writing tool to any LLM context.

Sources: https://raw.githubusercontent.com/NoFxAiOS/nofx/main/README.md, https://raw.githubusercontent.com/AI4Finance-Foundation/FinRobot/master/README.md, https://raw.githubusercontent.com/virattt/ai-hedge-fund/main/VISION.md, https://arxiv.org/abs/2605.16895

### Evidence base: LLM-picked trades do not beat buy-and-hold once leakage, survivorship and sample size are controlled

This decides where Jarvis's edge should come from: the deterministic 100-variant engine plus the honest quality gate, not LLM trade selection. It also supports the honest messaging the owner should see. Evidence strength: high and consistent across independent groups, peer-reviewed venues (NeurIPS and KDD Datasets & Benchmarks), and live real-money results.

How: Make it a product rule in policy: the LLM may (a) explain, (b) propose a veto or size reduction, never an increase, which code still checks, and (c) propose research specs. Following FINSABER, every report and UI comparison against buy-and-hold is broken down by the vol-gate regime (LOUD/NORMAL/QUIET) and by trend state, both computed in code. Add a static 'Why Jarvis doesn't let AI pick trades' info card citing these papers.

Sources: https://arxiv.org/html/2412.20138v7, https://arxiv.org/abs/2510.07920, https://arxiv.org/abs/2505.07078, https://arxiv.org/html/2510.02209v1, https://arxiv.org/abs/2505.11065, https://arxiv.org/abs/2511.03628, https://forklog.com/en/four-out-of-six-ai-models-suffer-losses-in-trading-tournament/, https://arxiv.org/abs/2602.14233

### Contamination firewall: never count LLM-in-the-loop behaviour on dates before the model's knowledge cutoff

Without this firewall, any LLM feature (sentiment, veto, regime commentary) backtested over history will look good because the model already knows what happened. That would silently corrupt the quality gate. Evidence strength: high, from several 2025-2026 papers.

How: 1. llm/cutoffs.json: a manually maintained {model, trainingCutoff} list. If the cutoff is unknown, use the release date (conservative).
2. Quality-gate rule: any backtest whose signal path calls an LLM and covers dates earlier than cutoff + 30 days is marked CONTAMINATED and cannot pass.
3. Collect LLM features forward only. Store them in an sqlite table llm_features(id, created_at, asof, model, prompt_hash, input_hash, value). A backtest may only read rows with created_at <= bar timestamp, so LLM features are evaluated on live-collected data only.
4. Optional robustness probes, scheduled weekly on Haiku via the Batch API. Anonymize the ticker ('Company A'). Counterfactual-perturb the inputs (reverse a headline, flip the 5-day trend). If the output barely changes (low input dependency), reject the feature as memorized.

Sources: https://arxiv.org/html/2510.07920v1, https://arxiv.org/abs/2505.11065, https://arxiv.org/abs/2512.23847, https://arxiv.org/abs/2605.24564, https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/CHANGELOG.md

### Fail-closed lessons from TradingAgents' 2026 changelog (real bugs in the most-used multi-agent trading framework)

Each bug maps onto a Jarvis hard rule (fail closed, stale data, never invent numbers). Copying the fixes costs little and avoids repeating mistakes found in a heavily used project. Evidence strength: high (first-party changelog).

How: 1. parseDecision(): on any schema or enum mismatch, return {ok:false, sentinel:'REVIEW'}. REVIEW is never tradeable and shows in the UI.
2. The data layer returns a discriminated union {status:'ok'|'unavailable'|'stale'|'error', asOf, source, fetchedAt, data?}. 'unavailable' is never turned into [].
3. The latest bar must have finite OHLCV, otherwise throw DATA_INCOMPLETE. The engine then blocks new entries for that symbol, while stops still manage open positions.
4. Nothing gap-fills values shown to the LLM or the user.
5. config.js throws on invalid environment or config values at startup.
6. Lessons carry outcome_known_at, and retrieval filters on outcome_known_at <= asOf.
7. Debate or critique prompts never reference an opponent message that does not exist.

Sources: https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/CHANGELOG.md, https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/README.md

### 'Never invent numbers' as an enforced verifier, not just a prompt instruction

A prompt rule alone is not reliable. Automatic verification makes 'never invent numbers' mechanically true. It also looks advanced in the UI (provenance chips). Evidence strength: medium-high (production systems plus benchmark error analysis).

How: brain/grounding.js:
1. Before each call, build `facts` [{id:'F12', value, unit, asOf, source}] from engine state.
2. The system prompt requires every number to be written as a {{F12}} placeholder.
3. Afterwards, render the placeholders from the facts so all numeric text comes from code.
4. Scan the remaining text with /[-+]?[$]?\d[\d,]*(\.\d+)?%?/g. Allow ISO dates, times and small ordinals ≤10. Any other numeric literal causes a rejection and one retry with the error message.
5. If it fails again, fall back to a deterministic template message.

In the UI, numbers carry a 'computed' chip whose tooltip shows source and asOf. Twilio voice scripts are built only from templates plus facts, never from raw LLM numbers. The LLM never computes quantities; position sizing is a code function.

Sources: https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/README.md, https://raw.githubusercontent.com/AI4Finance-Foundation/FinRobot/master/README.md, https://raw.githubusercontent.com/OpenBB-finance/agents-for-openbb/main/README.md, https://arxiv.org/html/2510.02209v1

### Use bull/bear debate as one bounded red-team critique, not multi-round agent theatre

Multi-round debates multiply cost and latency, add room for injected or fabricated content, and have no demonstrated accuracy benefit. A single structured critique keeps the 'advanced' look at a fraction of the tokens. Evidence strength: medium-high (several 2025-2026 studies, though mostly not finance-specific).

How: Per proposal, at most 2 LLM calls:
1. A brief from Sonnet 5.5 at medium effort, using the facts.
2. A red-team call on a different tier (Haiku 5.5, or Sonnet if the brief used Opus) returning strict JSON: {risks:[{code: enum['EARNINGS_WITHIN_5D','LIQUIDITY','GAP_RISK','CORRELATED_EXPOSURE','DATA_STALE','NEWS_CONFLICT','REGIME_MISMATCH'], severity: 1-3, factRefs}]}.

Where possible, each code maps to a deterministic check that confirms it, and the policy engine applies confirmed risks. The UI renders the brief and the critique as a 'Bull vs Bear' panel.

Sources: https://arxiv.org/abs/2502.08788, https://arxiv.org/abs/2509.05396, https://arxiv.org/abs/2509.23055, https://arxiv.org/abs/2510.11695, https://arxiv.org/abs/2608.24069

### Claude cost control: route by job tier, cache the static prefix, batch overnight work, enforce dollar caps in code

An always-on assistant with many agents can quietly run up a large bill. Tiering plus caching plus batching is the documented biggest lever, and a code-enforced cap keeps cost bounded with no owner action. Evidence strength: high (official pricing and docs).

How: llm/router.js route table:
- heartbeat, intent classification, triage → claude-haiku-5-5, effort low.
- chat, daily brief, agent runs → claude-sonnet-5-5, effort medium.
- weekly strategy review, monthly postmortem, research-idea generation → claude-opus-5-5, effort high, via messages.batches during 01:00-05:00 America/Edmonton.

Prompt layout: frozen tools and system prompt (persona, hard rules, tool docs, strategy catalog) first with cache_control, then volatile facts. Add an integration test asserting usage.cache_read_input_tokens > 0 on the second identical call.

llm/budget.js: price table → USD per call computed from the usage fields (input, output, cache_creation, cache_read), stored in sqlite llm_spend(day, model, agentId, usd). Caps: global daily cap (default $2/day, configurable), per-agent caps, and a monthly cap. When a cap is hit, degrade to Haiku, then to deterministic templates. The paper fleet never depends on the LLM.

Sources: https://platform.claude.com/docs/en/about-claude/pricing, https://platform.claude.com/docs/en/build-with-claude/prompt-caching, https://platform.claude.com/docs/en/build-with-claude/batch-processing, https://platform.claude.com/docs/en/build-with-claude/effort, https://platform.claude.com/docs/en/build-with-claude/task-budgets, https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/README.md

### Event-driven LLM wake-ups: code-side condition gates mean quiet ticks cost nothing

This is how Jarvis can be fully automated around the clock without per-bar LLM costs or notification spam. It also keeps automatic phone calls rare and meaningful. Evidence strength: medium-high (mature open-source scheduler design, consistent with the cost evidence).

How: 1. The engine writes typed events to sqlite `events` (FILL, STOP_HIT, GATE_REGIME_CHANGE, DATA_STALE, DRAWDOWN_BREACH, QUALITY_GATE_RESULT, DIGEST_DUE).
2. watchers/*.js are pure functions (state, prevState) -> {fire, message, nextState}, each with a 30-second minimum interval.
3. The scheduler calls Claude only when a watcher fires. Dedupe by (type, symbol) within 15 minutes. Limit to ≤5 LLM event turns per minute, ≥30 seconds apart.
4. Code decides phone calls (Twilio) from severity: severity 3, or HALT-class at any time. Quiet hours are 22:00-07:00 America/Edmonton except HALT-class.
5. The model can answer NO_REPLY, which suppresses notifications.

Sources: https://docs.openclaw.ai/automation/cron-jobs/schedules, https://docs.openclaw.ai/gateway/heartbeat, https://docs.openclaw.ai/automation, https://raw.githubusercontent.com/NoFxAiOS/nofx/main/README.md, https://arxiv.org/html/2412.20138v7

### Agents Jarvis creates himself: a local scheduler spec with budgets, leases, expiry, retries and blast-radius limits

The owner wants Jarvis online with no button and able to create his own agents. Without hard quotas and expiry, self-created agents can multiply, burn budget, or keep running stale. Mirroring proven semantics avoids design mistakes such as daylight-saving double-fires. Edmonton observes DST, and Managed Agents fires repeated wall-clock times twice. Evidence strength: high (documented production schedulers).

How: sqlite tables:
- agents(id, name, purpose, created_by enum('owner','jarvis'), schedule_kind enum('cron','every','at','watcher'), cron, tz DEFAULT 'America/Edmonton', every_ms, pacing_min_ms, pacing_max_ms, model_tier, effort, tools_allow JSON (read-only set by default), budget_usd_run, budget_usd_day, max_runs_day, expires_at, status enum('active','paused','auto_disabled','expired','archived'), consecutive_failures, next_run_at, lease_owner, lease_until).
- agent_runs(id, agent_id, scheduled_for, started_at, ended_at, exec_status, delivery_status, usd, tokens_in, tokens_out, error_code, output_ref).

Scheduler loop: one setInterval(tick, 1000). Claim a run atomically with UPDATE ... WHERE lease_until IS NULL OR lease_until < now.

Zero-dependency cron parser: about 150 lines, 5 fields, day-of-month/day-of-week OR. Compute local wall-clock times with Intl.DateTimeFormat('en-CA', {timeZone:'America/Edmonton'}). Dedupe on a local-wall-clock key so a repeated fall-back hour fires once, and skip times that don't exist. Unlike Managed Agents, Jarvis should not double-fire.

Retries: backoff 30s, 60s, 5m, 15m, 60m. Five consecutive failures set auto_disabled and notify the owner.

Guardrails for Jarvis-created agents:
- At most 20 active, and at most 5 created per day.
- Default expiry 14 days unless the owner pins the agent.
- Depth 1: an agent cannot create agents.
- tools_allow must be a subset of the creator's tools, and never order or proposal tools unless the owner enables them.
- Minimum interval 15 minutes for LLM agents; 30 seconds for code-only watchers.
- A budget is mandatory.
- HALT pauses everything and is create-only.

Every create or update is logged, and the Agents panel offers one-tap pause.

Seed agents: pre-market brief 06:45 MT, open watcher, midday fleet digest, close recap 14:15 MT, nightly research batch 01:30, weekly review Saturday 09:00, and a data-staleness watchdog (code only).

Sources: https://docs.openclaw.ai/automation/cron-jobs/managing-jobs, https://docs.openclaw.ai/automation/cron-jobs/schedules, https://platform.claude.com/docs/en/managed-agents/scheduled-deployments, https://raw.githubusercontent.com/virattt/ai-hedge-fund/main/ROADMAP.md, https://raw.githubusercontent.com/openclaw/openclaw/main/LICENSE

### One `advance()` for backtest and paper; idempotent ticks; hash-chained ledger; any failure halts

This removes the classic gap between research code and production code, makes 24/7 restarts safe because a repeated tick does nothing, and gives a tamper-evident track record. Evidence strength: high (actively maintained, very popular project; the design is explicit).

How: engine/advance.js is a pure function (state, bar, broker) -> {state, records[]}. The broker is either a simulated fill model or the paper book; the same cost model applies to both.

ledger.js: each row stores hash = sha256(prevHash + canonicalJSON(record)) using node:crypto.createHash. On boot, replay the ledger and verify the chain; a mismatch triggers HALT (create-only) and an alert.

Idempotency: UNIQUE(tick_key) where tick_key = `${botId}:${barTs}`, so re-running a tick is a no-op.

Golden test: paper replay over N historical sessions must deep-equal the backtest records.

UI: a 'Next action' preview per bot.

Sources: https://raw.githubusercontent.com/virattt/ai-hedge-fund/main/VISION.md, https://raw.githubusercontent.com/virattt/ai-hedge-fund/main/ROADMAP.md, https://raw.githubusercontent.com/virattt/ai-hedge-fund/main/README.md

### 24/7 on Windows with no button: logon task plus zero-dependency supervisor plus catch-up policy

'Jarvis online without pressing a button' is mainly a process-lifecycle problem. A logon task plus a self-restarting supervisor is the simplest dependency-free answer for a beginner on Windows 11. Evidence strength: high (official Microsoft docs).

How: Ship install-autostart.cmd: schtasks /create /tn "Jarvis Terminal" /tr "\"C:\Program Files\nodejs\node.exe\" \"%~dp0supervisor.js\"" /sc onlogon /delay 0000:30 /rl LIMITED /f.

supervisor.js:
- child_process.fork('server.js'); on exit, restart with backoff 1s, 2s, 4s up to 60s; reset after 10 minutes stable.
- Writes a heartbeat row every 10 seconds. server.js exposes /health.
- Opens the browser UI automatically once.

On boot:
- Run the preflight checks from the throttles/safe-mode finding.
- Missed recurring LLM runs are skipped (avoids stale reminders and spend). Missed one-shots run only if less than 2 hours late.
- Missed bars are replayed by the deterministic engine through idempotent ticks.

Autopilot starts ON. The UI shows uptime and a single Pause/HALT. Tell the owner to set sleep to Never in Windows power settings, because a sleeping PC cannot run 24/7.

Sources: https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create, https://docs.openclaw.ai/automation/cron-jobs/managing-jobs

### Memory and reflection that cannot fool itself: settled outcomes, point-in-time lessons, cheap overnight consolidation

Memory makes Jarvis feel personal and smart, but lessons drawn from single trades are noise. Gating lessons on sample size and keeping them away from risk parameters keeps the experience without creating a new overfitting channel. Evidence strength: medium (designs are well documented; benefits are self-reported).

How: sqlite tables:
- decisions(..., settled_at, realized_ret, alpha).
- lessons(id, scope enum('symbol','strategy','regime','owner_pref'), text, evidence_ids JSON, outcome_known_at, n_supporting, layer enum('shallow','mid','deep'), importance, last_used).

The nightly consolidation runs on Haiku 5.5 via the Batch API: it merges duplicates and expires stale lessons.

A lesson is 'confirmed' only when supported by at least 30 settled trades or by an engine statistical test that passed. Otherwise it is labelled 'anecdote'.

Lessons may shape explanations and research proposals. They can never change risk parameters or turn strategies on or off; only the quality gate can.

Retrieval: zero-dependency BM25 over lesson text plus FinMem-style recency decay; top-5 lessons go into the prompt.

Also store owner preferences (call windows, risk comfort) as deep-layer memories.

Sources: https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/README.md, https://arxiv.org/html/2311.13743v2, https://arxiv.org/html/2407.06567v3, https://letta.com/blog/sleep-time-compute, https://arxiv.org/abs/2605.16895

### LLM as research-idea generator inside the quality gate, with every idea counted as a trial

This is the best-evidenced productive role for LLMs in quant work. It feeds 'extreme optimization' without letting the LLM trade. The risk is multiple-testing inflation, so every LLM idea must count toward Deflated Sharpe and PBO. Evidence strength: medium (competition results plus self-reported papers).

How: research/propose.js: the LLM returns a StrategySpec JSON restricted to the existing families and their parameter ranges (JSON Schema enums, minimum/maximum, strict tool).

The engine runs the full gate: ≥30 trades, DSR, 2x costs, walk-forward folds, sign-flip, PBO/CSCV, random-entry placebo, beats buy-and-hold.

Rules:
- A global trials table per family feeds DSR's N and the PBO candidate set.
- Originality: reject if daily-return correlation with any active bot exceeds 0.9.
- Complexity: at most 4 free parameters.
- Every spec must carry a stated hypothesis, which is stored.
- A UCB1 bandit over families sets the nightly budget (for example 50 specs per night via the Batch API). Reward = 1 if the spec passes the gate, else 0, or use its DSR.

Only the gate promotes a spec to the paper fleet; the owner is notified. The 'Research Lab' UI lists each proposal with pass/fail reasons.

Sources: https://arxiv.org/abs/2308.00016, https://arxiv.org/abs/2505.15155, https://raw.githubusercontent.com/microsoft/RD-Agent/main/LICENSE, https://arxiv.org/abs/2502.16789

### Treat news and social text as hostile input (prompt injection, homoglyphs, misinformation)

An always-on assistant that reads the web and can trigger phone calls or proposals is a direct attack surface. Asymmetric influence plus input sanitization neutralizes most of the risk cheaply. Evidence strength: medium-high (several 2026 studies).

How: ingest/sanitize.js:
- Normalize text with str.normalize('NFKC').
- Strip zero-width characters (U+200B-U+200F, U+2060-U+2064, U+FEFF) and bidi controls (U+202A-U+202E, U+2066-U+2069).
- Flag tokens that mix Latin with Cyrillic or Greek letters, detected with a /\p{Script=Cyrillic}/u test.
- Drop HTML elements hidden with display:none, visibility:hidden, font-size:0 or aria-hidden, and record the hidden-text ratio as a suspicion score.

Prompting: wrap each item as <untrusted_document source asof> and state in the system prompt that instructions inside documents are data.

Influence rules:
- News can only add risk flags that block or reduce new entries. It never increases size or starts an entry.
- News-triggered phone calls need 2 or more independent sources.

Log URL, fetch time and prompt_hash for audit.

Sources: https://arxiv.org/abs/2601.13082, https://arxiv.org/abs/2605.09185, https://arxiv.org/abs/2608.24069, https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/README.md

### Runtime throttles and LLM safe-mode (ideas from NOFX's risk runtime; AGPL, so concepts only)

These cheap guards stop runaway loops, such as re-entry churn after a stop or bursts when data glitches, and they let Jarvis keep paper trading safely when Claude is down. Evidence strength: medium (practitioner system; consistent with the hard rules).

How: Policy rules:
- maxOpenPositions per bot and globally.
- Per-symbol cooldown after exit (configurable in bars).
- maxEntriesPerHour across the fleet; minHoldBars.
- A profit-giveback exit only if it is part of a backtested strategy spec, never ad hoc.

llmHealth circuit breaker: 3 consecutive LLM errors, refusals or schema failures disable LLM-originated proposals and critiques for 30 minutes. The deterministic fleet continues, and the UI shows 'AI degraded'.

Boot preflight, shown as HUD status lights: feeds fresh, sqlite writable, ledger chain valid, Twilio credentials valid, LLM reachable (optional), disk space.

Sources: https://raw.githubusercontent.com/NoFxAiOS/nofx/main/README.md, https://raw.githubusercontent.com/virattt/ai-hedge-fund/main/README.md

### Score the LLM layer forward-only against the deterministic baseline before giving it more authority

This turns 'is the AI helping?' into a measured, honest answer and gives a data-driven path to more autonomy. It also catches regressions when models are upgraded. Evidence strength: medium-high (2026 methodology papers).

How: llm_shadow table: every LLM proposal, veto and critique, accepted or not, with ts, model, effort, prompt_hash, facts_hash, the engine's action without the LLM, and the action with it.

Settle each entry after its holding window. Metrics:
- Veto value-add = paper PnL with vetoes minus engine-only PnL, by regime.
- Hit rate by claimed direction.
- Brier score and reliability bins when the LLM gives probabilities.
- Cost per useful decision, in USD.

Authority ladder: vetoes may auto-apply only after 30 or more settled post-cutoff cases, a bootstrap 95% CI of value-add above 0, and a passed sign-flip test. The monthly 'Is the AI helping?' page shows the evidence.

Sources: https://arxiv.org/abs/2605.16895, https://arxiv.org/abs/2602.14233, https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/CHANGELOG.md, https://arxiv.org/abs/2511.03628

### Workflows first, agents only for open-ended research, and an Autopilot that is ON by default

This meets 'extremely simple to use but looks advanced'. Predictable workflows are cheaper and safer than free-roaming agents. A visible decision timeline with expandable 'Why?' panels looks sophisticated while staying honest. Evidence strength: medium (vendor guidance plus convergent product UX).

How: Routing: Haiku 5.5 classifies each owner utterance into an intent enum (status, explain, backtest, research, create_agent, settings, smalltalk), then a fixed handler workflow runs. Only 'research' and 'create_agent' use a tool loop (Sonnet 5.5 with a task budget).

Tools are poka-yoke: symbols and strategies are enums from the registry, as-of dates are capped at now, and there is no free-form SQL or shell.

UI:
- Autopilot ON at boot, with a status line such as 'Watching 99 bots · next: Pre-market brief 06:45'.
- A decision timeline where each item has a 'Why?' expander showing rule verdicts, facts and the LLM brief.
- One Pause button and a HALT button.
- A daily HTML report snapshot saved to disk.

Sources: https://www.anthropic.com/engineering/building-effective-agents, https://raw.githubusercontent.com/NoFxAiOS/nofx/main/README.md, https://raw.githubusercontent.com/virattt/ai-hedge-fund/main/README.md, https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/README.md

### Avoid: LLM-as-trader autopilot loops, famous-investor persona agents, and RL-tuned 'trading LLMs' as decision makers

These designs contradict the hard rules (the AI decides), lack out-of-sample evidence, and add cost and attack surface. Evidence strength: high for the negative live results; the persona and RL claims are unverified.

How: Do not implement a place_order tool, persona agents in the decision path, or fine-tuned trading models. Personas may exist only as an educational chat style ("explain this like a value investor") with no access to the proposal or policy path. Keep the LLM's role limited as described in the 'hard split' and 'research-idea generator' findings.

Sources: https://forklog.com/en/four-out-of-six-ai-models-suffer-losses-in-trading-tournament/, https://www.gncrypto.news/news/qwen-wins-alpha-arena-season-1-with-22-percent-returns/, https://raw.githubusercontent.com/virattt/ai-hedge-fund/main/VISION.md, https://arxiv.org/abs/2509.11420, https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/README.md

## research

I surveyed freqtrade (GPL-3.0, docs current to 2026.4), Jesse (MIT), Hummingbot and its Condor AI harness (Apache-2.0 / MIT), NautilusTrader (LGPL-3.0), QuantConnect LEAN (Apache-2.0), vectorbt (Apache-2.0 + Commons Clause) and vectorbt PRO (proprietary), backtrader (GPL-3.0), OctoBot (GPL-3.0), Superalgos (Apache-2.0), freqtrade-strategies and NostalgiaForInfinity (both GPL-3.0), plus QuantStats (Apache-2.0) and Optuna (MIT) as helper libraries. I checked each claim against raw GitHub docs or source via curl, or against official doc pages.

**What we already have.** Our Python engine already covers the core anti-overfitting gate: Deflated Sharpe, PBO via CSCV, walk-forward folds, 2x costs, sign-flip, random-entry placebo and beats-buy-and-hold. None of the surveyed frameworks ships that whole gate built in. vectorbt PRO comes closest, but it is a paid product.

**Highest-value gaps (must):**
1. An automated lookahead-bias harness (freqtrade `lookahead-analysis`).
2. A warmup check that indicator values settle (freqtrade `recursive-analysis`). Ties into fail-closed.
3. Code-enforced protections and trading states (freqtrade StoplossGuard / MaxDrawdown / LowProfitPairs / Cooldown, plus Nautilus ACTIVE / REDUCING / HALTED).
4. A Condor-style agent harness. It answers "agents Jarvis can create": a tick loop, configs the agent can suggest vs limits only the owner can change, a pre-tick risk block that includes a daily LLM-cost cap, a per-session journal plus up to 20 long-term learnings, and a dry_run → run_once → loop lifecycle.
5. A zero-dependency TPE optimizer modelled on Optuna's defaults, with Jesse's trade-count-scaled fitness score and a held-out test period. LEAN's coarse-to-fine Euler search is the deterministic option.
6. vectorbt PRO-style robustness checks: parameter-plateau check, 1–2 bar signal delay, filter ablation, and a permutation test that reruns the whole parameter search on shuffled histories.
7. Honest intrabar fills: Nautilus's open-proximity high/low ordering, slippage capped to the bar range (backtrader), fills capped at a percentage of bar volume, and gap-through stops filled at the open.
8. A LEAN / QuantStats / freqtrade-grade tearsheet.
9. A manifest-based strategy plugin API with optimizable parameters declared inside each strategy.
10. Background jobs plus startup state reconciliation for 24/7 operation.
11. A freqtrade-style command set and on/silent/off notification matrix, mapped to voice, SMS and phone calls.

**Should and avoid.** "Should" items cover:
- Jesse's bootstrap rule-significance pre-filter.
- Monte Carlo outcome ranges.
- FreqAI-style retraining cadence and model expiry for the vol gate.
- Universe filters.
- Triple-barrier positions with an honest close-type column.
- Recording AI signals at the time they are made (OctoBot), so backtests replay them instead of re-asking an LLM about history.
- An incremental data downloader.

The "avoid" item covers copying GPL / LGPL / Commons-Clause code, the removed freqtrade Edge module, letting an LLM sit in the per-order path, and exposing the UI beyond 127.0.0.1. All findings below are ideas to re-implement ourselves in Node 24 with no npm dependencies besides the Anthropic SDK. The MIT and Apache-2.0 sources (Jesse, Condor, LEAN, QuantStats, Optuna) are the only ones whose formulas could be ported closely, with attribution.

### Automated lookahead-bias harness (freqtrade lookahead-analysis)

Porting 100 Python strategies to JS is exactly when off-by-one and full-series aggregation bugs creep in. freqtrade's docs say lookahead bias is usually THE driver of too-good-to-be-true results. Our engine says closed-bar signals, but today nothing proves it for each strategy. Evidence is strong: a mature, widely used tool, and the method is mechanical.

How: Add engine/validate/lookahead.js with two tests.

(1) Truncation test, freqtrade style: compute baseline = indicators(candles) and signals(candles). Pick up to N=20 bars that have entry signals and 20 with exit signals. For each bar t, recompute on candles.slice(0, t+1) and compare every indicator column and the signal at t with the baseline (tolerance 1e-9 relative). Report biasedIndicators[] and biasedSignals.

(2) Future-perturbation test (cheaper, stronger): replace bars t+1..end with a seeded random walk. Every signal and indicator value at or before t must stay identical.

While testing, force market fills, protections off and an unlimited paper wallet, as freqtrade does. Run it in worker_threads. Store the result in the SQLite table validation_runs(strategy_id, version, test='lookahead', passed, details_json, created_at).

Policy: a strategy or variant cannot be promoted to the fleet or an agent unless the latest lookahead run for its exact version passed. Also run it in `node --test` for every strategy file in CI. Endpoint: POST /api/validate/lookahead {strategyId} returns jobId.

Sources: https://www.freqtrade.io/en/stable/lookahead-analysis/, https://raw.githubusercontent.com/freqtrade/freqtrade/develop/docs/lookahead-analysis.md, https://github.com/freqtrade/freqtrade/blob/develop/LICENSE

### Warmup sufficiency check (freqtrade recursive-analysis)

Our live loop must fail closed on stale or insufficient data. Without a measured warmup, live RSI(14) or EMA values can differ from the backtest (freqtrade's example shows rsi_14 off by 24% with 20 startup candles), so paper results silently drift from the backtest. Evidence is strong and the check is cheap.

How: engine/validate/warmup.js: for each strategy and each default parameter set, take a 5000+ bar benchmark and compute the last-bar value of every indicator column for startup lengths [20,40,80,100,150,300,500,999]. Write the % variance table to validation_runs.

Set the manifest's startupBars to the smallest length where max |variance| < 0.1% for every indicator used by entry or exit.

In the live engine, the feed must hold at least startupBars+1 closed candles before evaluating. Otherwise emit DENY reason=INSUFFICIENT_HISTORY and take no trade (fail closed). In backtests, load startupBars before the start of the test range, as freqtrade does, and never trade inside the warmup window.

Sources: https://www.freqtrade.io/en/stable/recursive-analysis/, https://raw.githubusercontent.com/freqtrade/freqtrade/develop/docs/recursive-analysis.md, https://www.freqtrade.io/en/stable/strategy-customization/

### Code-enforced protections plus ACTIVE / REDUCING / HALTED trading states (freqtrade protections + NautilusTrader RiskEngine)

This gives our 99-bot fleet and the agents automatic circuit breakers that need no button press and that the AI cannot bypass. It matches 'AI proposes, code decides' and 'HALT is create-only'. Running the same protections inside backtests keeps backtest and paper behaviour identical. Evidence is strong: freqtrade and Nautilus both run protections on the same code path in simulation and live.

How: engine/risk/protections.js: pure functions (closedTrades, now, cfg) → locks[]. Defaults copied as numbers from the docs above, editable only through the owner settings UI, never through AI tools.

engine/risk/state.js: per-bot, per-agent and global state machine. ACTIVE → REDUCING (only stop tightening and exits) → HALTED (no new entries; existing hard stops still enforced).
- HALT rows sit in an append-only table halts(id, scope, reason, created_by, created_at). The gateway exposes only create_halt to the AI or agents.
- clear_halt exists only as an owner-authenticated UI or API action.

Every proposed order passes riskCheck(order) and returns {ok} or {deny: CODE}. Codes: TRADING_HALTED, STATE_REDUCING, PAIR_LOCKED, NO_STOP, NOT_LONG_ONLY, STALE_DATA, SIZE_EXCEEDS_LIMIT, EXPOSURE_EXCEEDS_LIMIT, RATE_LIMIT.

The backtester calls the same protections module, so optimization can tune protections as their own space. Events protection_trigger and protection_trigger_global feed the notification matrix (a global trigger places a phone call). Endpoints: GET /api/locks; POST /api/locks/:id/unlock (owner only).

Sources: https://www.freqtrade.io/en/stable/plugins/#protections, https://raw.githubusercontent.com/freqtrade/freqtrade/develop/docs/includes/protections.md, https://www.freqtrade.io/en/stable/hyperopt/#optimizing-protections, https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/docs/concepts/execution/index.md

### Autonomous agent harness Jarvis can spawn (Hummingbot Condor pattern)

This directly delivers the owner's 'Jarvis online 24/7 with as many agents as he wants' while keeping 'AI proposes, code decides'. Condor's docs say they moved indicator computation out of the LLM into deterministic routines, which cut session time from 2 minutes to under 1 and improved reliability. That matches our typed tool gateway and our cost goals. Evidence: current (2026) open-source design from a major project, though it is new and Condor itself flags the risk surface.

How: SQLite tables:
- agents(id, name, goal_md, tick_sec, universe_json, configs_json, limits_json, mode CHECK IN ('dry_run','run_once','loop','paused'), created_by, created_at)
- agent_ticks(agent_id, tick_no, started_at, observe_json, prompt, response, tool_calls_json, decisions_json, blocked_reason, llm_cost_usd)
- agent_learnings(agent_id, ts, text), capped at 20 rows by trigger
- agent_journal(agent_id, session, summary)

TickEngine is a single scheduler with setTimeout per agent and jitter; it skips a tick if the previous one is still running.
- Observe: the code providers (closed candles, vol-gate state, positions WHERE agent_id=?, locks, halts, data freshness).
- Pre-tick riskState: if blocked, write blocked_reason and do not call Claude.
- Decide: Anthropic SDK with tool_use limited to propose_paper_entry, propose_exit, tighten_stop, add_learning, suggest_config_change, create_halt and notify_owner. Use claude-haiku-5-5 for quiet ticks, claude-sonnet-5-5 when a code-detected signal exists, and claude-opus-5-5 for weekly reviews.
- Act: every proposal goes through riskCheck() and the paper broker; denials are logged with a code.

Limits are owner-only (PUT /api/agents/:id/limits requires the owner session). Jarvis's create_agent tool may only set configs inside owner-defined global caps, and new agents always start in dry_run. Auto-promotion runs dry_run → run_once → loop after K clean dry ticks (all fully paper). Track daily LLM spend from response usage tokens times the price table. Endpoints: GET/POST /api/agents, POST /api/agents/:id/mode, GET /api/agents/:id/ticks?limit.

Sources: https://github.com/hummingbot/condor, https://condor.hummingbot.org/motivation, https://condor.hummingbot.org/llms-full.txt, https://condor.hummingbot.org/webui/agents

### Zero-dependency TPE optimizer with Jesse-style fitness, held-out test, and LEAN Euler coarse-to-fine search

The owner wants extreme optimization. A TPE search reaches good regions in far fewer backtests than a full grid, which matters on a home PC running 100 variants. Jesse's log trade-count scaling discourages tiny-sample flukes. The held-out test plus our existing WFO, DSR and PBO gate guards the output. freqtrade practitioners report hyperopt making strategies worse out of sample when used without an edge, so optimization must feed the gate and must never be the gate itself. Evidence is strong: these are well-documented algorithms.

How: engine/opt/space.js turns manifest parameters ({type:'int'|'float'|'cat', min, max, step}) into a sampler domain.

engine/opt/tpe.js:
- Seeded PRNG (xoshiro128**). Log the seed as random_state.
- 30 random startup trials.
- Split finished trials by gamma(n) = min(ceil(0.1n), 25).
- Numeric parameters: Parzen mixture of truncated Gaussians, one per observation, with sigma = max(distance to neighbours, (max−min)/100), plus a uniform prior of weight 1. Sample 24 candidates from l and pick argmax log l − log g.
- Categorical parameters: smoothed counts.
- worker_threads pool sized to os.availableParallelism()−1, with constant liar (pending trials count as the worst value).
- Early stop after 25% of max epochs without improvement; hard cap 1000.

engine/opt/euler.js: deterministic coarse-to-fine grid for 1–2 parameters.

engine/opt/fitness.js: Jesse formula plus our cost-in-R rule, with selectable objectives (sharpeDaily, sortinoDaily, calmar, worstPairCalmar, multiMetric).

Storage: opt_runs(id, strategy, space_json, seed, objective, train_range, test_range) and opt_trials(run_id, n, params_json, fitness, train_metrics_json, test_metrics_json, state). The UI leaderboard shows train and test side by side. The top-K candidates go automatically to the existing walk-forward and quality gate; only gate-passing parameters are ever deployed. Endpoint: POST /api/optimize {strategyId, objective, epochs} returns jobId.

Sources: https://optuna.readthedocs.io/en/stable/reference/samplers/generated/optuna.samplers.TPESampler.html, https://github.com/optuna/optuna/blob/master/optuna/samplers/_tpe/sampler.py, https://www.freqtrade.io/en/stable/hyperopt/, https://raw.githubusercontent.com/jesse-ai/jesse/master/jesse/modes/optimize_mode/fitness.py, https://docs.jesse.trade/docs/research/optimize, https://github.com/QuantConnect/Lean/blob/master/Optimizer/Strategies/EulerSearchOptimizationStrategy.cs

### Robustness battery beyond our current gate (vectorbt PRO checks)

We already have DSR, PBO/CSCV, sign-flip, a random-entry placebo and 2x costs. The missing pieces are the plateau test, the delay test, ablation, and the permutation test of the whole search. Those catch overfitting that single-strategy tests miss, especially across 100 variants × many parameters. Evidence: methodology from the leading vectorized backtester, consistent with López de Prado's work.

How: engine/validate/robustness.js runs after optimization.
(a) Plateau: for the chosen parameters, evaluate all ±1-step neighbours (3^d−1, capped at 26). Pass if the median neighbour test-Sharpe ≥ 0.7×best and no neighbour has negative expectancy. Pick final parameters by the highest neighbourhood-mean, not the raw peak.
(b) Delay: re-run with entries and exits shifted +1 and +2 bars. Pass if Sharpe stays > 0 and ≥ 50% of base.
(c) Cost stress: 3× fees and slippage must stay positive in R.
(d) Ablation: each manifest filter toggled off. Report the Sharpe delta, and flag filters adding < 0.1 Sharpe as complexity to remove.
(e) Whole-search permutation: K=100 shuffled-return histories (permute bar log-returns, rebuild OHLC from the shuffled returns while keeping each bar's own high/low ratios), rerun the same TPE budget (or the top grid), and set p = share of histories whose best fitness ≥ the real one. Require p < 0.05.
(f) Mirrored OHLC and GBM controls, reported only.

All results go to validation_runs, and the UI shows a pass/fail checklist per strategy. The heavy runs (e) are queued as overnight jobs.

Sources: https://vectorbt.pro/features/optimization/robustness-and-overfitting/, https://vectorbt.pro/features/optimization/, https://github.com/polakowo/vectorbt/blob/master/LICENSE.md

### Honest intrabar fill model shared by backtester and paper broker (Nautilus, backtrader, freqtrade)

The owner wants an honest backtester, and our rule is 'never invent numbers'. Same-bar stop-vs-target ambiguity, gap-through stops and illiquid fills are the most common sources of backtest-vs-paper drift for ORB, NR7 and breakout strategies on 5m–1h bars. Evidence is strong: documented, deliberate design choices across three mature engines.

How: engine/exec/fillModel.js, used by BOTH backtester.js and paperBroker.js. Rules:
- Market entry at the next bar open × (1 + slipPerc), capped at bar.high.
- Long stop: if open ≤ stop then fill = open (gap-through), else fill = stop × (1 − slipPerc), floored at bar.low.
- Same-bar stop and target: by default stop first (conservative). Option 'adaptive' uses the Nautilus open-proximity rule.
- Optional detailTf: if a bar holds an open trade or signal and 1m data exists, walk the 1m bars in order for exact sequencing (freqtrade timeframe-detail).
- Fill qty = min(order qty, maxBarVolPct (default 2%) × bar.volume); the remainder is carried as a partial fill or cancelled per strategy.
- Limit orders use a seeded probFillOnLimit (default 1.0; stress 0.5).

Every fill row records model_version and the reason (gap_open, stop, target, time). A unit-test fixture of hand-built bars checks each rule. Report 'model sensitivity': results under conservative vs adaptive ordering.

Sources: https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/docs/concepts/backtesting/bar-execution.md, https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/docs/concepts/backtesting/fill-models.md, https://www.backtrader.com/docu/slippage/slippage/, https://www.backtrader.com/docu/filler/, https://www.freqtrade.io/en/stable/backtesting/#assumptions-made-by-backtesting

### Professional tearsheet: freqtrade summary + LEAN report + QuantStats metrics + Jesse benchmark matrix

This makes the software look and be advanced, and gives the beginner owner clear evidence. The per-tag and per-exit-reason tables show WHICH of the 100 variants or exit paths actually earn. The autocorrelation-penalized Sharpe stops mean-reversion strategies with smoothed returns from looking better than they are. Evidence is strong: these are standard reports across the industry.

How: engine/report/metrics.js: pure functions over trades[] and dailyEquity[], with every number computed in code and stored in a reports table as JSON. Claude only narrates from this JSON, under the never-invent rule.

engine/report/render.js: dependency-free SVG for the equity curve, underwater plot (top-5 drawdowns), monthly heatmap, annual bars, R-multiple histogram and rolling 6/12-month Sharpe. Also PSR at SR*=0 and SR*=1, and our own crypto/equity stress windows (for example 2020-03 COVID crash, 2022-05 LUNA, 2022-11 FTX, 2024-08 carry unwind), date-bounded and shown only where they overlap the data.

Benchmark view: POST /api/benchmark {strategies[], symbols[], tfs[]} → jobs. The result is a sortable matrix with a gate-status column.

Endpoints: GET /api/report/:runId (JSON), GET /api/report/:runId.html (self-contained export), GET /api/fleet/breakdown?period=day|week|month.

Sources: https://www.freqtrade.io/en/stable/backtesting/#understand-the-backtesting-result, https://www.freqtrade.io/en/stable/advanced-backtesting/, https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/report, https://github.com/ranaroussi/quantstats/blob/main/quantstats/stats.py, https://github.com/jesse-ai/jesse

### Manifest-based strategy plugin API with in-strategy optimizable params and lookahead-safe higher-timeframe merge

With 100 variants plus strategies the AI writes, we need one strict contract. The engine can then automatically optimize, validate (lookahead and warmup), report per tag, and refuse unsafe code such as a missing stop or a short entry. This is how 'the AI proposes, code decides' applies to strategy authorship. Evidence is strong: these are the two most-used open-source crypto frameworks.

How: strategies/<id>.js exports:
- meta = {id, version, interfaceVersion: 1, timeframe, informative: [{tf: '1d', cols: ['close', 'ema200']}], startupBars, longOnly: true, params: {len: {type: 'int', default: 2, min: 2, max: 6, step: 1, space: 'entry', optimize: true}}, filters: ['trend', 'volGate'], plot: {...}}
- Pure functions: indicators(c, p) → columns; entry(c, p, i) → {tag} | null; exit(c, p, i, trade) → {reason} | null; initialStop(c, p, i) → price, which is required; trailStop(trade, c, i) → price, ratchet-up only and enforced by the engine.

engine/strategy/loader.js validates the schema and rejects modules without initialStop or with longOnly !== true. It runs strategies in a vm-free worker with a frozen candles array.

engine/data/informative.js: mergeInformative(base, inf, tf) maps each base bar to the last higher-timeframe bar whose CLOSE time ≤ the base bar's close time.

Routes table: routes(bot_id, venue, symbol, tf, strategy_id, params_json), with UNIQUE(bot_id, venue, symbol). AI-authored strategies are written only to strategies/sandbox/ and promoted after lookahead + warmup + quality gate pass.

Sources: https://www.freqtrade.io/en/stable/strategy-customization/, https://www.freqtrade.io/en/stable/strategy-callbacks/, https://www.freqtrade.io/en/stable/hyperopt/#hyperoptable-parameters, https://docs.jesse.trade/docs/routes

### 24/7 runtime: background job queue, startup reconciliation, doctor self-check, shared indicator cache

The owner's first complaint is having to press a button. A supervised always-on process that resumes its own jobs and self-checks fixes that. Reconciliation means a Windows reboot never corrupts the paper book. The shared cache keeps 99 bots plus agents cheap on a home PC. Evidence is strong: these are the operational patterns of production bots.

How: Jobs table: jobs(id, type, params_json, status IN ('queued','running','done','failed'), progress, result_ref, heartbeat_at, created_at). Run jobs in a worker_threads pool; on boot, re-queue 'running' jobs with heartbeat older than 2 minutes. Endpoints: POST /api/jobs/{backtest|optimize|validate|mc}, GET /api/jobs/:id, GET /api/jobs?status=. Heavy jobs are capped at 1 concurrent; light jobs at N.

Boot sequence:
1. PRAGMA quick_check.
2. Rebuild positions from the immutable fills ledger and diff against the positions cache. On mismatch, raise a HALT for the affected bot and alert the owner.
3. Data freshness check.
4. Start the scheduler.
5. Send an SMS or speak 'Jarvis online'.
On SIGINT/SIGTERM: stop schedulers, flush, close the DB.

Windows autostart via `schtasks /Create /SC ONLOGON` running a watchdog .cmd that restarts node on exit. `node jarvis.js doctor` (read-only) checks Node ≥ 24, DB integrity, data staleness per symbol, presence of the Anthropic, Twilio and Ollama keys (masked) and their reachability, and that the port is bound to 127.0.0.1.

engine/data/indicatorCache.js memoizes columns by key (symbol, tf, indicator, paramsHash, lastClosedTs); fleet bots subscribe through an EventEmitter on bar close.

Sources: https://www.freqtrade.io/en/stable/rest-api/, https://www.freqtrade.io/en/stable/recursive-analysis/#running-recursive-analysis-via-the-rest-api, https://www.freqtrade.io/en/stable/producer-consumer/, https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/docs/concepts/live.md, https://github.com/hummingbot/condor

### Operator command set and per-event notification levels mapped to voice, SMS and phone (freqtrade Telegram + Condor)

Simple to use but advanced: the owner speaks natural language and Jarvis maps it to a small, auditable command set. A three-level notification matrix decides whether something deserves a phone call, an SMS or just a log line. That fixes the phone system needing a button, without call spam. Evidence is strong: these are the most-used bot operator UX patterns.

How: jarvis/commands.js: an intent registry {name, args schema, handler, role: 'owner'|'ai'}. status, profit(n), daily/weekly/monthly(n), performance, exits, locks, unlock (owner only), pause_entries (creates a REDUCING state; AI-allowed), halt (create-only, AI-allowed), clear_halt (owner only), report(runId), agents, memory and logs(n) are all available through the voice UI, the web UI, Twilio SMS webhook keywords (STATUS, PROFIT 7, HALT) and optional Telegram long-polling with fetch and getUpdates, restricted to owner_chat_id.

notify.js: a matrix in SQLite settings, e.g. {halt: 'call', protection_trigger_global: 'call', data_stale_over_15m: 'call', daily_summary: 'sms', entry: 'silent', exit_stop_loss: 'sms', '*': 'silent'}, where call = Twilio Voice with TwiML <Say>, sms = Twilio SMS, silent = in-app feed. Add quiet hours in America/Edmonton and dedupe/rate-limit (at most 1 call per 15 minutes unless it is a HALT).

Every AI answer in the UI carries collapsible 'Reasoning' and 'Tools used (N)' panels sourced from the agent_ticks and tool logs.

Sources: https://www.freqtrade.io/en/stable/telegram-usage/, https://raw.githubusercontent.com/freqtrade/freqtrade/develop/docs/telegram-usage.md, https://condor.hummingbot.org/webui/agents, https://github.com/hummingbot/condor

### Cheap rule-significance pre-filter before full backtests (Jesse bootstrap test)

Jarvis and its agents will generate many strategy ideas. A significance screen costing milliseconds rejects noise before spending TPE, walk-forward and Monte Carlo compute. Detrending stops long-only rules from looking good just because BTC went up. Evidence: documented method (Jesse, MIT), based on standard White's Reality Check-style bootstrap ideas.

How: engine/validate/significance.js: given closed-bar long signals s[] ∈ {0,1}:
- r[t+1] = ln(C[t+1]/C[t]) − mean(ln returns).
- x[t] = s[t]·r[t+1], and m = mean(x).
- centered = x − m.
- Stationary bootstrap (Politis–Romano): at each step continue the block with probability 1−1/10, otherwise jump to a random index, with wraparound.
- B=2000 with a seeded PRNG; p = count(simMean ≥ m)/B.

Run it across all 100 variants × symbols and apply Benjamini–Hochberg FDR (q=0.10), because Jesse notes there is no multiple-testing correction. Store the result in validation_runs. Policy: p_adj ≥ 0.10 means 'no evidence' and the idea is skipped from optimization unless the owner overrides.

Sources: https://docs.jesse.trade/docs/rule-significance-testing/bootstrap, https://docs.jesse.trade/docs/research/rule-significance-testing, https://github.com/jesse-ai/jesse

### Monte Carlo outcome ranges: trade-order shuffle + moving-block candle bootstrap (Jesse, QuantStats)

A beginner needs a realistic range, not one equity curve. For example: in 90% of reshuffles the worst drawdown was between A and B; the chance of a 20% drawdown is C%. That range can also size risk-per-trade automatically so the 95th-percentile drawdown stays within the owner's limit. Evidence: established practice in two current frameworks.

How: engine/validate/montecarlo.js:
(a) Trade-order shuffle: Fisher–Yates on the trades' R-multiples with a seed, 1000 sims. For each, compute max drawdown, longest losing streak and time-to-recover; report p5/p50/p95.
(b) Bootstrap with replacement for the CAGR and Sharpe distributions, plus bust probability (equity < −X%).
(c) Candle moving-block bootstrap: take base-timeframe log returns plus each bar's (h/c, l/c, o/prevC) ratios, resample blocks of one week, rebuild OHLC (enforcing h ≥ max(o,c), l ≤ min(o,c)), re-run the strategy, 200 sims.

Flag 'fragile' if the original Sharpe is above the 90th percentile of the candle-bootstrap distribution.

sizing.js: riskPerTrade = the largest value in a list such that p95 drawdown ≤ the owner's limit, which the code enforces. Results go to mc_runs. The UI shows a fan chart and a plain-English sentence generated only from the stored numbers.

Sources: https://github.com/jesse-ai/jesse, https://docs.jesse.trade/docs/monte-carlo/, https://docs.jesse.trade/docs/monte-carlo/candle-pipelines, https://github.com/ranaroussi/quantstats/blob/main/quantstats/stats.py

### Self-adaptive retraining cadence and model expiry for the volatility gate (FreqAI)

Our walk-forward logistic LOUD/NORMAL/QUIET gate must be backtested exactly as it will be retrained live. Otherwise the gate leaks in-sample information. An explicit expiry turns 'model is stale' into a fail-closed condition. Evidence is strong: a documented production design.

How: engine/gate/volGate.js gains config {trainDays: 365, retrainEveryDays: 7, expiryHours: 36}.
- Backtest: loop over windows, fit on [t−trainDays, t), predict on [t, t+retrainEveryDays), with no refit inside the window.
- Live: the scheduler retrains after the daily close when the model is older than retrainEveryDays.
- Store gate_models(id, symbol, trained_from, trained_to, coef_json, created_at); keep the last 5.
- predict() returns UNKNOWN when the model is older than expiryHours or the inputs are stale, and policy maps UNKNOWN to no new entries.
- Winsorize features at 1%/99% using training-window percentiles only.

Sources: https://www.freqtrade.io/en/stable/freqai/, https://www.freqtrade.io/en/stable/freqai-running/

### Triple-barrier position executor with honest close types, plus portfolio risk overlays (Hummingbot, LEAN)

Our hard-stop-on-every-trade rule maps directly onto a required barrier. The time limit stops dead positions from tying up paper capital, which is good for ORB and first-hour strategies. Close types make 'why did we exit' auditable for the AI and the owner. Triple-barrier outcomes also give natural labels for future model training. Evidence is strong.

How: engine/exec/position.js: Position {entry, qty, barriers: {stop (required, ratchet-up only), target?, timeLimitBars?, trail?: {activatePct, deltaPct}}}. It is evaluated on each closed bar through fillModel.js, and the first barrier hit closes with close_type IN ('stop_loss', 'take_profit', 'time_limit', 'trailing_stop', 'signal_exit', 'protection', 'halt_flatten', 'failed').

Portfolio overlays in engine/risk/overlays.js: per-bot trailing equity drawdown sets REDUCING, global portfolio drawdown sets HALTED (create-only), plus a max exposure per symbol and per correlated cluster (BTC/ETH/SOL as one cluster).

UI: an Executions ledger with filters (pair, strategy, close_type), a stat strip (PnL, win rate, fees) and CSV export.

Sources: https://hummingbot.org/strategies/v2-strategies/executors/positionexecutor/, https://condor.hummingbot.org/executors/position-executor, https://condor.hummingbot.org/webui/executors, https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/risk-management/supported-models

### Point-in-time recording of every AI signal; never re-query an LLM over history (OctoBot AI evaluator)

LLMs have memorized market history, so asking Claude about 2023 candles in a backtest is lookahead bias by construction. Recording proposals at the time they are made is the only honest way to measure whether Jarvis's AI layer adds value. It also enforces 'never invent numbers' because the record is immutable. Evidence: a concrete implementation in a widely used bot.

How: Table ai_signals(id, ts_utc, agent_id, model, prompt_hash, inputs_ref (candle ts range + feature snapshot hash), output_json (schema-validated: {action: 'enter'|'exit'|'hold', symbol, confidence 0..1, rationale}), accepted BOOL, deny_code, linked_trade_id). It is append-only and written by the gateway before policy evaluation.

The backtester's 'AI overlay' mode can only replay ai_signals for timestamps inside the range. Otherwise the overlay is neutral and the report says 'AI layer not testable before <first signal date>'.

A weekly job compares the accepted-AI-signal subset with the code-only baseline: delta expectancy in R with a bootstrap confidence interval. Code-enforced minimum confidence and the schema stop free-text from ever reaching the broker.

Sources: https://github.com/Drakkar-Software/OctoBot-Tentacles/blob/master/Evaluator/TA/ai_evaluator/ai.py, https://github.com/Drakkar-Software/OctoBot

### Incremental, idempotent data downloader with coverage and gap reports (freqtrade download-data, Jesse import)

Extreme backtesting needs deep, clean history. Fail-closed live trading needs freshness monitoring. Incremental and idempotent downloads let the 24/7 scheduler keep data current with no button press, and a coverage report shows the owner data health at a glance. Evidence is strong.

How: data/downloader.js uses built-in fetch, with per-venue adapters for public OHLC endpoints (only endpoints with no keys, paper only), a token-bucket rate limiter and exponential backoff.

Schema candles(venue, symbol, tf, ts, o, h, l, c, v, PRIMARY KEY(venue, symbol, tf, ts)) WITHOUT ROWID, written with INSERT OR IGNORE so it is idempotent.
- update(): start = MAX(ts)+tf per key, end = last CLOSED bar.
- prepend(from): fetch older pages down to `from`.
- Store the finest base (1m or 5m) where available and derive higher timeframes deterministically: aggregate only complete buckets, never a partial current bar.
- gaps(venue, symbol, tf): detected via ts diffs, written to data_gaps.
- coverage(): earliest, latest, count, gap count and staleness in minutes; feeds GET /api/data/coverage and the doctor command.

The scheduler calls update() just after each bar close (plus a 10s grace period). If staleness exceeds 2× tf, the symbol's state becomes STALE and entries are denied.

Sources: https://www.freqtrade.io/en/stable/data-download/, https://docs.jesse.trade/docs/import-candles

### Daily universe filters (freqtrade pairlist handlers)

Many of our strategies (ORB, squeeze, vol-breakout, meme sleeve) only make sense on liquid, sufficiently volatile and not-too-new symbols. A code-defined, logged universe stops agents from 'discovering' illiquid coins that make paper results look fake. Evidence: mature, documented feature.

How: engine/universe/filters.js: an ordered list of pure filters (symbols, marketData, cfg) → {kept, dropped: [{symbol, filter, reason}]}.
- age: daysListed ≥ 60.
- liquidity: 30-day median quote volume ≥ X.
- spread: (ask−bid)/mid ≤ 0.2% at the snapshot.
- price: tickSize/price ≤ 0.1%.
- volatility: 10-day realized vol in [min, max].
- rangeStability: |ROC 30d| ≤ cap.
- delist: venue status.

Run daily after the close. Write universe_snapshots(date, symbol, included, reasons_json), and give fleet bots and agents read-only access to today's universe. Backtests use the point-in-time snapshot when available (otherwise a flagged warning) to avoid survivorship bias. Endpoint: GET /api/universe?date=.

Sources: https://www.freqtrade.io/en/stable/plugins/#pairlists-and-pairlist-handlers, https://raw.githubusercontent.com/freqtrade/freqtrade/develop/docs/includes/pairlists.md

### Optional local MCP server so Claude Desktop/Code can drive Jarvis (Jesse MCP, Condor MCP tools)

This is not core to the owner's goals, but it is cheap leverage. The same typed tool gateway could be used from Claude Desktop on the owner's PC, while every action still passes our policy. It is lower priority than the in-app agent harness.

How: jarvis/mcp/stdio.js: a dependency-free JSON-RPC 2.0 server over stdin/stdout implementing initialize, tools/list and tools/call. It maps to existing gateway tools with read-only or paper-only scope: get_status, list_strategies, run_backtest (returns a jobId), get_report, run_significance, run_montecarlo, get_fleet, create_halt (create-only). There is no clear_halt and no limit edits. Every call is logged to ai_signals or tool_log. Ship a sample claude_desktop_config.json snippet in the docs.

Sources: https://github.com/jesse-ai/jesse, https://condor.hummingbot.org/llms-full.txt

### Avoid: copying GPL/LGPL/Commons-Clause code, Edge-style position sizing, LLM in the per-order path, exposed UI

These are legal and safety guardrails for an app that may be shared later. They also keep stops deterministic (hard rule), avoid deprecated sizing ideas, and stop a phone-reachable UI from becoming a remote-control hole. Evidence is strong: checked LICENSE files and official docs.

How: Add a docs/THIRD_PARTY_IDEAS.md listing which ideas came from which project and its license. Code ported from MIT or Apache sources carries attribution headers. Nothing is copied from GPL, LGPL or Commons-Clause repositories, and strategy logic is re-implemented from our own Python engine.

Stops and protections are evaluated by code on bar close or tick and never wait for Claude. Claude calls happen only in agent ticks and chat, with timeouts (e.g. 30s); a timeout means no action.

The HTTP server binds 127.0.0.1. The Twilio webhook is reached only through a signed tunnel, with X-Twilio-Signature verified on every request. Twilio SMS and voice commands are restricted to the owner's number. The same restrictions apply to Telegram, which is limited to owner_chat_id.

Sources: https://github.com/freqtrade/freqtrade/blob/develop/LICENSE, https://github.com/freqtrade/freqtrade-strategies/blob/main/LICENSE, https://github.com/iterativv/NostalgiaForInfinity/blob/main/LICENSE, https://github.com/mementum/backtrader/blob/master/LICENSE, https://github.com/Drakkar-Software/OctoBot/blob/master/LICENSE, https://github.com/nautechsystems/nautilus_trader/blob/develop/LICENSE, https://github.com/polakowo/vectorbt/blob/master/LICENSE.md, https://www.freqtrade.io/en/stable/deprecated/, https://condor.hummingbot.org/motivation, https://www.freqtrade.io/en/stable/rest-api/
