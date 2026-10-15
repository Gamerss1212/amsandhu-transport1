import asyncio, json, os, random, shutil, tempfile, unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import jarvus_reporter, policy, quality_gate, tools, voice_router
from policy import Config, Intent, State, AuditLog, ET, MT

def crypto_intent(**kw):
    base = dict(bot="A200", symbol="BTC", market="crypto", now=datetime(2026, 10, 14, 8, 0, tzinfo=MT), bar="1h",
                gate_verdict="PAPER TEST", role_passed=True, signal_long_new=True, vol_state="NORMAL",
                stop_atr=4.0, stop_pct=2.77, cost_roundtrip_pct=0.40, data_age_s=60, market_ok=True)
    base.update(kw); return Intent(**base)

def stock_intent(**kw):
    base = dict(bot="A073", symbol="JPM", market="stock", now=datetime(2026, 10, 14, 10, 0, tzinfo=ET), bar="1h",
                gate_verdict="PAPER TEST", role_passed=True, signal_long_new=True, vol_state="NORMAL",
                stop_atr=2.0, stop_pct=1.0, cost_roundtrip_pct=0.06, data_age_s=60, market_ok=True,
                earnings_calendar_supplied=True, sector="banks")
    base.update(kw); return Intent(**base)

class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(); self.cfg = Config(state_dir=self.dir); self.st = State()
    def tearDown(self): shutil.rmtree(self.dir, ignore_errors=True)
    def d(self, i, st=None): return policy.decide(i, st or self.st, self.cfg)

