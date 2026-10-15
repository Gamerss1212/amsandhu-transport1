# jarvus_kit — starter code for the "code decides" side of Jarvus

Paper trading only. No broker keys, no order-placing code anywhere.

## Quick start (Windows or Mac/Linux)
```
pip install tzdata mcp
python test_kit.py                 # expect: Ran 42 tests ... OK
python make_demo_data.py           # makes demo/ files (FAKE paper trades)
python voice_loop_local.py --report demo/report.json --state-dir state --verified
```

## Files
| File | What it does |
|---|---|
| `policy.py` | BUY/WAIT decision (10 steps from the master prompt), HALT flag (create-only), pause files, audit log with a hash chain, auto-pause rules |
| `quality_gate.py` | Gate as a program: trades, days, Deflated Sharpe, 2x costs, folds, sign-flip. Closed until PBO, placebo, buy-and-hold are supplied |
| `voice_router.py` | Fixed-list command matcher (no AI), spoken report builder, `execute()` |
| `tools.py` | Read-only tools + HALT/PAUSE |
| `mcp_server.py` | Exposes `tools.py` to AI apps as 5 MCP tools. No trading tools |
| `voice_loop_local.py` | Whole voice pipeline with the keyboard standing in for mic and speaker |
| `jarvus_reporter.py` | Daily report, optional Telegram text or Twilio call to YOUR number |
| `make_demo_data.py`, `demo/` | Fake data for trying things |
| `test_kit.py` | 42 tests |

## Using the decision engine
```python
from datetime import datetime
import policy
cfg = policy.Config(state_dir="state")
i = policy.Intent(bot="A200", symbol="BTC", market="crypto", now=datetime.now(policy.MT), ...)
d = policy.decide(i, policy.State(), cfg, policy.AuditLog(cfg.audit_file))
print(d.bullets())      # "- WAIT / - Real money is off and the bot has not passed"
```
Fill `cfg.holidays` and `cfg.early_close_dates` from the NYSE calendar. They are empty by default.

## Using the gate
```python
import quality_gate
trades = [{"date": "2026-01-05", "r_net": 0.4, "cost_r": 0.12}, ...]   # pooled by role
print(quality_gate.run_gate(trades, n_trials=22).table())
```
Keep `n_trials` honest: every strategy variant you ever tried on that data counts.

## Known gaps (read these)
- HALF_SIZE and PAUSE_NEW_ENTRIES actions are returned by `auto_pause_actions` but nothing applies them yet. Your order-sizing code must.
- The HALT file path must be the same for every program: set `JARVUS_HALT_FILE` for `jarvus_reporter.py` to `<state_dir>/HALT`.
- `Intent` fields (stop %, cost %, volatility state, gate verdict) must come from your data service and gate. They are not computed here.
- No speech, phone or wake-word code. Use Pipecat or LiveKit for that and keep `voice_router.py` in the middle.
