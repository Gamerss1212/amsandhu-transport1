"""tests/test_jarvis.py - stdlib unittest suite for Jarvis Core (jarvis/): gateway, core, brain, alerts, phone,
memory and tasks.

Run from the project folder:
    python3 -m unittest tests.test_jarvis -v
    python3 tests/test_jarvis.py

Rules these tests follow:
  - NO network. The app runs with DataService mode='demo' (simulated, labelled DEMO). A module-level guard makes
    any accidental HTTP call raise. The AI brain is a FakeBrain (scripted) or an AnthropicBrain with a fake client.
  - All state goes to temporary folders. Nothing is written to the project's state/ folder.
  - Twilio is never contacted: outbound calls use an injected sender, and env vars are faked per test.
  - Source files are never modified. Tests that expose a real source bug are kept, marked
"""
from __future__ import annotations
import base64, hashlib, hmac, json, os, re, shutil, sys, tempfile, threading, time, unittest, warnings
from datetime import datetime, timedelta
from urllib.parse import urlparse
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import engine                                   # noqa: E402  (adds kit/ to sys.path)
import jarvis                                   # noqa: E402
from engine import library                      # noqa: E402
from jarvis import alerts as alerts_mod         # noqa: E402
from jarvis import brain as brain_mod           # noqa: E402
from jarvis import core as core_mod             # noqa: E402
from jarvis import gateway as gw_mod            # noqa: E402
from jarvis.alerts import AlertEngine, describe, validate as validate_rule   # noqa: E402
from jarvis.app import App, DEFAULTS            # noqa: E402
from jarvis.brain import FALLBACK_BETA, AnthropicBrain, BrainError, FakeBrain, route_for   # noqa: E402
from jarvis.core import entities, speakable     # noqa: E402
from jarvis.gateway import LEVEL_TIERS, obj, validate as validate_schema, S, I, B   # noqa: E402
from jarvis.memory import Store                 # noqa: E402
from jarvis.phone import (GENERIC_CALL, InboundCalls, OutboundCallManager, PinAuth,  # noqa: E402
                          twilio_signature_ok)
from jarvis.tasks import TaskManager            # noqa: E402
import policy                                   # noqa: E402  (kit/)

MT = ZoneInfo("America/Edmonton")
# tools that MUST be T2 (desktop-only owner_operate). The live T2 set is read from the registry (t2_names()).
T2_TOOLS = {"run_backtest", "run_role_test", "create_alert_rule", "disable_alert_rule", "remember", "forget_memory",
            "open_panel", "start_research", "build_website", "open_website"}
RR_TOOLS = {"halt_fleet", "pause_agent"}
CHANNELS = ("gui", "desktop_voice", "telephone")
LEVELS = ("anonymous", "owner_read", "owner_operate")
TWILIO_ENV = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "JARVUS_MY_NUMBER", "JARVIS_PHONE_INBOUND")
FAKE_TWILIO = {"TWILIO_ACCOUNT_SID": "ACtest000", "TWILIO_AUTH_TOKEN": "tok_test", "TWILIO_FROM_NUMBER": "+15875550100",
               "JARVUS_MY_NUMBER": "+15875550199"}


# ---------------------------------------------------------------- network guard + clean environment
_PATCHES: list = []


def _no_network(*a, **k):
    raise RuntimeError("tests must not use the network")


def setUpModule():
    warnings.filterwarnings("ignore", category=ResourceWarning)     # source opens json files without closing them
    for target in ("urllib.request.urlopen", "engine.data_feed._get_json"):
        p = mock.patch(target, _no_network)
        p.start()
        _PATCHES.append(p)
    env = mock.patch.dict(os.environ, {"JARVIS_DATA_MODE": "demo"})
    env.start()
    _PATCHES.append(env)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN") + TWILIO_ENV:
        os.environ.pop(k, None)                    # restored by the patch.dict above at teardown


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


# ---------------------------------------------------------------- helpers
def tmpdir(tc: unittest.TestCase) -> str:
    d = tempfile.mkdtemp(prefix="jarvis_core_test_")
    tc.addCleanup(shutil.rmtree, d, True)
    return d


def make_store(tc) -> Store:
    return Store(os.path.join(tmpdir(tc), "jarvis.db"))


def make_app(tc, brain=None, **cfg_over) -> App:
    d = tmpdir(tc)
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["data_mode"] = "demo"
    cfg["watchlist"] = ["BTC-USD", "ETH-USD", "AAPL"]
    cfg.update(cfg_over)
    app = App(state_dir=d, cfg=cfg, brain=brain if brain is not None else FakeBrain([]), start_threads=False)
    tc.addCleanup(lambda: app.tasks.pool.shutdown(wait=True, cancel_futures=True))
    tc.addCleanup(app.stop)
    assert os.path.realpath(app.fleet.cfg.state_dir) == os.path.realpath(d)
    return app


def t2_names(app) -> set:
    return {n for n, t in app.gateway.tools.items() if t["tier"] == "T2"}


def read_json(path):
    with open(path) as f:
        return json.load(f)


def read_text(path):
    with open(path) as f:
        return f.read()


def wait_for(pred, timeout: float = 10.0, step: float = 0.02):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


def text_resp(text, stop="end_turn", citations=None):
    b = {"type": "text", "text": text}
    if citations is not None:
        b["citations"] = citations
    return {"content": [b], "stop_reason": stop}


def tool_resp(name, args, tid="toolu_01"):
    return {"content": [{"type": "text", "text": "Checking."},
                        {"type": "tool_use", "id": tid, "name": name, "input": args}], "stop_reason": "tool_use"}


def sess(level="owner_operate", channel="gui", sid="test-session"):
    return {"id": sid, "channel": channel, "auth_level": level}


def tool_results(messages) -> list:
    """All tool_result blocks the core sent back to the model."""
    out = []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            out += [b for b in m["content"] if b.get("type") == "tool_result"]
    return out


def local_ts(y, mo, d, h, mi, s=0) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=MT).timestamp()


# ================================================================ gateway
class TestGatewaySchema(unittest.TestCase):
    def test_validate_strict_schema(self):
        sch = obj({"sym": S(pattern=r"^[A-Z]{1,5}$"), "n": I(minimum=1, maximum=5), "flag": B,
                   "kind": S(enum=["a", "b"]), "note": S(maxLength=4)}, ["sym"])
        self.assertIsNone(validate_schema(sch, {"sym": "BTC", "n": 3, "flag": True, "kind": "a", "note": "ok"}))
        self.assertIn("unexpected", validate_schema(sch, {"sym": "BTC", "extra": 1}))
        self.assertIn("missing", validate_schema(sch, {}))
        self.assertIn("bad format", validate_schema(sch, {"sym": "btc"}))
        self.assertIn("integer", validate_schema(sch, {"sym": "BTC", "n": True}))      # bool is not an int here
        self.assertIn("integer", validate_schema(sch, {"sym": "BTC", "n": "3"}))
        self.assertIn("out of range", validate_schema(sch, {"sym": "BTC", "n": 6}))
        self.assertIn("out of range", validate_schema(sch, {"sym": "BTC", "n": 0}))
        self.assertIn("true/false", validate_schema(sch, {"sym": "BTC", "flag": "yes"}))
        self.assertIn("one of", validate_schema(sch, {"sym": "BTC", "kind": "c"}))
        self.assertIn("too long", validate_schema(sch, {"sym": "BTC", "note": "toolong"}))
        self.assertIn("object", validate_schema(sch, ["BTC"]))

    def test_level_tiers(self):
        self.assertEqual(LEVEL_TIERS["anonymous"], {"T0"})
        self.assertNotIn("T2", LEVEL_TIERS["owner_read"])
        self.assertIn("RR", LEVEL_TIERS["owner_read"])
        self.assertIn("T2", LEVEL_TIERS["owner_operate"])