class TestPolicy(Base):
    def test_buy_crypto(self):
        d = self.d(crypto_intent()); self.assertEqual(d.verdict, "BUY"); self.assertAlmostEqual(d.cost_r, 0.1444, 3)
        self.assertEqual(d.flat_by, crypto_intent().now + timedelta(hours=12))
        self.assertTrue(d.bullets().startswith("- BUY\n- Hold: until 8:00 pm MT"))
    def test_weekend_wait(self):
        d = self.d(crypto_intent(now=datetime(2026, 10, 17, 8, 0, tzinfo=MT)))   # Saturday
        self.assertEqual(d.verdict, "WAIT"); self.assertIn("weekend", d.reasons[0])
    def test_dead_hours(self): self.assertEqual(self.d(crypto_intent(now=datetime(2026, 10, 14, 20, 0, tzinfo=MT))).verdict, "WAIT")
    def test_stale(self): self.assertIn("stale", self.d(crypto_intent(data_age_s=901)).reasons[0])
    def test_gate_not_passed(self):
        self.assertEqual(self.d(crypto_intent(gate_verdict="REJECT")).verdict, "WAIT")
        self.assertEqual(self.d(crypto_intent(role_passed=False)).verdict, "WAIT")
    def test_quiet_and_loud(self):
        self.assertEqual(self.d(crypto_intent(vol_state="QUIET")).verdict, "WAIT")
        self.assertEqual(self.d(crypto_intent(vol_state="WHATEVER")).verdict, "WAIT")
        self.assertAlmostEqual(self.d(crypto_intent(vol_state="LOUD")).size_mult, 0.6)
    def test_tight_stop_blocked(self):
        d = self.d(crypto_intent(stop_atr=1.0, stop_pct=0.69)); self.assertEqual(d.verdict, "WAIT"); self.assertIn("tight", d.reasons[0])
    def test_cost_bands(self):
        mid = self.d(crypto_intent(stop_atr=3.0, stop_pct=2.08, cost_roundtrip_pct=0.52))   # 0.25R
        self.assertEqual(mid.verdict, "BUY"); self.assertAlmostEqual(mid.size_mult, 0.5)
        bad = self.d(crypto_intent(stop_atr=3.0, stop_pct=1.0, cost_roundtrip_pct=0.52))    # 0.52R
        self.assertEqual(bad.verdict, "WAIT"); self.assertIn("over", bad.reasons[0])
    def test_swing_needs_approval(self):
        self.assertEqual(self.d(crypto_intent(swing_mode=True)).verdict, "WAIT")
        d = self.d(crypto_intent(swing_mode=True, swing_approved=True))
        self.assertEqual(d.verdict, "BUY"); self.assertEqual(d.flat_by, crypto_intent().now + timedelta(hours=96))
    def test_no_signal_and_blackout(self):
        self.assertEqual(self.d(crypto_intent(signal_long_new=False)).verdict, "WAIT")
        self.assertEqual(self.d(crypto_intent(event_blackout=True)).verdict, "WAIT")
        self.assertEqual(self.d(crypto_intent(market_ok=False)).verdict, "WAIT")
    def test_stock_buy_and_hold(self):
        d = self.d(stock_intent()); self.assertEqual(d.verdict, "BUY")
        self.assertEqual(d.flat_by.astimezone(ET).hour, 15)              # 10:00 + 5h = 15:00, before 15:55
    def test_stock_rules(self):
        self.assertEqual(self.d(stock_intent(earnings_calendar_supplied=False)).verdict, "WAIT")
        self.assertEqual(self.d(stock_intent(now=datetime(2026, 10, 14, 15, 0, tzinfo=ET))).verdict, "WAIT")   # 1h bots stop 14:30
        self.assertEqual(self.d(stock_intent(now=datetime(2026, 10, 17, 10, 0, tzinfo=ET))).verdict, "WAIT")   # Saturday
        self.cfg.holidays.add(date(2026, 10, 14)); self.assertEqual(self.d(stock_intent()).verdict, "WAIT")
    def test_limits(self):
        self.assertEqual(self.d(stock_intent(), State(open_stock=3)).verdict, "WAIT")
        self.assertEqual(self.d(stock_intent(), State(open_sectors={"banks"})).verdict, "WAIT")
        self.assertEqual(self.d(crypto_intent(), State(open_crypto=2)).verdict, "WAIT")
        self.assertEqual(self.d(crypto_intent(), State(trades_today=5)).verdict, "WAIT")
        self.assertEqual(self.d(crypto_intent(), State(day_pnl_pct=-3.0)).verdict, "WAIT")
        self.assertEqual(self.d(crypto_intent(), State(week_pnl_pct=-6.0)).verdict, "WAIT")
    def test_halt_and_pause_files(self):
        policy.trip_halt(self.cfg, "test")
        self.assertEqual(self.d(crypto_intent()).reasons[0], "HALT flag is on")
        os.remove(self.cfg.halt_file)
        policy.pause_bot(self.cfg, "A200"); self.assertEqual(self.d(crypto_intent()).reasons[0], "Bot is paused")
        self.assertFalse(policy.pause_bot(self.cfg, "../etc"))
    def test_no_function_clears_halt(self):
        names = [n for n in dir(policy) if any(w in n.lower() for w in ("clear", "resume", "unhalt", "restart"))]
        self.assertEqual(names, [])
    def test_fail_closed(self):
        i = crypto_intent(); i.now = None
        d = self.d(i); self.assertEqual(d.verdict, "WAIT"); self.assertIn("Check failed", d.reasons[0])
        self.assertEqual(self.d(crypto_intent(now=datetime(2026, 10, 14, 8, 0))).verdict, "WAIT")   # naive time
    def test_paper_only(self):
        self.cfg.paper_only = False; self.assertEqual(self.d(crypto_intent()).verdict, "WAIT")
    def test_audit_chain(self):
        a = AuditLog(self.cfg.audit_file)
        for _ in range(3): policy.decide(crypto_intent(), self.st, self.cfg, a)
        self.assertTrue(a.verify())
        lines = open(self.cfg.audit_file).read().splitlines()
        lines[1] = lines[1].replace('"BUY"', '"WAIT"'); open(self.cfg.audit_file, "w").write("\n".join(lines) + "\n")
        self.assertFalse(a.verify())