class TestGateway(unittest.TestCase):
    def setUp(self):
        self.app = make_app(self)
        self.g = self.app.gateway

    def test_unknown_tool(self):
        r = self.g.call("place_order", {"symbol": "BTC-USD"}, sess())
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "UNKNOWN_TOOL")

    def test_extra_argument_rejected(self):
        r = self.g.call("get_time", {"tz": "UTC"}, sess())
        self.assertEqual(r["error"]["code"], "BAD_ARGUMENTS")
        r = self.g.call("get_quote", {"symbol": "BTC-USD", "size": 5}, sess())
        self.assertEqual(r["error"]["code"], "BAD_ARGUMENTS")

    def test_bad_bot_pattern_rejected(self):
        for tool in ("get_agent", "pause_agent"):
            with self.subTest(tool=tool):
                r = self.g.call(tool, {"bot_id": "X1"}, sess())
                self.assertEqual(r["error"]["code"], "BAD_ARGUMENTS")
        self.assertEqual(policy.paused_bots(self.app.fleet.cfg), set())

    def test_missing_required_and_bad_symbol(self):
        self.assertEqual(self.g.call("get_quote", {}, sess())["error"]["code"], "BAD_ARGUMENTS")
        self.assertEqual(self.g.call("get_quote", {"symbol": "btc usd; rm -rf"}, sess())["error"]["code"], "BAD_ARGUMENTS")

    def test_anonymous_only_t0(self):
        for ch in CHANNELS:
            listed = self.g.for_session("anonymous", ch)
            self.assertTrue(listed)
            for t in listed:
                self.assertEqual(self.g.tools[t["name"]]["tier"], "T0", t["name"])
        self.assertTrue(self.g.call("get_time", {}, sess("anonymous", "telephone"))["ok"])
        for name, t in self.g.tools.items():
            if t["tier"] == "T0":
                continue
            with self.subTest(tool=name):
                r = self.g.call(name, {}, sess("anonymous", "gui"))
                self.assertEqual(r["error"]["code"], "UNAUTHORIZED")
        self.assertFalse(policy.halt_active(self.app.fleet.cfg))

    def test_unknown_level_gets_nothing(self):
        self.assertEqual(self.g.for_session("root", "gui"), [])
        self.assertEqual(self.g.call("get_time", {}, sess("root"))["error"]["code"], "UNAUTHORIZED")
        r = self.g.call("get_portfolio", {}, {"id": "x", "channel": "gui"})      # no level -> anonymous
        self.assertEqual(r["error"]["code"], "UNAUTHORIZED")

    def test_owner_read_phone_cannot_call_t2(self):
        s = sess("owner_read", "telephone")
        calls = {"run_backtest": {"strategy_id": "x", "symbol": "BTC-USD"}, "remember": {"text": "my pin is 1234"},
                 "open_panel": {"panel": "chart"}, "create_alert_rule": {"condition": {"kind": "any_signal"}},
                 "forget_memory": {"memory_id": "abc"}, "disable_alert_rule": {"rule_id": "abc"},
                 "run_role_test": {"role": "x"}}
        for name, args in calls.items():
            with self.subTest(tool=name):
                self.assertEqual(self.g.call(name, args, s)["error"]["code"], "UNAUTHORIZED")
                # owner_read on the desktop is still not owner_operate
                self.assertEqual(self.g.call(name, args, sess("owner_read", "gui"))["error"]["code"], "UNAUTHORIZED")
        self.assertEqual(self.app.store.memories(), [])
        self.assertEqual(self.app.tasks.list(), [])
        listed = {t["name"] for t in self.g.for_session("owner_read", "telephone")}
        self.assertFalse(listed & t2_names(self.app))
        self.assertTrue(RR_TOOLS <= listed)

    def test_owner_read_phone_can_halt_and_pause(self):
        s = sess("owner_read", "telephone")
        r = self.g.call("pause_agent", {"bot_id": "A073"}, s)
        self.assertTrue(r["ok"], r)
        self.assertIn("A073", policy.paused_bots(self.app.fleet.cfg))
        r = self.g.call("halt_fleet", {"reason": "caller asked"}, s)
        self.assertTrue(r["ok"], r)
        self.assertTrue(policy.halt_active(self.app.fleet.cfg))
        self.assertIn("telephone", read_text(self.app.fleet.cfg.halt_file))
        crit = [n for n in self.app.store.notifications() if n["title"] == "HALT"]
        self.assertEqual(crit[0]["severity"], "critical")

    def test_pause_unknown_bot_not_found(self):
        r = self.g.call("pause_agent", {"bot_id": "A999"}, sess())
        self.assertEqual(r["error"]["code"], "NOT_FOUND")
        self.assertEqual(policy.paused_bots(self.app.fleet.cfg), set())

    def test_telephone_channel_denies_t2_even_with_forged_level(self):
        s = sess("owner_operate", "telephone")          # forged: phone should never be owner_operate
        for name, args in {"run_backtest": {"strategy_id": "x", "symbol": "BTC-USD"}, "remember": {"text": "x"},
                           "open_panel": {"panel": "chart"}, "create_alert_rule": {"condition": {"kind": "any_signal"}}}.items():
            with self.subTest(tool=name):
                self.assertEqual(self.g.call(name, args, s)["error"]["code"], "CHANNEL_DENIED")
        listed = {t["name"] for t in self.g.for_session("owner_operate", "telephone")}
        self.assertFalse(listed & t2_names(self.app))
        self.assertEqual(self.app.store.memories(), [])
        self.assertEqual(list(self.app.ui_events), [])

    def test_owner_operate_desktop_can_use_t2(self):
        r = self.g.call("open_panel", {"panel": "chart", "symbol": "BTC-USD"}, sess())
        self.assertTrue(r["ok"])
        self.assertEqual(self.app.ui_events[-1]["panel"], "chart")
        r = self.g.call("remember", {"text": "Prefer short answers", "kind": "preference"}, sess("owner_operate", "desktop_voice"))
        self.assertTrue(r["ok"])
        mid = r["data"]["memory_id"]
        mems = self.app.store.memories()
        self.assertEqual(mems[0]["text"], "Prefer short answers")
        self.assertEqual(mems[0]["source"], "owner via desktop_voice")
        self.assertTrue(self.g.call("forget_memory", {"memory_id": mid}, sess())["ok"])
        self.assertEqual(self.app.store.memories(), [])

    def test_create_alert_rule_tool_readback_and_validation(self):
        r = self.g.call("create_alert_rule", {"condition": {"kind": "metric_threshold", "metric": "price:BTC-USD",
                                                            "operator": ">", "threshold": "65000"}, "label": "BTC"}, sess())
        self.assertTrue(r["ok"], r)
        self.assertIn("price of BTC-USD > 65000", r["data"]["readback"])
        self.assertIn("when it crosses", r["data"]["readback"])
        rules = self.g.call("list_alert_rules", {}, sess("owner_read"))["data"]["rules"]
        self.assertEqual([x["id"] for x in rules], [r["data"]["rule_id"]])
        bad = self.g.call("create_alert_rule", {"condition": {"kind": "free_form", "expr": "price>1"}}, sess())
        self.assertEqual(bad["error"]["code"], "BAD_ARGUMENTS")
        bad = self.g.call("create_alert_rule", {"condition": {"kind": "any_signal"}, "channels": ["sms"]}, sess())
        self.assertEqual(bad["error"]["code"], "BAD_ARGUMENTS")
        self.assertTrue(self.g.call("disable_alert_rule", {"rule_id": r["data"]["rule_id"]}, sess())["ok"])
        self.assertFalse(self.app.alerts.list()[0]["enabled"])

    def test_run_backtest_wiring_and_unknown_strategy(self):
        r = self.g.call("run_backtest", {"strategy_id": "no.such.strategy", "symbol": "BTC-USD"}, sess())
        self.assertEqual(r["error"]["code"], "NOT_FOUND")
        sid = library.load()["keep"][0]["id"]
        with mock.patch.object(self.app, "start_backtest", return_value="task123") as sb:
            r = self.g.call("run_backtest", {"strategy_id": sid, "symbol": "BTC-USD", "bar": "1h"}, sess(sid="S1"))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["task_id"], "task123")
        sb.assert_called_once_with(sid, "BTC-USD", "1h", None, True, "S1")
        self.assertEqual(self.g.call("run_role_test", {"role": "not a role"}, sess())["error"]["code"], "NOT_FOUND")

    def test_t2_tools_are_desktop_only(self):
        t2 = t2_names(self.app)
        self.assertTrue(T2_TOOLS <= t2, T2_TOOLS - t2)
        for name in t2:
            self.assertEqual(self.g.tools[name]["channels"], {"gui", "desktop_voice"}, name)
        for name in RR_TOOLS:
            self.assertEqual(self.g.tools[name]["tier"], "RR")
            self.assertIn("telephone", self.g.tools[name]["channels"])

    def test_for_session_never_lists_order_tools(self):
        bad = re.compile(r"order|buy|sell", re.I)
        for name in self.g.tools:
            self.assertIsNone(bad.search(name), name)
        for lvl in LEVELS + ("root", ""):
            for ch in CHANNELS + ("sms",):
                for t in self.g.for_session(lvl, ch):
                    self.assertIsNone(bad.search(t["name"]), (lvl, ch, t["name"]))
                    tier = self.g.tools[t["name"]]["tier"]
                    self.assertIn(tier, LEVEL_TIERS.get(lvl, set()))
                    self.assertIn(ch, self.g.tools[t["name"]]["channels"])
                    self.assertEqual(set(t), {"name", "description", "input_schema"})   # no fn / tier leaked

    def test_every_call_is_audited(self):
        n0 = len(self.app.store.tool_audit(500))
        calls = [("get_time", {}, sess(), True), ("no_such_tool", {}, sess(), False),
                 ("get_time", {"x": 1}, sess(), False), ("get_portfolio", {}, sess("anonymous"), False),
                 ("remember", {"text": "x"}, sess("owner_operate", "telephone"), False),
                 ("get_portfolio", {}, sess("owner_read", "telephone", sid="phone-1"), True)]
        for name, args, s, _ in calls:
            self.g.call(name, args, s)
        rows = list(reversed(self.app.store.tool_audit(500)))[n0:]
        self.assertEqual(len(rows), len(calls))
        for row, (name, args, s, ok) in zip(rows, calls):
            self.assertEqual(row["tool"], name)
            self.assertEqual(bool(row["ok"]), ok, name)
            self.assertEqual(row["channel"], s["channel"])
            if not ok:
                self.assertTrue(row["error"])
        self.assertEqual(rows[1]["tier"], "?")
        self.assertIn("UNKNOWN_TOOL", rows[1]["error"])
        self.assertIn("BAD_ARGUMENTS", rows[2]["error"])
        self.assertIn("UNAUTHORIZED", rows[3]["error"])
        self.assertIn("CHANNEL_DENIED", rows[4]["error"])
        self.assertEqual(rows[5]["session_id"], "phone-1")

    def test_tool_exception_is_unavailable_and_audited(self):
        with mock.patch.object(self.app.fleet.paper, "summary", side_effect=RuntimeError("disk gone")):
            r = self.g.call("get_portfolio", {}, sess())
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "UNAVAILABLE")
        self.assertEqual(self.app.store.tool_audit(1)[0]["ok"], 0)

    def test_quote_never_invented(self):
        r = self.g.call("get_quote", {"symbol": "BTC-USD"}, sess("anonymous"))
        self.assertTrue(r["ok"])
        q = self.app.data.quote("BTC-USD", "1h")
        self.assertEqual(r["data"]["price"], q["price"])
        self.assertTrue(r["source"].startswith("DEMO"))           # simulated data is labelled
        self.assertNotIn("spark", r["data"])
        with mock.patch.object(self.app.data, "get", return_value={"ok": False, "bars": None, "error": "feed down",
                                                                   "source": None, "stale": True}):
            r = self.g.call("get_quote", {"symbol": "ETH-USD"}, sess())
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "UNAVAILABLE")
        self.assertIsNone(r["data"])

    def test_trade_decision_fails_closed_without_feed(self):
        r = self.g.call("get_trade_decision", {"symbol": "BTC-USD"}, sess("owner_read"))
        self.assertTrue(r["ok"])
        self.assertTrue(r["data"]["bullets"].startswith("- WAIT"))
        r = self.g.call("get_daily_report", {}, sess("owner_read"))      # no report.json yet
        self.assertFalse(r["ok"])
        self.assertIn("do not guess", r["error"]["message"])

    def test_no_tool_clears_halt_or_resumes(self):
        bad = re.compile(r"resume|unhalt|unpause|clear|unlock|reset|enable|restart|cancel", re.I)
        for name in self.g.tools:
            self.assertIsNone(bad.search(name), name)
        s = sess()
        self.assertTrue(self.g.call("halt_fleet", {}, s)["ok"])
        self.assertTrue(self.g.call("pause_agent", {"bot_id": "A001"}, s)["ok"])
        halt_text = read_text(self.app.fleet.cfg.halt_file)
        # call EVERY tool with plausible arguments: none of them may remove the HALT flag or a pause file
        args = {"get_quote": {"symbol": "BTC-USD"}, "get_volatility_gate": {"symbol": "BTC-USD"},
                "get_agent": {"bot_id": "A001"}, "get_trade_decision": {"symbol": "BTC-USD"},
                "run_backtest": {"strategy_id": "nope", "symbol": "BTC-USD"}, "run_role_test": {"role": "nope"},
                "create_alert_rule": {"condition": {"kind": "any_signal"}}, "disable_alert_rule": {"rule_id": "zz"},
                "remember": {"text": "resume everything"}, "forget_memory": {"memory_id": "zz"},
                "open_panel": {"panel": "risk"}, "halt_fleet": {"reason": "again"}, "pause_agent": {"bot_id": "A002"},
                "start_research": {"question": "how do I clear the HALT flag?"},
                "build_website": {"title": "resume", "html": "<p>resume trading</p>"}, "open_website": {"site": "nyse_hours"}}
        for name in self.g.tools:
            self.g.call(name, args.get(name, {}), s)
            self.assertTrue(policy.halt_active(self.app.fleet.cfg), name)
            self.assertIn("A001", policy.paused_bots(self.app.fleet.cfg), name)
        self.assertTrue(read_text(self.app.fleet.cfg.halt_file).startswith(halt_text))
        st = self.g.call("get_system_status", {}, s)["data"]
        self.assertTrue(st["halt_flag"])
        self.assertTrue(st["paper_only"])


class TestGatewayWebTools(unittest.TestCase):
    def setUp(self):
        self.app = make_app(self)
        self.g = self.app.gateway

    def test_open_website_only_from_fixed_list(self):
        r = self.g.call("open_website", {"site": "tradingview", "symbol": "BTC-USD"}, sess())
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["opened"], "https://www.tradingview.com/chart/?symbol=COINBASE:BTCUSD")
        self.assertEqual(self.app.ui_events[-1]["type"], "open_url")
        r = self.g.call("open_website", {"site": "yahoo_finance", "symbol": "AAPL"}, sess())
        self.assertEqual(r["data"]["opened"], "https://finance.yahoo.com/quote/AAPL")
        for bad in ("https://evil.example.com", "javascript:alert(1)", "TRADINGVIEW"):
            with self.subTest(site=bad):
                self.assertEqual(self.g.call("open_website", {"site": bad}, sess())["error"]["code"], "BAD_ARGUMENTS")
        self.assertEqual(self.g.call("open_website", {"site": "kraken", "symbol": "eth/../../x"}, sess())["error"]["code"],
                         "BAD_ARGUMENTS")
        self.assertEqual(self.g.call("open_website", {"site": "kraken", "url": "https://x"}, sess())["error"]["code"],
                         "BAD_ARGUMENTS")
        # every allowlisted site resolves to its own fixed https host
        for site, template in gw_mod.SITES.items():
            with self.subTest(site=site):
                url = self.g.call("open_website", {"site": site, "symbol": "SOL-USD"}, sess())["data"]["opened"]
                self.assertEqual(urlparse(url).scheme, "https")
                self.assertEqual(urlparse(url).netloc, urlparse(template).netloc)
        self.assertEqual(self.g.call("open_website", {"site": "ndax"}, sess("owner_operate", "telephone"))["error"]["code"],
                         "CHANNEL_DENIED")

    def test_build_website_saves_inside_sites_dir_only(self):
        r = self.g.call("build_website", {"title": "../../Etc/Passwd!!", "html": "<h1>Hi</h1>"}, sess())
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["saved"], "/sites/etc-passwd.html")
        self.assertEqual(os.listdir(self.app.sites_dir), ["etc-passwd.html"])
        self.assertEqual(read_text(os.path.join(self.app.sites_dir, "etc-passwd.html")), "<h1>Hi</h1>")
        r = self.g.call("build_website", {"title": "!!!", "html": "<p>x</p>"}, sess())
        self.assertEqual(r["data"]["saved"], "/sites/site.html")
        self.assertEqual(self.app.ui_events[-1]["url"], "/sites/site.html")
        self.assertEqual(self.g.call("build_website", {"title": "x", "html": "y"}, sess("owner_read"))["error"]["code"],
                         "UNAUTHORIZED")

    def test_research_needs_ready_brain(self):
        with mock.patch.object(self.app.brain, "ready", return_value=(False, "SETUP_REQUIRED")):
            r = self.g.call("start_research", {"question": "What is new in the FOMC calendar?"}, sess())
        self.assertEqual(r["error"]["code"], "SETUP_REQUIRED")
        self.assertEqual(self.app.tasks.list(), [])
        r = self.g.call("start_research", {"question": "x"}, sess("owner_read", "telephone"))
        self.assertEqual(r["error"]["code"], "UNAUTHORIZED")


class TestResearchJob(unittest.TestCase):
    def test_research_keeps_only_retrieved_citations(self):
        real, fake = "https://www.federalreserve.gov/fomc", "https://invented.example.net/fomc"
        script = [{"content": [{"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "fomc"}},
                               {"type": "web_search_tool_result", "tool_use_id": "s1",
                                "content": [{"type": "web_search_result", "url": real, "title": "FOMC"}]}],
                   "stop_reason": "pause_turn"},
                  {"content": [{"type": "text", "text": "The next meeting is Oct 28.",
                                "citations": [{"url": real, "title": "FOMC calendar"}, {"url": fake, "title": "Fake"}]}],
                   "stop_reason": "end_turn"}]
        brain = FakeBrain(script)
        app = make_app(self, brain=brain)
        r = app.gateway.call("start_research", {"question": "When is the next FOMC meeting?"}, sess(sid="R1"))
        self.assertTrue(r["ok"], r)
        tid = r["data"]["task_id"]
        self.assertTrue(wait_for(lambda: app.tasks.get(tid)["status"] in ("done", "failed")))
        t = app.tasks.get(tid)
        self.assertEqual(t["status"], "done", t.get("error"))
        res = t["result"]
        self.assertEqual(res["sources"], [{"url": real, "title": "FOMC calendar"}])
        self.assertEqual(res["sources_read"], 1)
        self.assertIn("Oct 28", res["report"])
        self.assertEqual(t["kind"], "research")
        self.assertEqual(t["session_id"], "R1")
        self.assertEqual([c["route"] for c in brain.calls], ["strong", "strong"])
        self.assertEqual(brain.calls[0]["tools"], [])                       # no app tools in the research worker
        self.assertEqual(brain.calls[1]["messages"][-1]["role"], "assistant")  # pause_turn continuation
        self.assertNotIn(fake, json.dumps(res))

    def test_result_urls_and_cited_sources(self):
        content = [{"type": "web_search_tool_result", "content": [{"url": "https://a.example/1"}, {"url": "https://a.example/2"}]},
                   {"type": "web_fetch_tool_result", "content": {"type": "web_fetch_result", "url": "https://b.example/"}},
                   {"type": "web_search_tool_result", "content": {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}},
                   {"type": "text", "text": "x", "citations": [{"url": "https://a.example/2", "title": "two"},
                                                               {"url": "https://nope.example/", "title": "made up"},
                                                               {"url": "https://b.example/"}]}]
        seen = core_mod.result_urls(content)
        self.assertEqual(seen, {"https://a.example/1", "https://a.example/2", "https://b.example/"})
        out = core_mod.cited_sources(content, seen, [])
        self.assertEqual(out, [{"url": "https://a.example/2", "title": "two"},
                               {"url": "https://b.example/", "title": "https://b.example/"}])
        self.assertEqual(core_mod.cited_sources(content, set(), []), [])


# ================================================================ brain routing / provider
class TestRouting(unittest.TestCase):
    def test_route_for(self):
        self.assertEqual(route_for("hi", {}), "fast")
        self.assertEqual(route_for("thanks!", {}), "fast")
        self.assertEqual(route_for("why is bitcoin down today?", {}), "strong")
        self.assertEqual(route_for("compare A073 and A081", {}), "strong")
        self.assertEqual(route_for("Explain the volatility gate", {}), "strong")
        self.assertEqual(route_for("x " * 150, {}), "strong")            # long input
        self.assertEqual(route_for("how?", {"last_route": "strong"}), "strong")
        self.assertEqual(route_for("how?", {"last_route": "fast"}), "fast")

    def test_entities(self):
        e = entities("how is A012 and ETH doing")
        self.assertEqual(e.get("bot"), "A012")
        self.assertEqual(e.get("symbol"), "ETH-USD")
        self.assertEqual(entities("what about bitcoin")["symbol"], "BTC-USD")
        self.assertEqual(entities("tell me about agent 073")["bot"], "A073")
        self.assertEqual(entities("hi there"), {})


class _FakeMessages:
    def __init__(self, resp=None, fail=None):
        self.resp, self.fail, self.kw = resp, fail, []

    def create(self, **kw):
        self.kw.append(kw)
        if self.fail is not None:
            raise self.fail
        return self.resp


def _fake_client(beta_fail=None, plain_fail=None):
    resp = SimpleNamespace(content=[{"type": "text", "text": "Hello Abhi."}], stop_reason="end_turn",
                           model="claude-opus-5-5", usage=SimpleNamespace())
    return SimpleNamespace(beta=SimpleNamespace(messages=_FakeMessages(resp, beta_fail)),
                           messages=_FakeMessages(resp, plain_fail))


def _httpx():
    for name in ("httpx2", "httpx"):
        try:
            return __import__(name)
        except ImportError:
            pass
    return None


class TestAnthropicBrain(unittest.TestCase):
    MODELS = {"strong": "claude-opus-5-5", "fast": "claude-sonnet-5-5"}

    def test_not_ready_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None); os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            ok, why = AnthropicBrain(self.MODELS).ready()
        self.assertFalse(ok)
        self.assertIn("SETUP_REQUIRED", why)

    def test_create_uses_fallback_beta_routes_and_server_tools(self):
        c = _fake_client()
        b = AnthropicBrain(self.MODELS, web=True, client=c)
        self.assertEqual(b.ready(), (True, "ok"))
        out = b.create("strong", "sys", [{"role": "user", "content": "hi"}], [{"name": "get_time", "description": "t",
                                                                              "input_schema": obj({})}])
        kw = c.beta.messages.kw[0]
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["betas"], [FALLBACK_BETA])
        self.assertEqual(kw["fallbacks"], "default")
        types = [t.get("type") for t in kw["tools"]]
        self.assertIn("web_search_20260209", types)
        self.assertIn("web_fetch_20260209", types)
        self.assertEqual(kw["tools"][0]["name"], "get_time")
        self.assertEqual(out["content"], [{"type": "text", "text": "Hello Abhi."}])
        self.assertEqual(out["stop_reason"], "end_turn")
        b.create("fast", "sys", [{"role": "user", "content": "hi"}], [])
        self.assertEqual(c.beta.messages.kw[1]["model"], "claude-sonnet-5-5")
        self.assertEqual(c.messages.kw, [])

    def test_no_web_tools_when_research_off(self):
        c = _fake_client()
        AnthropicBrain(self.MODELS, web=False, client=c).create("fast", "s", [{"role": "user", "content": "x"}], [])
        self.assertEqual(c.beta.messages.kw[0]["tools"], [])

    def test_graceful_retry_without_fallbacks(self):
        c = _fake_client(beta_fail=TypeError("unexpected keyword argument 'fallbacks'"))
        b = AnthropicBrain(self.MODELS, client=c)
        out = b.create("strong", "s", [{"role": "user", "content": "x"}], [])
        self.assertEqual(out["stop_reason"], "end_turn")
        self.assertFalse(b.fallbacks_ok)
        self.assertEqual(len(c.messages.kw), 1)
        self.assertNotIn("fallbacks", c.messages.kw[0])
        self.assertNotIn("betas", c.messages.kw[0])
        b.create("strong", "s", [{"role": "user", "content": "x"}], [])   # second call goes straight to plain create
        self.assertEqual(len(c.beta.messages.kw), 1)
        self.assertEqual(len(c.messages.kw), 2)

    def test_bad_request_mentioning_fallback_retries(self):
        import anthropic
        hx = _httpx()
        if hx is None:
            self.skipTest("httpx not importable")
        req = hx.Request("POST", "https://api.anthropic.com/v1/messages")
        e = anthropic.BadRequestError("fallbacks: unknown parameter", response=hx.Response(400, request=req), body=None)
        c = _fake_client(beta_fail=e)
        b = AnthropicBrain(self.MODELS, client=c)
        self.assertEqual(b.create("fast", "s", [{"role": "user", "content": "x"}], [])["stop_reason"], "end_turn")
        self.assertFalse(b.fallbacks_ok)

    def test_errors_become_typed_brain_errors(self):
        import anthropic
        hx = _httpx()
        if hx is None:
            self.skipTest("httpx not importable")
        req = hx.Request("POST", "https://api.anthropic.com/v1/messages")
        cases = [(anthropic.RateLimitError("slow", response=hx.Response(429, request=req), body=None), "RATE_LIMIT"),
                 (anthropic.AuthenticationError("no", response=hx.Response(401, request=req), body=None), "AUTH"),
                 (anthropic.APIConnectionError(request=req), "NETWORK"),
                 (RuntimeError("weird"), "UNAVAILABLE")]
        for exc, code in cases:
            with self.subTest(code=code):
                b = AnthropicBrain(self.MODELS, client=_fake_client(beta_fail=exc))
                with self.assertRaises(BrainError) as cm:
                    b.create("fast", "s", [{"role": "user", "content": "x"}], [])
                self.assertEqual(cm.exception.code, code)
                self.assertTrue(b.last_error)