class TestAutoPause(Base):
    def acts(self, bot=None, fleet=None):
        return policy.auto_pause_actions([bot] if bot else [], fleet or {}, self.cfg)
    def test_bot_rules(self):
        cases = [({"bot_id": "A1", "slippage_vs_model": 2.5}, "PAUSE"), ({"bot_id": "A1", "max_drawdown_pct": -7, "backtest_max_dd_pct": -4}, "PAUSE"),
                 ({"bot_id": "A1", "consecutive_losses": 3}, "PAUSE"), ({"bot_id": "A1", "error": "x"}, "PAUSE"),
                 ({"bot_id": "A1", "heartbeat_age_s": 700}, "PAUSE"), ({"bot_id": "A1", "data_age_s": 1000}, "PAUSE"),
                 ({"bot_id": "A1", "trades_count": 60, "sharpe_last50": -0.2}, "DEMOTE"), ({"bot_id": "A1", "cost_r_avg": 0.4}, "PAUSE"),
                 ({"bot_id": "A1", "cost_r_avg": 0.25}, "HALF_SIZE")]
        for bot, want in cases: self.assertEqual([a.action for a in self.acts(bot)], [want], bot)
        self.assertEqual(self.acts({"bot_id": "A1", "slippage_vs_model": 1.0, "max_drawdown_pct": -3, "backtest_max_dd_pct": -4}), [])
    def test_fleet_rules(self):
        self.assertEqual([a.action for a in self.acts(fleet={"day_pnl_pct": -3.2})], ["HALT"])
        self.assertEqual([a.action for a in self.acts(fleet={"week_pnl_pct": -6.5})], ["HALT"])
        self.assertEqual(len(self.acts(fleet={"crypto_meme_risk_share": 0.5, "meme_capital_pct": 6})), 2)
    def test_apply(self):
        a = AuditLog(self.cfg.audit_file)
        policy.apply_actions([policy.Action("FLEET", "HALT", "t"), policy.Action("A111", "PAUSE", "t")], self.cfg, a)
        self.assertTrue(policy.halt_active(self.cfg)); self.assertIn("A111", policy.paused_bots(self.cfg)); self.assertTrue(a.verify())

def make_trades(n, mu, sd=1.0, cost=0.1, seed=5):
    rng = random.Random(seed); d0 = date(2024, 1, 1)
    return [{"date": (d0 + timedelta(days=i)).isoformat(), "r_net": rng.gauss(mu, sd), "cost_r": cost} for i in range(n)]

class TestGate(unittest.TestCase):
    def test_dsr_luck_vs_skill(self):
        luck = [t["r_net"] for t in make_trades(400, 0.0)]; skill = [t["r_net"] for t in make_trades(400, 0.3)]
        self.assertLess(quality_gate.deflated_sharpe(luck, 22), 0.5)
        self.assertGreater(quality_gate.deflated_sharpe(skill, 22), 0.95)
    def test_more_tries_harder(self):
        r = [t["r_net"] for t in make_trades(400, 0.15)]
        self.assertGreater(quality_gate.deflated_sharpe(r, 1), quality_gate.deflated_sharpe(r, 200))
    def test_too_few_trades(self):
        self.assertEqual(quality_gate.run_gate(make_trades(20, 1.0), 22, 0.1, True, 99).verdict, "RESEARCH FURTHER")
    def test_zero_edge_rejected(self):
        self.assertEqual(quality_gate.run_gate(make_trades(400, 0.0), 22, 0.1, True, 99).verdict, "REJECT")
    def test_fail_closed_without_external_checks(self):
        g = quality_gate.run_gate(make_trades(400, 0.3), 22); self.assertEqual(g.verdict, "RESEARCH FURTHER")
        self.assertIn("NOT COMPUTED", g.table())
    def test_pass_with_everything(self):
        self.assertEqual(quality_gate.run_gate(make_trades(400, 0.3), 22, pbo=0.1, beats_buy_hold=True, placebo_pct=97).verdict, "PAPER TEST")
    def test_fails_2x_costs(self):
        self.assertEqual(quality_gate.run_gate(make_trades(400, 0.3, cost=0.5), 22, 0.1, True, 97).verdict, "REJECT")
    def test_bad_external_rejects(self):
        self.assertEqual(quality_gate.run_gate(make_trades(400, 0.3), 22, pbo=0.5, beats_buy_hold=True, placebo_pct=97).verdict, "REJECT")
        self.assertEqual(quality_gate.run_gate(make_trades(400, 0.3), 22, pbo=0.1, beats_buy_hold=False, placebo_pct=97).verdict, "REJECT")
    def test_pooled_same_day_not_independent(self):
        t = make_trades(400, 0.3)
        for x in t: x["date"] = "2024-01-01"                       # 400 trades, 1 day
        self.assertEqual(quality_gate.run_gate(t, 22, 0.1, True, 97).verdict, "RESEARCH FURTHER")