# ================================================================ core
class TestSpeakable(unittest.TestCase):
    def test_dollars_bullets_urls(self):
        self.assertEqual(speakable("$1,234.50"), "1,234.50 dollars")
        out = speakable("- Paper equity is $10,000.00\n- Source: https://example.com/a?b=1\n* done")
        self.assertNotIn("http", out)
        self.assertNotIn("$", out)
        self.assertFalse(out.startswith("-"))
        self.assertIn("10,000.00 dollars", out)
        self.assertNotRegex(out, r"(^|\. )[-*] ")
        self.assertEqual(speakable("**Up 5%** & rising"), "Up 5 percent and rising")
        self.assertNotIn("#", speakable("## Heading\ntext"))

    def test_price_text_never_scientific(self):
        pt = core_mod.price_text
        self.assertEqual(pt(81523.934), "81,523.93")
        self.assertEqual(pt(342.96), "342.96")
        self.assertEqual(pt(12.0), "12")
        self.assertEqual(pt(1.5), "1.5")
        self.assertEqual(pt(0.12134), "0.1213")
        self.assertEqual(pt(0.00001234), "0.00001234")
        self.assertEqual(pt(-1234.5), "-1,234.50")
        for x in (64000.0, 1e6, 0.0005, 3e-5):
            self.assertNotIn("e", pt(x).lower(), x)

    def test_markdown_link_keeps_its_text(self):
        # SOURCE BUG: jarvis/core.py speakable() strips r"https?://\S+" BEFORE the markdown-link rule
        # r"\[([^\]]+)\]\([^)]+\)", so the link rule never matches a web link: "[the docs](https://x.org/d)"
        # becomes "[the docs](" and the brackets are read aloud.
        self.assertEqual(speakable("see [the docs](https://x.org/d)"), "see the docs")

    def test_negative_dollars_keep_their_sign(self):
        # SOURCE BUG: jarvis/core.py speakable() runs the generic r"\$(-?...)" substitution BEFORE the
        # r"-\$..." -> "minus ... dollars" rule, so the "minus" rule is dead code; then the bullet stripper
        # r"^\s*[-*•]\s*" removes the leading "-" of a line, so a paper LOSS of -$45.20 is spoken as a gain
        # ("45.20 dollars"). Same for a line starting with "-2.5%".
        self.assertEqual(speakable("-$45.20"), "minus 45.20 dollars")
        self.assertIn("minus 45.20 dollars", speakable("Today's paper P&L:\n-$45.20"))
        self.assertRegex(speakable("Day change:\n-2.5%"), r"(minus |-)2\.5 percent")


class CoreBase(unittest.TestCase):
    def app_with(self, script, **kw):
        self.brain = FakeBrain(script)
        self.app = make_app(self, brain=self.brain, **kw)
        return self.app


class TestCoreCommands(CoreBase):
    def test_fixed_commands_bypass_the_model(self):
        app = self.app_with([])
        out = app.core.handle("status")
        self.assertEqual(out["route"], "command")
        self.assertIn("will not guess", out["reply"])                 # no report yet -> refuses to guess numbers
        out = app.core.handle("pause A073", session_id=out["session_id"])
        self.assertEqual(out["route"], "command")
        self.assertEqual(out["reply"], "Paused A073.")
        self.assertIn("A073", policy.paused_bots(app.fleet.cfg))
        out = app.core.handle("Halt everything", channel="desktop_voice")
        self.assertEqual(out["route"], "command")
        self.assertEqual(out["tools"], ["command:HALT"])
        self.assertTrue(policy.halt_active(app.fleet.cfg))
        self.assertEqual(self.brain.calls, [])                         # FakeBrain NEVER called
        self.assertTrue(out["paper_only"])
        self.assertTrue(any(n["title"] == "HALT" for n in app.store.notifications()))
        with open(app.fleet.cfg.audit_file) as f:
            self.assertIn("voice_route", f.read())
        self.assertTrue(app.fleet.audit.verify())

    def test_report_command_reads_file(self):
        app = self.app_with([])
        app.fleet.write_report()
        out = app.core.handle("daily report", channel="telephone", auth_level="owner_read")
        self.assertEqual(out["route"], "command")
        self.assertIn("Paper only", out["reply"])
        self.assertEqual(self.brain.calls, [])

    def test_pause_unknown_bot_command(self):
        app = self.app_with([])
        out = app.core.handle("pause A999")
        self.assertEqual(out["route"], "command")
        self.assertIn("do not know", out["reply"])
        self.assertEqual(policy.paused_bots(app.fleet.cfg), set())

    def test_anonymous_cannot_use_fixed_halt(self):
        app = self.app_with([text_resp("I can't do that without your PIN.")])
        out = app.core.handle("halt everything", channel="telephone", auth_level="anonymous")
        self.assertNotEqual(out["route"], "command")
        self.assertFalse(policy.halt_active(app.fleet.cfg))
        tools = set(self.brain.calls[0]["tools"])
        self.assertTrue(tools)
        self.assertTrue(all(app.gateway.tools[t]["tier"] == "T0" for t in tools))
        self.assertNotIn("halt_fleet", tools)

    def test_empty_text(self):
        app = self.app_with([])
        out = app.core.handle("   ")
        self.assertEqual(out["route"], "none")
        self.assertEqual(self.brain.calls, [])


class TestCoreOffline(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {})
        env.start(); self.addCleanup(env.stop)
        os.environ.pop("ANTHROPIC_API_KEY", None); os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        self.brain = AnthropicBrain({"strong": "claude-opus-5-5", "fast": "claude-sonnet-5-5"})
        self.app = make_app(self, brain=self.brain)

    def test_offline_mode_is_labelled(self):
        out = self.app.core.handle("hello jarvis, write me a poem")
        self.assertEqual(out["route"], "offline")
        self.assertIn("offline mode", out["reply"])
        self.assertIn("SETUP_REQUIRED", out["note"])
        self.assertIsNone(out["model"])
        self.assertFalse(self.app.brain_status()["ready"])

    def test_offline_quote_uses_real_tool_data(self):
        out = self.app.core.handle("what is the price of bitcoin")
        self.assertEqual(out["route"], "offline")
        self.assertIn("get_quote", out["tools"])
        q = self.app.data.quote("BTC-USD", "1h")
        self.assertIn(core_mod.price_text(q["price"]), out["reply"])
        self.assertNotRegex(out["reply"], r"\de[+-]\d")                  # never scientific notation
        self.assertIn("DEMO", out["reply"])

    def test_offline_portfolio_owner(self):
        out = self.app.core.handle("what's my paper equity", auth_level="owner_read")
        self.assertEqual(out["route"], "offline")
        self.assertIn("Paper equity is 10,000.00 dollars", out["reply"])

    def test_offline_buy_question_is_wait_without_live_data(self):
        out = self.app.core.handle("should I buy bitcoin")
        self.assertEqual(out["route"], "offline")
        self.assertTrue(out["reply"].startswith("- WAIT"))

    def test_offline_anonymous_private_question_does_not_crash(self):
        # SOURCE BUG: jarvis/core.py _offline() assumes get_portfolio / get_fleet_summary / get_risk_status /
        # search_strategies succeed (p = r["data"]; p["equity"]). For an anonymous session the gateway answers
        # UNAUTHORIZED with data=None, so handle() raises TypeError ('NoneType' object is not subscriptable)
        # instead of replying. It must check r["ok"] like the quote / agent / vol branches do.
        out = self.app.core.handle("what is my portfolio balance", channel="telephone", auth_level="anonymous")
        self.assertEqual(out["route"], "offline")
        self.assertNotIn("10,000", out["reply"])


class TestCoreLLM(CoreBase):
    def test_tool_loop_get_portfolio(self):
        app = self.app_with([tool_resp("get_portfolio", {}), text_resp("Your paper equity is 10,000 dollars.")])
        out = app.core.handle("how is my portfolio")
        self.assertEqual(out["reply"], "Your paper equity is 10,000 dollars.")
        self.assertEqual(out["tools"], ["get_portfolio"])
        self.assertEqual(out["route"], "fast")
        self.assertEqual(out["model"], "fake-fast")
        self.assertEqual(len(self.brain.calls), 2)
        res = tool_results(self.brain.calls[1]["messages"])
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["tool_use_id"], "toolu_01")
        payload = json.loads(res[0]["content"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["equity"], 10000.0)
        self.assertNotIn("is_error", res[0])
        audit = app.store.tool_audit(5)
        self.assertEqual(audit[0]["tool"], "get_portfolio")
        self.assertEqual(audit[0]["session_id"], out["session_id"])
        self.assertTrue(any(e.get("type") == "tool" and e.get("name") == "get_portfolio" for e in app.ui_events))
        # the conversation is stored
        hist = app.store.history(out["session_id"])
        self.assertEqual([h["role"] for h in hist], ["user", "assistant"])

    def test_first_call_has_context_and_system_prompt(self):
        app = self.app_with([text_resp("Hi.")])
        app.core.handle("hi")
        c = self.brain.calls[0]
        self.assertEqual(c["route"], "fast")
        self.assertIn("PAPER TRADING ONLY", c["system"])
        last = c["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("<context>", last["content"])
        self.assertIn("data mode: demo", last["content"])
        self.assertTrue(last["content"].endswith("hi"))

    def test_pause_turn_continues_without_new_user_message(self):
        paused = {"content": [{"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
                               "input": {"query": "bitcoin news"}}], "stop_reason": "pause_turn"}
        app = self.app_with([paused, text_resp("Here is the news.")])
        out = app.core.handle("what's the news on bitcoin")
        self.assertEqual(out["reply"], "Here is the news.")
        self.assertEqual(len(self.brain.calls), 2)
        m0, m1 = self.brain.calls[0]["messages"], self.brain.calls[1]["messages"]
        self.assertEqual(m1[:len(m0)], m0)
        self.assertEqual(len(m1), len(m0) + 1)
        self.assertEqual(m1[-1]["role"], "assistant")
        self.assertEqual(m1[-1]["content"], paused["content"])
        self.assertEqual(sum(1 for m in m1 if m["role"] == "user"), sum(1 for m in m0 if m["role"] == "user"))

    def test_pause_turn_continuations_are_bounded(self):
        paused = {"content": [{"type": "server_tool_use", "id": "s", "name": "web_search", "input": {}}],
                  "stop_reason": "pause_turn"}
        app = self.app_with([dict(paused) for _ in range(8)])
        app.core.handle("research something")
        self.assertEqual(len(self.brain.calls), 5)          # 1 + at most 4 continuations

    def test_tool_loop_is_bounded(self):
        app = self.app_with([tool_resp("get_time", {}, tid=f"t{i}") for i in range(15)])
        out = app.core.handle("what time is it")
        self.assertEqual(len(self.brain.calls), 10)
        self.assertIn("too many steps", out["reply"])

    def test_refusal(self):
        app = self.app_with([text_resp("partial", stop="refusal")])
        out = app.core.handle("help me with something bad")
        self.assertIn("I can't help", out["reply"])
        self.assertEqual(out["sources"], [])

    def test_max_tokens_note(self):
        app = self.app_with([text_resp("Long answer", stop="max_tokens")])
        out = app.core.handle("tell me a story")
        self.assertTrue(out["reply"].startswith("Long answer"))
        self.assertIn("ran out of room", out["reply"])

    def test_citations_only_for_urls_search_returned(self):
        real, fake = "https://news.example.com/btc-etf", "https://made-up.example.org/fabricated"
        search = {"content": [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "btc etf"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
             "content": [{"type": "web_search_result", "url": real, "title": "BTC ETF news", "encrypted_content": "x"}]},
            {"type": "text", "text": "ETF flows were positive.",
             "citations": [{"type": "web_search_result_location", "url": real, "title": "BTC ETF news", "cited_text": "..."},
                           {"type": "web_search_result_location", "url": fake, "title": "Fake", "cited_text": "..."}]},
            {"type": "text", "text": "Again.",
             "citations": [{"type": "web_search_result_location", "url": real, "title": "dup"}]}],
            "stop_reason": "end_turn"}
        app = self.app_with([search])
        out = app.core.handle("what's the latest on bitcoin ETFs")
        self.assertEqual(out["sources"], [{"url": real, "title": "BTC ETF news"}])
        self.assertIn("web_search", out["tools"])
        self.assertIn("ETF flows were positive.", out["reply"])
        self.assertNotIn(fake, json.dumps(out))

    def test_web_fetch_result_counts_as_seen(self):
        url = "https://docs.example.com/page"
        resp = {"content": [
            {"type": "web_fetch_tool_result", "tool_use_id": "s1", "content": {"type": "web_fetch_result", "url": url}},
            {"type": "text", "text": "Done.", "citations": [{"type": "char_location", "url": url, "title": "Page"}]}],
            "stop_reason": "end_turn"}
        app = self.app_with([resp])
        out = app.core.handle("read that page")
        self.assertEqual(out["sources"], [{"url": url, "title": "Page"}])

    def test_telephone_owner_read_hides_t2_tools_from_model(self):
        app = self.app_with([text_resp("Sure.")])
        app.core.handle("what can you do", channel="telephone", auth_level="owner_read")
        tools = set(self.brain.calls[0]["tools"])
        self.assertFalse(tools & t2_names(app))
        self.assertFalse(tools & T2_TOOLS)
        self.assertTrue(RR_TOOLS <= tools)
        self.assertIn("get_portfolio", tools)
        self.assertIn("permission: owner_read", self.brain.calls[0]["messages"][-1]["content"])

    def test_desktop_owner_operate_sees_t2_tools(self):
        app = self.app_with([text_resp("Sure.")])
        app.core.handle("what can you do", channel="gui", auth_level="owner_operate")
        self.assertTrue(t2_names(app) <= set(self.brain.calls[0]["tools"]))
        self.assertTrue(T2_TOOLS <= set(self.brain.calls[0]["tools"]))

    def test_model_cannot_call_hidden_or_unknown_tools(self):
        app = self.app_with([tool_resp("run_backtest", {"strategy_id": "x", "symbol": "BTC-USD"}, tid="a"),
                             tool_resp("place_order", {"symbol": "BTC-USD", "qty": 1}, tid="b"),
                             text_resp("I can't do that by phone.")])
        out = app.core.handle("run a backtest", channel="telephone", auth_level="owner_read")
        self.assertEqual(out["reply"], "I can't do that by phone.")
        res = tool_results(self.brain.calls[-1]["messages"])
        self.assertEqual(len(res), 2)
        self.assertTrue(all(r.get("is_error") for r in res))
        self.assertEqual(json.loads(res[0]["content"])["error"]["code"], "UNAUTHORIZED")
        self.assertEqual(json.loads(res[1]["content"])["error"]["code"], "UNKNOWN_TOOL")
        self.assertEqual(app.tasks.list(), [])

    def test_short_term_state_persists_across_turns(self):
        app = self.app_with([text_resp("ETH is fine."), text_resp("A012 is watching."),
                             tool_resp("get_quote", {"symbol": "SOL-USD"}), text_resp("SOL is up."), text_resp("ok")])
        o1 = app.core.handle("how is ETH doing")
        sid = o1["session_id"]
        o2 = app.core.handle("and what about A012?", session_id=sid)
        self.assertEqual(o2["session_id"], sid)
        ctx = self.brain.calls[1]["messages"][-1]["content"]
        self.assertIn('"last_symbol": "ETH-USD"', ctx)
        self.assertIn('"last_bot": "A012"', ctx)
        # the earlier turn is in the history the model sees
        self.assertEqual(self.brain.calls[1]["messages"][0]["role"], "user")
        self.assertIn("how is ETH doing", self.brain.calls[1]["messages"][0]["content"])
        self.assertEqual(self.brain.calls[1]["messages"][1], {"role": "assistant", "content": "ETH is fine."})
        app.core.handle("quote please", session_id=sid)              # model looks up SOL -> becomes last_symbol
        st = app.store.session(sid, "gui", "owner_operate", [])
        self.assertEqual(st["state"]["last_symbol"], "SOL-USD")
        self.assertEqual(st["state"]["last_bot"], "A012")
        self.assertEqual(st["state"]["turns"], 3)
        self.assertEqual(st["version"], 3)
        # a different session does not inherit it
        app.core.handle("hello")
        self.assertIn("short-term context: none", self.brain.calls[-1]["messages"][-1]["content"])

    def test_brain_error_degrades_to_offline(self):
        class Broken(FakeBrain):
            def create(self, *a, **k):
                raise BrainError("RATE_LIMIT", "Rate limited by the AI provider.")
        brain = Broken([])
        app = make_app(self, brain=brain)
        out = app.core.handle("what time is it")
        self.assertEqual(out["route"], "degraded")
        self.assertTrue(out["reply"].startswith("My AI brain is unavailable right now (RATE_LIMIT)."))

    def test_session_cannot_upgrade_channel_permissions(self):
        app = self.app_with([text_resp("a"), text_resp("b")])
        o = app.core.handle("hello", channel="gui", auth_level="owner_operate")
        app.core.handle("hello again", channel="telephone", session_id=o["session_id"], auth_level="owner_read")
        self.assertFalse(set(self.brain.calls[1]["tools"]) & t2_names(app))


# ================================================================ alerts
class TestAlertValidation(unittest.TestCase):
    def bad(self, rule, msg):
        with self.assertRaises(ValueError) as cm:
            validate_rule(rule)
        self.assertIn(msg, str(cm.exception))

    def test_rejects(self):
        mt = lambda **c: {"condition": {"kind": "metric_threshold", "metric": "price:BTC-USD", "operator": ">",
                                        "threshold": "1", **c}}
        self.bad({"condition": {"kind": "python_expr", "expr": "1"}}, "unknown condition kind")
        self.bad({}, "unknown condition kind")
        self.bad(mt(operator="=="), "operator")
        self.bad(mt(operator="!="), "operator")
        self.bad(mt(threshold="abc"), "decimal")
        self.bad(mt(threshold=None), "decimal")
        self.bad(mt(metric="bank_balance"), "unsupported metric")
        self.bad({"condition": {"kind": "scheduled", "local_time": "09:00", "recurrence": "once", "message": "x"}},
                 "local_date")
        self.bad({"condition": {"kind": "scheduled", "local_time": "09:00", "message": "x"}}, "local_date")
        self.bad({"condition": {"kind": "scheduled", "local_time": "9am", "recurrence": "daily"}}, "HH:MM")
        self.bad({"condition": {"kind": "scheduled", "local_time": "09:00", "recurrence": "weekly"}}, "recurrence")
        self.bad({"condition": {"kind": "vol_state", "symbol": "BTC-USD", "state": "WILD"}}, "LOUD/NORMAL/QUIET")
        self.bad({"condition": {"kind": "any_signal"}, "channels": ["sms"]}, "channels")
        self.bad({"condition": {"kind": "any_signal"}, "cooldown_s": -1}, "cooldown")

    def test_defaults_and_readback(self):
        r = validate_rule({"condition": {"kind": "metric_threshold", "metric": "price:BTC-USD", "operator": ">=",
                                         "threshold": "65000.5"}, "severity": "apocalyptic"})
        self.assertEqual(r["condition"]["initial_true_policy"], "wait_for_crossing")
        self.assertEqual(r["channels"], ["desktop"])
        self.assertEqual(r["timezone"], "America/Edmonton")
        self.assertEqual(r["severity"], "info")
        self.assertEqual(r["quiet_hours_behavior"], "defer")
        self.assertEqual(r["max_data_age_s"], 5400)
        self.assertEqual(describe(r), "Notify by desktop when it crosses price of BTC-USD >= 65000.5 (data no older than 90 min).")
        s = validate_rule({"condition": {"kind": "scheduled", "local_time": "08:30", "recurrence": "daily", "message": "Stretch"},
                           "channels": ["desktop", "phone"]})
        self.assertEqual(describe(s), "Remind by desktop and phone at 08:30 every day (America/Edmonton): Stretch")

    def test_rejects_non_finite_threshold(self):
        # SOURCE BUG: jarvis/alerts.py validate() accepts threshold "NaN" / "Infinity" (Decimal parses them).
        # A NaN rule can never fire: every evaluation raises InvalidOperation on the ordering comparison and is
        # silently recorded as last_error, so the owner believes an alert is armed when it is not.
        self.bad({"condition": {"kind": "metric_threshold", "metric": "price:BTC-USD", "operator": ">",
                                "threshold": "NaN"}}, "decimal")


class AlertBase(unittest.TestCase):
    def setUp(self):
        self.store = make_store(self)
        self.vals: dict = {}
        self.calls = None

    def engine(self, calls=None):
        self.eng = AlertEngine(self.store, lambda name: self.vals.get(name), calls, ("22:00", "07:00"))
        return self.eng

    def outbox(self):
        return self.store.q("SELECT * FROM outbox ORDER BY created_at")

    def notes(self, title=None):
        n = self.store.notifications(0, 500)
        return [x for x in n if title is None or x["title"] == title]


class TestMetricAlerts(AlertBase):
    T0 = local_ts(2026, 10, 14, 12, 0)                                 # Wednesday noon MT (not quiet hours)

    def setUp(self):
        super().setUp()
        self.engine()

    def rule(self, op=">", thr="65000", policy_="wait_for_crossing", **kw):
        return self.eng.add({"condition": {"kind": "metric_threshold", "metric": "price:BTC-USD", "operator": op,
                                           "threshold": thr, "initial_true_policy": policy_},
                             "label": kw.pop("label", "BTC alert"), **kw})

    def at(self, value, t, age=0):
        self.vals["price:BTC-USD"] = (value, t - age)
        return self.eng.evaluate(t)

    def test_wait_for_crossing_fires_once_on_crossing(self):
        r = self.rule()
        self.assertEqual(self.at(65100, self.T0), [])                # already true at first look: wait
        self.assertEqual(self.at(65200, self.T0 + 60), [])           # still true: no crossing
        self.assertEqual(self.at(64900, self.T0 + 120), [])          # false
        fired = self.at(65000.01, self.T0 + 180)                     # false -> true crossing
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["rule"], r["id"])
        self.assertIn("BTC-USD is 65000.01", fired[0]["message"])
        self.assertEqual(self.at(65300, self.T0 + 240), [])          # still true: no repeat
        self.assertEqual(len(self.notes("BTC alert")), 1)
        ob = self.outbox()
        self.assertEqual(len(ob), 1)
        self.assertEqual(ob[0]["state"], "confirmed")
        self.assertTrue(ob[0]["key"].startswith(f"{r['id']}:1:"))
        self.assertTrue(ob[0]["key"].endswith(":desktop"))
        st = self.eng.list()[0]["state"]
        self.assertEqual(st["last_value"], "65300")
        self.assertTrue(st["prev_true"])

    def test_trigger_policy_fires_on_first_true(self):
        self.rule(policy_="trigger")
        self.assertEqual(len(self.at(65100, self.T0)), 1)
        self.assertEqual(self.at(65100, self.T0 + 60), [])

    def test_cooldown_blocks_quick_refire(self):
        self.rule(policy_="trigger")
        self.assertEqual(len(self.at(65100, self.T0)), 1)
        self.at(64000, self.T0 + 60)
        self.assertEqual(self.at(65100, self.T0 + 120), [])          # within default 600 s cooldown

    def test_second_crossing_after_cooldown_fires_again(self):
        # SOURCE BUG: jarvis/alerts.py _check() builds the occurrence key as f"{local.date()}:{st.get('n', 0) + 1}"
        # but never increments st["n"], so every crossing on the same local day gets the SAME outbox key
        # ("<rule>:<ver>:<date>:1:<channel>"). The outbox dedupe (INSERT OR IGNORE) then swallows every crossing
        # after the first one of the day, even after the cooldown has passed.
        self.rule(policy_="trigger")
        self.assertEqual(len(self.at(65100, self.T0)), 1)
        self.at(64000, self.T0 + 300)
        fired = self.at(65100, self.T0 + 1000)                       # cooldown (600 s) has passed
        self.assertEqual(len(fired), 1)
        self.assertEqual(len(self.notes("BTC alert")), 2)

    def test_stale_data_not_evaluated(self):
        self.rule(policy_="trigger")
        self.assertEqual(self.at(70000, self.T0, age=6000), [])       # older than max_data_age_s (5400)
        st = self.eng.list()[0]["state"]
        self.assertEqual(st["note"], "data stale - not evaluated")
        self.assertNotIn("prev_true", st)
        self.assertEqual(self.notes(), [])
        self.assertEqual(len(self.at(70000, self.T0 + 60, age=60)), 1)   # fresh data: evaluated and fires

    def test_missing_metric_not_evaluated(self):
        self.rule(policy_="trigger")
        self.assertEqual(self.eng.evaluate(self.T0), [])
        self.assertEqual(self.eng.list()[0]["state"]["note"], "metric unavailable")

    def test_gt_vs_gte_at_equality(self):
        gt = self.rule(">", "100.5", "trigger", label="gt")
        gte = self.rule(">=", "100.5", "trigger", label="gte")
        lt = self.rule("<", "100.5", "trigger", label="lt")
        lte = self.rule("<=", "100.5", "trigger", label="lte")
        fired = {f["rule"] for f in self.at("100.50", self.T0)}
        self.assertEqual(fired, {gte["id"], lte["id"]})
        self.assertNotIn(gt["id"], fired)
        self.assertNotIn(lt["id"], fired)

    def test_decimal_precision(self):
        self.rule(">", "64999.99999999", "trigger")
        self.assertEqual(len(self.at("65000.00000000", self.T0)), 1)

    def test_expired_rule_disabled(self):
        r = self.eng.add({"condition": {"kind": "metric_threshold", "metric": "price:BTC-USD", "operator": ">",
                                        "threshold": "1", "initial_true_policy": "trigger"}, "expires_at": self.T0 - 1})
        self.assertEqual(self.at(5, self.T0), [])
        self.assertFalse([x for x in self.eng.list() if x["id"] == r["id"]][0]["enabled"])

    def test_desktop_quiet_hours_lowers_severity(self):
        self.rule(policy_="trigger", severity="critical", label="crit")
        night = local_ts(2026, 10, 14, 23, 30)
        self.assertEqual(len(self.at(65100, night)), 1)
        self.assertEqual(self.notes("crit")[0]["severity"], "info")