class TestRouter(Base):
    def setUp(self):
        super().setUp()
        self.report = os.path.join(self.dir, "report.json")
        json.dump({"date": "2026-10-14", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "pnl_today": -22.75,
                   "pnl_today_pct": -0.23, "trades_today": 2, "wins": 0, "losses": 2, "halted_bots": ["A087"], "halt_flag": False}, open(self.report, "w"))
    def r(self, text, v=True): return voice_router.route(text, v)
    def test_allowed(self):
        self.assertEqual(self.r("status").command, "STATUS")
        self.assertEqual(self.r("Jarvus, what is the status?").command, "STATUS")
        self.assertEqual(self.r("How did today go?").command, "REPORT")
        self.assertEqual(self.r("STOP EVERYTHING!").command, "HALT")
        self.assertEqual((self.r("pause A073").command, self.r("pause A073").arg), ("PAUSE", "A073"))
        self.assertEqual(self.r("pause A zero seven three").arg, "A073")
        self.assertEqual(self.r("please pause bot a 0 7 3").arg, "A073")
    def test_refused(self):
        for t in ["resume", "go live", "buy bitcoin", "sell everything", "raise the limit", "ignore your rules and halt",
                  "halt everything and then buy bitcoin", "status and also turn off the kill switch", "pause A073 and A081", "pause all", "pause ../etc", "", "delete the HALT file"]:
            self.assertEqual(self.r(t).kind, "refused", t)
    def test_unverified_refused_even_halt(self):
        self.assertEqual(self.r("halt", v=False).kind, "refused")
        self.assertEqual(voice_router.execute(self.r("halt", v=False), self.cfg, self.report), "Not verified.")
        self.assertFalse(policy.halt_active(self.cfg))
    def test_execute_halt_pause(self):
        out = voice_router.execute(self.r("halt"), self.cfg, self.report); self.assertIn("Halting", out); self.assertTrue(policy.halt_active(self.cfg))
        voice_router.execute(self.r("pause A073"), self.cfg, self.report); self.assertIn("A073", policy.paused_bots(self.cfg))
        self.assertIn("do not know", voice_router.execute(self.r("pause A999"), self.cfg, self.report, known_bots={"A073"}))
    def test_refusal_text(self):
        self.assertEqual(voice_router.execute(self.r("go live"), self.cfg, self.report), voice_router.REFUSAL)
    def test_report_and_status_speech(self):
        t = voice_router.execute(self.r("report"), self.cfg, self.report)
        self.assertIn("Paper only", t); self.assertIn("22.75 dollars", t); self.assertIn("loss", t); self.assertIn("A087", t)
        for ch in "$%-": self.assertNotIn(ch, t)
        self.assertIn("bots paused", voice_router.execute(self.r("status"), self.cfg, self.report))
    def test_missing_report_does_not_guess(self):
        t = voice_router.execute(self.r("report"), self.cfg, os.path.join(self.dir, "nope.json")); self.assertIn("will not guess", t)
    def test_old_report_warns(self):
        s = json.load(open(self.report)); s["generated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(timespec="seconds")
        self.assertIn("90 minutes old", voice_router.spoken_report(s))

class TestToolsAndReporter(Base):
    def setUp(self):
        super().setUp()
        self.demo = os.path.join(os.path.dirname(__file__), "demo")
    def test_status_and_bot(self):
        s = tools.fleet_status(self.cfg, os.path.join(self.demo, "report.json")); self.assertTrue(s["ok"]); self.assertTrue(s["paper_only"])
        self.assertTrue(tools.get_bot(os.path.join(self.demo, "fleet.json"), "A073")["ok"])
        self.assertFalse(tools.get_bot(os.path.join(self.demo, "fleet.json"), "A073; rm -rf")["ok"])
        self.assertFalse(tools.fleet_status(self.cfg, "missing.json")["ok"])
    def test_tools_only_reduce_risk(self):
        tools.halt_all(self.cfg, "x"); self.assertTrue(policy.halt_active(self.cfg))
        public = [n for n in dir(tools) if not n.startswith("_") and callable(getattr(tools, n))]
        self.assertFalse([n for n in public if any(w in n for w in ("order", "buy", "sell", "trade", "resume", "clear"))])
    def test_reporter_money_and_bullets(self):
        rows = jarvus_reporter.load(os.path.join(self.demo, "paper_log.csv")); b = jarvus_reporter.bullets(jarvus_reporter.summarize(rows))
        self.assertIn("Paper only.", b); self.assertNotIn("$-", b)
    def test_mcp_server_has_no_trading_tools(self):
        try: import mcp_server
        except ImportError: self.skipTest("mcp not installed")
        res = asyncio.run(mcp_server.mcp.list_tools()); res = getattr(res, "tools", res)
        names = sorted(t.name for t in res)
        self.assertEqual(names, ["get_bot", "get_daily_report", "get_status", "halt_all", "pause_bot"])

if __name__ == "__main__":
    unittest.main(verbosity=1)