class TestEventAlerts(AlertBase):
    def setUp(self):
        super().setUp()
        self.engine()

    def test_task_completed_fires_once(self):
        self.eng.add({"condition": {"kind": "task_completed", "task_id": "abc123"}, "label": "task"})
        self.eng.emit("task_completed", "other")
        self.assertEqual(self.eng.evaluate(), [])
        self.eng.emit("task_completed", "abc123")
        self.assertEqual(len(self.eng.evaluate()), 1)
        self.assertEqual(self.eng.evaluate(), [])
        self.assertEqual(self.notes("task")[0]["body"], "Task abc123 finished")

    def test_any_signal_ignores_old_events(self):
        self.eng.emit("signal", "A001: old signal")
        time.sleep(0.01)
        self.eng.add({"condition": {"kind": "any_signal"}, "label": "sig"})
        self.assertEqual(self.eng.evaluate(), [])                     # first look sets the baseline
        time.sleep(0.01)
        self.eng.emit("signal", "A002: shadow LONG signal")
        fired = self.eng.evaluate()
        self.assertEqual(len(fired), 1)
        self.assertIn("A002", fired[0]["message"])

    def test_vol_state_transition(self):
        self.eng.add({"condition": {"kind": "vol_state", "symbol": "BTC-USD", "state": "LOUD"}})
        t = time.time()
        self.vals["vol:BTC-USD"] = ("LOUD", t)
        self.assertEqual(self.eng.evaluate(t), [])                    # already LOUD at first look
        self.vals["vol:BTC-USD"] = ("NORMAL", t)
        self.assertEqual(self.eng.evaluate(t + 1), [])
        self.vals["vol:BTC-USD"] = ("LOUD", t)
        self.assertEqual(len(self.eng.evaluate(t + 2)), 1)


class TestScheduledAlerts(AlertBase):
    def setUp(self):
        super().setUp()
        self.engine()

    def once(self, hhmm, date="2026-10-14", **kw):
        return self.eng.add({"condition": {"kind": "scheduled", "local_time": hhmm, "local_date": date,
                                           "recurrence": "once", "message": kw.pop("message", "Check the fleet")},
                             "label": kw.pop("label", "Reminder"), **kw})

    def test_once_fires_once_then_disables(self):
        r = self.once("12:30")
        self.assertEqual(self.eng.evaluate(local_ts(2026, 10, 14, 12, 29)), [])
        fired = self.eng.evaluate(local_ts(2026, 10, 14, 12, 31))
        self.assertEqual(fired, [{"rule": r["id"], "message": "Check the fleet"}])
        self.assertFalse(self.eng.list()[0]["enabled"])
        self.assertEqual(self.eng.evaluate(local_ts(2026, 10, 14, 12, 32)), [])
        self.assertEqual(len(self.notes("Reminder")), 1)
        self.assertEqual(self.notes("Reminder")[0]["body"], "Check the fleet")

    def test_once_long_missed_is_disabled_without_firing(self):
        self.once("06:00")
        self.assertEqual(self.eng.evaluate(local_ts(2026, 10, 14, 13, 0)), [])
        self.assertFalse(self.eng.list()[0]["enabled"])
        self.assertEqual(self.notes(), [])

    def test_daily_fires_once_per_day(self):
        self.eng.add({"condition": {"kind": "scheduled", "local_time": "09:00", "recurrence": "daily", "message": "Stand up"},
                      "label": "daily"})
        self.assertEqual(self.eng.evaluate(local_ts(2026, 10, 14, 8, 59)), [])
        self.assertEqual(len(self.eng.evaluate(local_ts(2026, 10, 14, 9, 0, 30))), 1)
        self.assertEqual(self.eng.evaluate(local_ts(2026, 10, 14, 9, 20)), [])
        self.assertEqual(self.eng.evaluate(local_ts(2026, 10, 14, 9, 50)), [])
        self.assertEqual(len(self.eng.evaluate(local_ts(2026, 10, 15, 9, 5))), 1)
        self.assertEqual(self.eng.evaluate(local_ts(2026, 10, 15, 9, 6)), [])
        self.assertEqual(len(self.notes("daily")), 2)
        self.assertTrue(self.eng.list()[0]["enabled"])
        keys = sorted(o["key"] for o in self.outbox())
        self.assertTrue(keys[0].endswith(":daily:2026-10-14:desktop"))
        self.assertTrue(keys[1].endswith(":daily:2026-10-15:desktop"))

    def test_desktop_delivery_writes_notification_and_outbox(self):
        self.once("12:30", severity="warning", label="Desk")
        self.eng.evaluate(local_ts(2026, 10, 14, 12, 31))
        n = self.notes("Desk")
        self.assertEqual(len(n), 1)
        self.assertEqual(n[0]["severity"], "warning")
        ob = self.outbox()
        self.assertEqual([(o["channel"], o["state"]) for o in ob], [("desktop", "confirmed")])

    def test_dedupe_same_occurrence_delivers_once(self):
        r = validate_rule({"condition": {"kind": "any_signal"}, "label": "dup"})
        local = datetime(2026, 10, 14, 12, 0, tzinfo=MT)
        self.assertTrue(self.eng._deliver(r, "occ-1", "hello", local))
        self.assertFalse(self.eng._deliver(r, "occ-1", "hello", local))
        self.assertEqual(len(self.notes("dup")), 1)
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(self.outbox()[0]["key"], f"{r['id']}:{r['version']}:occ-1:desktop")

    def test_phone_without_twilio_env_fails_before_send(self):
        with mock.patch.dict(os.environ, {}):
            for k in TWILIO_ENV:
                os.environ.pop(k, None)
            self.engine(OutboundCallManager(self.store, {"max_calls_per_hour": 2, "max_calls_per_day": 6}))
            self.once("12:30", channels=["phone"])
            self.eng.evaluate(local_ts(2026, 10, 14, 12, 31))
        n = self.notes("Phone alert not sent")
        self.assertEqual(len(n), 1)
        self.assertIn("SETUP_REQUIRED", n[0]["body"])
        ob = self.outbox()
        self.assertEqual(len(ob), 1)
        self.assertEqual(ob[0]["channel"], "phone")
        self.assertEqual(ob[0]["state"], "failed_before_send")
        self.assertEqual(ob[0]["error"], "SETUP_REQUIRED")
        self.assertEqual(ob[0]["message"], GENERIC_CALL)                 # never the private alert text

    def test_quiet_hours_defer_phone(self):
        calls = mock.Mock()
        calls.request.return_value = {"ok": True, "state": "confirmed"}
        self.engine(calls)
        self.once("23:00", channels=["phone", "desktop"], label="Late")
        fired = self.eng.evaluate(local_ts(2026, 10, 14, 23, 1))
        self.assertEqual(len(fired), 1)                                  # desktop still delivered
        calls.request.assert_not_called()
        self.assertEqual(len(self.notes("Call deferred (quiet hours)")), 1)
        self.assertEqual(self.notes("Late")[0]["severity"], "info")
        self.assertEqual([o["channel"] for o in self.outbox()], ["desktop"])

    def test_quiet_hours_suppress_and_override(self):
        calls = mock.Mock()
        calls.request.return_value = {"ok": True, "state": "confirmed"}
        self.engine(calls)
        self.once("23:00", channels=["phone"], quiet_hours_behavior="suppress", label="S")
        self.once("23:00", channels=["phone"], quiet_hours_behavior="authorized_override", severity="critical", label="O")
        self.once("23:00", channels=["phone"], quiet_hours_behavior="authorized_override", severity="warning", label="W")
        self.eng.evaluate(local_ts(2026, 10, 14, 23, 1))
        self.assertEqual(calls.request.call_count, 1)                    # only the critical override calls
        self.assertEqual(len(self.notes("Call deferred (quiet hours)")), 1)  # the non-critical override is deferred

    def test_quiet_hours_window(self):
        e = self.engine()
        q = lambda h, m: e._in_quiet(datetime(2026, 10, 14, h, m, tzinfo=MT))
        self.assertTrue(q(22, 0)); self.assertTrue(q(23, 59)); self.assertTrue(q(0, 30)); self.assertTrue(q(6, 59))
        self.assertFalse(q(7, 0)); self.assertFalse(q(12, 0)); self.assertFalse(q(21, 59))


# ================================================================ phone
class TestPinAuth(unittest.TestCase):
    PIN = "730519"

    def setUp(self):
        self.dir = tmpdir(self)
        self.pin = PinAuth(self.dir)

    def test_set_and_verify(self):
        self.assertFalse(self.pin.configured())
        self.assertFalse(self.pin.verify(self.PIN))                      # no PIN set -> nothing verifies
        self.pin.set_pin(self.PIN)
        self.assertTrue(self.pin.configured())
        self.assertTrue(self.pin.verify(self.PIN))
        self.assertFalse(self.pin.verify("000000"))
        self.assertFalse(self.pin.verify(""))
        self.assertFalse(self.pin.verify(None))

    def test_bad_pin_formats_rejected(self):
        for bad in ("123", "12a4", "12345678901", "", "12 34"):
            with self.subTest(pin=bad), self.assertRaises(ValueError):
                self.pin.set_pin(bad)

    def test_wrong_pin_increments_and_success_resets(self):
        self.pin.set_pin(self.PIN)
        self.pin.verify("111111"); self.pin.verify("222222")
        self.assertEqual(len(read_json(self.pin.path)["fails"]), 2)
        self.assertTrue(self.pin.verify(self.PIN))
        self.assertEqual(read_json(self.pin.path)["fails"], [])

    def test_lockout_after_five_failures(self):
        self.pin.set_pin(self.PIN)
        for i in range(4):
            self.pin.verify("99999" + str(i))
        self.assertFalse(self.pin.locked())
        self.pin.verify("999999")
        self.assertTrue(self.pin.locked())
        self.assertFalse(self.pin.verify(self.PIN))                     # even the right PIN is refused while locked
        self.assertEqual(len(read_json(self.pin.path)["fails"]), 5)

    def test_pin_never_stored_in_plain_text(self):
        self.pin.set_pin(self.PIN)
        self.pin.verify("111111")
        raw = read_text(self.pin.path)
        self.assertNotIn(self.PIN, raw)
        d = json.loads(raw)
        self.assertEqual(set(d) - {"fails"}, {"salt", "hash", "iterations"})
        self.assertGreaterEqual(d["iterations"], 100_000)
        expect = hashlib.pbkdf2_hmac("sha256", self.PIN.encode(), bytes.fromhex(d["salt"]), d["iterations"]).hex()
        self.assertEqual(d["hash"], expect)
        other = PinAuth(tmpdir(self)); other.set_pin(self.PIN)          # fresh salt -> different hash
        self.assertNotEqual(read_json(other.path)["hash"], d["hash"])


class TestTwilioSignature(unittest.TestCase):
    def test_known_good_vector_computed_independently(self):
        token = "test_auth_token_123"
        url = "https://jarvis.example.ngrok.app/phone/pin"
        params = {"From": "+15875550199", "CallSid": "CA0123456789abcdef", "Digits": "4821"}
        # Twilio: URL + each POST param name+value, params sorted by name, HMAC-SHA1 with the auth token, base64.
        data = url + "CallSid" + "CA0123456789abcdef" + "Digits" + "4821" + "From" + "+15875550199"
        sig = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()
        self.assertTrue(twilio_signature_ok(token, url, params, sig))
        self.assertFalse(twilio_signature_ok(token, url, {**params, "Digits": "4822"}, sig))
        self.assertFalse(twilio_signature_ok(token, url + "?x=1", params, sig))
        self.assertFalse(twilio_signature_ok("other_token", url, params, sig))
        self.assertFalse(twilio_signature_ok(token, url, params, ""))
        self.assertFalse(twilio_signature_ok(token, url, params, None))

    def test_twilio_published_example(self):
        # The example from Twilio's request-validation documentation.
        params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234",
                  "From": "+14158675310", "To": "+18005551212"}
        self.assertTrue(twilio_signature_ok("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params,
                                            "GvWf1cFY/Q7PnoempGyD5oXAezc="))

    def test_inbound_verify_uses_env_token(self):
        url, params = "https://x.example/phone/voice", {"CallSid": "CA1"}
        data = url + "CallSidCA1"
        sig = base64.b64encode(hmac.new(b"tok_test", data.encode(), hashlib.sha1).digest()).decode()
        ib = InboundCalls(PinAuth(tmpdir(self)), lambda *a, **k: {})
        with mock.patch.dict(os.environ, {"TWILIO_AUTH_TOKEN": "tok_test"}):
            self.assertTrue(ib.verify(url, params, sig))
            self.assertFalse(ib.verify(url, params, "AAAA"))


class TestOutboundCalls(unittest.TestCase):
    def setUp(self):
        self.store = make_store(self)
        env = mock.patch.dict(os.environ, FAKE_TWILIO)
        env.start(); self.addCleanup(env.stop)
        self.sent = []

    def mgr(self, **cfg):
        return OutboundCallManager(self.store, {"max_calls_per_hour": 2, "max_calls_per_day": 6, **cfg})

    def sender(self, text):
        self.sent.append(text)
        return f"CA{len(self.sent):04d}"

    def row(self, key):
        return self.store.q("SELECT * FROM outbox WHERE key=?", (key,))[0]

    def test_status(self):
        self.assertEqual(self.mgr().status()["state"], "READY")
        self.assertEqual(self.mgr(calls_off=True).status()["state"], "KILL_SWITCH_ON")
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("JARVUS_MY_NUMBER")
            st = self.mgr().status()
        self.assertEqual(st["state"], "SETUP_REQUIRED")
        self.assertEqual(st["missing"], ["JARVUS_MY_NUMBER"])

    def test_confirmed_and_dedupe_same_key(self):
        m = self.mgr()
        r1 = m.request("rule1:1:occ:phone", "rule1", sender=self.sender)
        self.assertEqual(r1, {"ok": True, "state": "confirmed", "provider_id": "CA0001"})
        r2 = m.request("rule1:1:occ:phone", "rule1", sender=self.sender)
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["state"], "confirmed")
        self.assertEqual(r2["reason"], "already handled")
        self.assertEqual(self.sent, [GENERIC_CALL])                      # one call, generic text only
        self.assertEqual(self.row("rule1:1:occ:phone")["provider_id"], "CA0001")
        self.assertFalse(re.search(r"\d", GENERIC_CALL))                 # no numbers / balances spoken

    def test_hourly_cap(self):
        m = self.mgr(max_calls_per_hour=2)
        self.assertTrue(m.request("k1", "r", sender=self.sender)["ok"])
        self.assertTrue(m.request("k2", "r", sender=self.sender)["ok"])
        r3 = m.request("k3", "r", sender=self.sender)
        self.assertEqual(r3, {"ok": False, "state": "CAPPED"})
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.row("k3")["state"], "failed_before_send")
        self.assertEqual(self.row("k3")["error"], "call cap reached")

    def test_daily_cap(self):
        m = self.mgr(max_calls_per_hour=100, max_calls_per_day=1)
        self.assertTrue(m.request("d1", "r", sender=self.sender)["ok"])
        self.assertEqual(m.request("d2", "r", sender=self.sender)["state"], "CAPPED")

    def test_kill_switch_and_setup_required_do_not_send(self):
        r = self.mgr(calls_off=True).request("ks", "r", sender=self.sender)
        self.assertEqual(r["state"], "KILL_SWITCH_ON")
        self.assertEqual(self.row("ks")["state"], "failed_before_send")
        with mock.patch.dict(os.environ, {}):
            for k in TWILIO_ENV:
                os.environ.pop(k, None)
            r = self.mgr().request("nosetup", "r", sender=self.sender)
        self.assertEqual(r["state"], "SETUP_REQUIRED")
        self.assertEqual(self.sent, [])

    def test_sender_exception_is_failed_before_send(self):
        def boom(text):
            raise ConnectionRefusedError("no route")
        r = self.mgr().request("ex", "r", sender=boom)
        self.assertEqual(r, {"ok": False, "state": "failed_before_send"})
        self.assertEqual(self.row("ex")["state"], "failed_before_send")
        self.assertEqual(self.row("ex")["error"], "ConnectionRefusedError")

    def test_timeout_is_unknown_outcome(self):
        def slow(text):
            raise TimeoutError("read timed out")
        m = self.mgr(max_calls_per_hour=1)
        r = m.request("to", "r", sender=slow)
        self.assertEqual(r, {"ok": False, "state": "unknown_outcome"})
        self.assertEqual(self.row("to")["state"], "unknown_outcome")
        # an unknown outcome might have rung the phone: it counts toward the cap
        self.assertEqual(m.request("to2", "r", sender=self.sender)["state"], "CAPPED")
        # and is never retried under the same key
        self.assertEqual(m.request("to", "r", sender=self.sender)["reason"], "already handled")
        self.assertEqual(self.sent, [])

    def test_default_sender_never_reaches_network(self):
        r = self.mgr().request("net", "r")                               # urlopen is patched to raise
        self.assertFalse(r["ok"])
        self.assertEqual(self.row("net")["state"], "failed_before_send")


class TestInboundCalls(unittest.TestCase):
    PIN = "482913"

    def setUp(self):
        self.pin = PinAuth(tmpdir(self))
        self.pin.set_pin(self.PIN)
        self.handled = []

        def core_handle(text, channel, session_id, auth_level):
            self.handled.append({"text": text, "channel": channel, "session_id": session_id, "auth_level": auth_level})
            return {"session_id": "sess-1", "reply": "Fleet is fine.", "speak": "Fleet is fine."}
        self.ib = InboundCalls(self.pin, core_handle)

    def test_enabled_needs_flag_token_and_pin(self):
        with mock.patch.dict(os.environ, {}):
            for k in TWILIO_ENV:
                os.environ.pop(k, None)
            self.assertFalse(self.ib.enabled())
            os.environ["JARVIS_PHONE_INBOUND"] = "1"
            self.assertFalse(self.ib.enabled())
            os.environ["TWILIO_AUTH_TOKEN"] = "t"
            self.assertTrue(self.ib.enabled())
            self.assertFalse(InboundCalls(PinAuth(tmpdir(self)), None).enabled())   # no PIN configured

    def test_pin_flow(self):
        xml = self.ib.voice({"CallSid": "CA1"})
        self.assertIn('<Gather input="dtmf"', xml)
        self.assertIn('action="/phone/pin"', xml)
        # speech before the PIN is refused and never reaches the core
        out = self.ib.speech({"CallSid": "CA1", "SpeechResult": "halt everything"})
        self.assertIn("call back and authenticate", out)
        self.assertEqual(self.handled, [])
        out = self.ib.pin_entered({"CallSid": "CA1", "Digits": "111111"})
        self.assertIn("Try again", out)
        out = self.ib.pin_entered({"CallSid": "CA1", "Digits": self.PIN})
        self.assertIn("Authenticated", out)
        self.assertIn('input="speech"', out)
        out = self.ib.speech({"CallSid": "CA1", "SpeechResult": "status please"})
        self.assertIn("Fleet is fine.", out)
        self.assertEqual(self.handled, [{"text": "status please", "channel": "telephone", "session_id": None,
                                         "auth_level": "owner_read"}])
        self.ib.speech({"CallSid": "CA1", "SpeechResult": "and the risk?"})
        self.assertEqual(self.handled[-1]["session_id"], "sess-1")         # same session for the whole call
        self.assertNotIn(self.PIN, json.dumps(self.handled))               # PIN digits never reach the AI
        self.assertIn("did not catch", self.ib.speech({"CallSid": "CA1", "SpeechResult": "  "}))

    def test_unknown_or_expired_call(self):
        self.assertIn("Session expired", self.ib.pin_entered({"CallSid": "nope", "Digits": self.PIN}))
        self.ib.voice({"CallSid": "CA2"})
        self.ib.calls["CA2"]["exp"] = time.time() - 1
        self.assertIn("Session expired", self.ib.pin_entered({"CallSid": "CA2", "Digits": self.PIN}))
        self.ib.voice({"CallSid": "CA3"})
        self.ib.pin_entered({"CallSid": "CA3", "Digits": self.PIN})
        self.ib.calls["CA3"]["exp"] = time.time() - 1
        self.assertIn("call back and authenticate", self.ib.speech({"CallSid": "CA3", "SpeechResult": "status"}))
        self.assertEqual(self.handled, [])

    def test_three_wrong_pins_hang_up(self):
        self.ib.voice({"CallSid": "CA4"})
        self.ib.pin_entered({"CallSid": "CA4", "Digits": "1"})
        self.ib.pin_entered({"CallSid": "CA4", "Digits": "2"})
        out = self.ib.pin_entered({"CallSid": "CA4", "Digits": "3"})
        self.assertIn("<Hangup/>", out)
        self.assertFalse(self.ib.calls["CA4"]["auth"])

    def test_twiml_escapes_reply(self):
        self.ib.core_handle = lambda *a, **k: {"session_id": "s", "speak": "<Redirect>http://evil</Redirect> & more"}
        self.ib.voice({"CallSid": "CA5"})
        self.ib.pin_entered({"CallSid": "CA5", "Digits": self.PIN})
        out = self.ib.speech({"CallSid": "CA5", "SpeechResult": "x"})
        self.assertNotIn("<Redirect>", out)
        self.assertIn("&lt;Redirect&gt;", out)


class TestPhoneIntegration(CoreBase):
    def test_phone_call_reaches_core_with_read_access_only(self):
        app = self.app_with([text_resp("Noted.")])
        app.pin.set_pin("559911")
        ib = app.inbound
        ib.voice({"CallSid": "CAX"})
        ib.pin_entered({"CallSid": "CAX", "Digits": "559911"})
        ib.speech({"CallSid": "CAX", "SpeechResult": "remember that I like tea"})
        tools = set(self.brain.calls[0]["tools"])
        self.assertFalse(tools & t2_names(app))
        self.assertIn("halt_fleet", tools)
        self.assertNotIn("559911", json.dumps(self.brain.calls))
        out = ib.speech({"CallSid": "CAX", "SpeechResult": "halt everything"})   # fixed command, no AI
        self.assertTrue(policy.halt_active(app.fleet.cfg))
        self.assertIn("Halting everything", out)
        self.assertEqual(len(self.brain.calls), 1)


# ================================================================ memory + tasks
class TestStore(unittest.TestCase):
    def setUp(self):
        self.store = make_store(self)

    def test_session_and_optimistic_concurrency(self):
        s = self.store.session(None, "gui", "owner_operate", ["read"])
        self.assertEqual(s["version"], 0)
        sid = s["id"]
        self.assertTrue(self.store.save_state(sid, {"last_symbol": "BTC-USD"}, 0))
        self.assertFalse(self.store.save_state(sid, {"last_symbol": "ETH-USD"}, 0))      # stale version
        s2 = self.store.session(sid, "gui", "owner_operate", [])
        self.assertEqual(s2["version"], 1)
        self.assertEqual(s2["state"], {"last_symbol": "BTC-USD"})
        self.assertEqual(s2["scopes"], ["read"])
        self.assertTrue(self.store.save_state(sid, {"last_symbol": "SOL-USD"}, 1))
        self.assertEqual(self.store.session(sid, "gui", "x", [])["state"]["last_symbol"], "SOL-USD")
        self.assertFalse(self.store.save_state("no-such-session", {}, 0))

    def test_concurrent_writers_only_one_wins(self):
        sid = self.store.session(None, "gui", "owner_operate", [])["id"]
        results = []
        barrier = threading.Barrier(8)

        def writer(i):
            barrier.wait()
            results.append(self.store.save_state(sid, {"w": i}, 0))
        th = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in th: t.start()
        for t in th: t.join()
        self.assertEqual(results.count(True), 1)

    def test_messages_history(self):
        sid = "s1"
        for i in range(5):
            self.store.add_message(sid, "user" if i % 2 == 0 else "assistant", f"m{i}", "gui")
        self.store.add_message("s2", "user", "other", "gui")
        h = self.store.history(sid, 3)
        self.assertEqual([x["content"] for x in h], ["m2", "m3", "m4"])

    def test_remember_recall_forget(self):
        a = self.store.remember("Abhi prefers short answers", "preference")
        b = self.store.remember("Project: build the Jarvis portal", "project")
        self.assertEqual({m["id"] for m in self.store.memories()}, {a, b})
        found = self.store.memories("short answers please")
        self.assertEqual([m["id"] for m in found], [a])
        self.assertEqual(self.store.memories("zzzz qqqq"), [])
        self.store.forget(a)
        self.assertEqual([m["id"] for m in self.store.memories()], [b])
        long_id = self.store.remember("x" * 900)
        self.assertEqual(len([m for m in self.store.memories() if m["id"] == long_id][0]["text"]), 500)

    def test_correct_memory(self):
        a = self.store.remember("likes coffee")
        self.assertTrue(self.store.correct_memory(a, "likes tea"))
        m = self.store.memories()[0]
        self.assertEqual(m["text"], "likes tea")
        self.assertEqual(m["corrected"], 1)

    def test_correct_unknown_memory_returns_false(self):
        # SOURCE BUG: jarvis/memory.py correct_memory() returns `self.x(...) is not None`, but x() returns
        # cursor.lastrowid, which sqlite3 sets to an int (0) after an UPDATE as well, so it is ALWAYS True -
        # even when no memory has that id. It should check the UPDATE's rowcount.
        self.assertFalse(self.store.correct_memory("no-such-id", "text"))

    def test_purge_retention(self):
        old = time.time() - 40 * 86400
        self.store.add_message("s", "user", "fresh", "gui")
        self.store.x("INSERT INTO messages (session_id, role, content, channel, created_at, meta) VALUES (?,?,?,?,?,?)",
                     ("s", "user", "ancient", "gui", old, "{}"))
        self.store.audit_tool("s", "gui", "get_time", "T0", {}, True, None, 1)
        self.store.x("INSERT INTO tool_audit (ts, session_id, channel, tool, tier, args, ok, error, ms) VALUES (?,?,?,?,?,?,?,?,?)",
                     (old, "s", "gui", "get_time", "T0", "{}", 1, None, 1))
        keep = self.store.remember("keep forever")
        temp = self.store.remember("temporary", expires_days=1)
        self.store.x("UPDATE memories SET expires_at=? WHERE id=?", (time.time() - 10, temp))
        self.store.purge(30)
        self.assertEqual([m["content"] for m in self.store.recent_messages()], ["fresh"])
        self.assertEqual(len(self.store.tool_audit()), 1)
        self.assertEqual([m["id"] for m in self.store.memories()], [keep])

    def test_notifications(self):
        a = self.store.notify("t1", "b1")
        self.store.notify("t2", "b2", "warning")
        self.assertEqual([n["title"] for n in self.store.notifications()], ["t2", "t1"])
        self.assertEqual([n["title"] for n in self.store.notifications(since_id=a)], ["t2"])


class TestTaskManager(unittest.TestCase):
    def setUp(self):
        self.store = make_store(self)
        self.done = []

    def tm(self, workers=2):
        t = TaskManager(self.store, workers, on_done=lambda *a: self.done.append(a))
        self.addCleanup(lambda: t.pool.shutdown(wait=True, cancel_futures=True))
        return t

    def wait_status(self, tm, tid, status, timeout=10):
        return wait_for(lambda: (tm.get(tid) or {}).get("status") == status, timeout)

    def test_submit_runs_to_done_with_result(self):
        tm = self.tm()

        def job(progress, cancel):
            progress(0.5, "half way")
            return {"answer": 42, "items": [1, 2]}
        tid = tm.submit("backtest", "Test job", job, session_id="S")
        self.assertTrue(self.wait_status(tm, tid, "done"))
        t = tm.get(tid)
        self.assertEqual(t["result"], {"answer": 42, "items": [1, 2]})
        self.assertEqual(t["progress"], 1)
        self.assertEqual(t["session_id"], "S")
        self.assertIsNone(tm.get(tid, with_result=False)["result"])
        self.assertTrue(wait_for(lambda: self.done))
        self.assertEqual(self.done[0], (tid, "backtest", "Test job", "done"))
        self.assertEqual([x["id"] for x in tm.list()], [tid])
        self.assertIsNone(tm.get("missing"))

    def test_failing_job(self):
        tm = self.tm()

        def job(progress, cancel):
            raise ValueError("bad data")
        tid = tm.submit("backtest", "Fails", job)
        self.assertTrue(self.wait_status(tm, tid, "failed"))
        self.assertIn("ValueError: bad data", tm.get(tid)["error"])
        self.assertTrue(wait_for(lambda: self.done))
        self.assertEqual(self.done[0][3], "failed")

    def test_cancel_queued(self):
        tm = self.tm(workers=1)
        gate = threading.Event()
        ran = []

        def blocker(progress, cancel):
            gate.wait(10)
            return "first"

        def second(progress, cancel):
            ran.append(True)
            return "second"
        t1 = tm.submit("backtest", "blocker", blocker)
        self.assertTrue(self.wait_status(tm, t1, "running"))
        t2 = tm.submit("backtest", "queued", second)
        self.assertEqual(tm.get(t2)["status"], "queued")
        self.assertTrue(tm.cancel(t2))
        self.assertEqual(tm.get(t2)["status"], "cancelled")
        gate.set()
        self.assertTrue(self.wait_status(tm, t1, "done"))
        tm.pool.shutdown(wait=True)
        self.assertEqual(ran, [])
        self.assertEqual(tm.get(t2)["status"], "cancelled")
        self.assertFalse(tm.cancel("unknown"))

    def test_cancel_running_job_via_progress(self):
        tm = self.tm()
        started = threading.Event()

        def job(progress, cancel):
            started.set()
            for i in range(500):
                progress(i / 500, f"step {i}")
                time.sleep(0.01)
            return "finished"
        tid = tm.submit("role_test", "long", job)
        self.assertTrue(started.wait(5))
        tm.cancel(tid)
        self.assertTrue(self.wait_status(tm, tid, "cancelled"))
        self.assertTrue(wait_for(lambda: self.done))
        self.assertEqual(self.done[0][3], "cancelled")

    def test_restart_marks_unfinished_as_interrupted(self):
        now = time.time()
        self.store.x("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("old1", "backtest", "t", "running", 0.3, "", now, now, None, None, None))
        self.store.x("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("old2", "backtest", "t", "done", 1, "", now, now, "1", None, None))
        tm = self.tm()
        self.assertEqual(tm.get("old1")["status"], "interrupted")
        self.assertEqual(tm.get("old2")["status"], "done")


class TestAppWiring(CoreBase):
    def test_task_completion_feeds_alert_rule(self):
        app = self.app_with([])
        tid = app.tasks.submit("backtest", "quick", lambda progress, cancel: {"ok": 1})
        self.assertTrue(wait_for(lambda: app.tasks.get(tid)["status"] == "done"))
        self.assertTrue(wait_for(lambda: any(e[1] == tid for e in app.alerts.events)))
        app.alerts.add({"condition": {"kind": "task_completed", "task_id": tid}, "label": "bt done"})
        fired = app.alerts.evaluate()
        self.assertEqual(len(fired), 1)
        self.assertTrue(any(n["title"] == "bt done" for n in app.store.notifications()))
        self.assertTrue(any(n["title"] == "Task done" for n in app.store.notifications()))

    def test_metrics_for_alerts(self):
        app = self.app_with([])
        v, t = app.metric("price:BTC-USD")
        self.assertEqual(v, app.data.quote("BTC-USD")["price"])
        self.assertIsInstance(t, (int, float))
        self.assertEqual(app.metric("paper_equity")[0], 10000.0)
        self.assertEqual(app.metric("open_positions")[0], 0)
        self.assertEqual(app.metric("paused_bots")[0], 0)
        self.assertIsNone(app.metric("bank_balance"))

    def test_state_stays_in_temp_dir(self):
        app = self.app_with([text_resp("ok")])
        app.core.handle("hello")
        app.core.handle("halt everything")
        self.assertTrue(os.path.exists(os.path.join(app.state_dir, "jarvis.db")))
        self.assertTrue(os.path.exists(os.path.join(app.state_dir, "HALT")))
        base = os.path.realpath(app.state_dir)
        for path in (app.store.path, app.fleet.cfg.halt_file, app.fleet.cfg.audit_file, app.fleet.cfg.pause_dir,
                     app.data.dir, app.pin.path, app.report_path, app.sites_dir):
            self.assertTrue(os.path.realpath(path).startswith(base + os.sep), path)
        self.assertNotEqual(base, os.path.realpath(os.path.join(ROOT, "state")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
