"""tests/test_server.py - stdlib unittest suite for server.py (the local web server of the Jarvis Trading Portal).

Run from the project folder:
    python3 -m unittest tests.test_server -v
    python3 tests/test_server.py

What these tests do:
  - Start the REAL server (server.H on a ThreadingHTTPServer) in a background thread on a free port on 127.0.0.1,
    wired to a real App (DataService mode='demo', simulated prices labelled DEMO) in a temporary state folder, with a
    scripted FakeBrain. One fleet cycle runs in setUpClass so the fleet feed, report and book files exist.
  - Talk to it over real HTTP with http.client (Host / Origin / X-Jarvis-Token headers set per request).
  - Risk-cutting tests (HALT, PAUSE, phone) run against a SEPARATE throwaway App swapped into server.APP, so the
    shared app never gets halted.

Rules these tests follow:
  - NO network: urllib.request.urlopen and engine.data_feed._get_json raise, and socket.create_connection only allows
    loopback. Data is demo bars only.
  - All state goes to temporary folders; nothing is written to the project's state/ folder.
  - Source files are never modified. Tests that expose a real source bug are kept, marked @unittest.expectedFailure,
    and carry a 'SOURCE BUG:' comment.
"""
from __future__ import annotations
import base64, contextlib, hashlib, hmac, http.client, io, json, os, re, shutil, socket, sys, tempfile, threading, time
import unittest, warnings
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ---- environment BEFORE importing server (server.py reads config/env at import time) ----
_ENV_KEYS = ("JARVIS_DATA_MODE", "JARVUS_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "TWILIO_ACCOUNT_SID",
             "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "JARVUS_MY_NUMBER", "JARVIS_PHONE_INBOUND", "JARVIS_PUBLIC_URL")
_ENV_BEFORE = {k: os.environ.get(k) for k in _ENV_KEYS}
_STATE = tempfile.mkdtemp(prefix="jarvis_server_test_")
os.environ["JARVIS_DATA_MODE"] = "demo"
os.environ["JARVUS_DIR"] = _STATE
for _k in _ENV_KEYS[2:]:
    os.environ.pop(_k, None)

import server                                                   # noqa: E402
from http.server import ThreadingHTTPServer                     # noqa: E402
import policy                                                   # noqa: E402  (kit/, put on sys.path by engine)
from engine import library                                      # noqa: E402
from jarvis.app import App, load_config                         # noqa: E402
from jarvis.brain import AnthropicBrain, FakeBrain              # noqa: E402

assert os.path.realpath(server.__file__) == os.path.realpath(os.path.join(ROOT, "server.py")), server.__file__

MT = ZoneInfo("America/Edmonton")
REPO_STATE = os.path.realpath(os.path.join(ROOT, "state"))
T2_TOOLS = {"run_backtest", "run_role_test", "create_alert_rule", "disable_alert_rule", "remember", "forget_memory",
            "open_panel", "start_research", "build_website", "open_website"}
FORBIDDEN_WORDS = re.compile(r"resume|unhalt|clear|unpause|order|place|buy|sell|withdraw|transfer|leverage|short", re.I)


# ---------------------------------------------------------------- shared server (one per module run)
class _Shared:
    app: App | None = None
    srv: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None
    brain: FakeBrain | None = None
    port: int = 0


_PATCHES: list = []


def _no_network(*a, **k):
    raise RuntimeError("tests must not use the network")


_real_create_connection = socket.create_connection


def _loopback_only(address, *a, **k):
    if str(address[0]) not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError(f"tests must not use the network ({address[0]})")
    return _real_create_connection(address, *a, **k)


def setUpModule():
    warnings.filterwarnings("ignore", category=ResourceWarning)   # source opens json files without closing them
    for target, repl in (("urllib.request.urlopen", _no_network), ("engine.data_feed._get_json", _no_network),
                         ("socket.create_connection", _loopback_only)):
        p = mock.patch(target, repl)
        p.start()
        _PATCHES.append(p)


def _start_shared():
    """Idempotent: start the real server + App once; every test class calls this from setUpClass."""
    if _Shared.srv is not None:
        return
    cfg = load_config()
    cfg["data_mode"] = "demo"
    brain = FakeBrain([])
    app = App(state_dir=_STATE, cfg=cfg, brain=brain, start_threads=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
    srv.handle_error = lambda request, client_address: None     # keep test output clean (see expectedFailure tests)
    server.PORT = srv.server_address[1]
    server.APP = app
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True, name="test-jarvis-server")
    t.start()
    _Shared.app, _Shared.srv, _Shared.thread, _Shared.brain, _Shared.port = app, srv, t, brain, srv.server_address[1]
    app.cycle_once(80)                                          # fleet feed, books, report.json exist from here on


def tearDownModule():
    if _Shared.srv is not None:
        _Shared.srv.shutdown()
        _Shared.srv.server_close()
        _Shared.app.stop()
        _Shared.app.tasks.pool.shutdown(wait=True, cancel_futures=True)
        _Shared.srv = None
    while _PATCHES:
        _PATCHES.pop().stop()
    for k, v in _ENV_BEFORE.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(_STATE, ignore_errors=True)


# ---------------------------------------------------------------- HTTP helpers
class Resp(SimpleNamespace):
    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def header(self, name, default=None):
        return self.headers.get(name, default) if self.headers is not None else default


def req(method: str, path: str, body=None, *, token=True, host=None, origin=None, headers=None, raw: bytes | None = None,
        timeout: float = 60) -> Resp:
    """One real HTTP request to the test server. token=True sends the right token, a str sends that, False sends none.
    Returns status None if the server dropped the connection without answering."""
    c = http.client.HTTPConnection("127.0.0.1", _Shared.port, timeout=timeout)
    h = {"Host": host or f"127.0.0.1:{_Shared.port}"}
    if token is True:
        h["X-Jarvis-Token"] = server.TOKEN
    elif isinstance(token, str):
        h["X-Jarvis-Token"] = token
    if origin:
        h["Origin"] = origin
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    try:
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        return Resp(status=r.status, headers=r.headers, body=r.read())
    except (http.client.RemoteDisconnected, ConnectionError, http.client.BadStatusLine):
        return Resp(status=None, headers=None, body=b"")
    finally:
        c.close()


def get(path, **kw) -> Resp:
    return req("GET", path, **kw)


def post(path, body=None, **kw) -> Resp:
    return req("POST", path, body if body is not None else {}, **kw)


def wait_for(pred, timeout: float = 10.0, step: float = 0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


def text_resp(text, stop="end_turn"):
    return {"content": [{"type": "text", "text": text}], "stop_reason": stop}


def tool_resp(name, args, tid="toolu_01"):
    return {"content": [{"type": "text", "text": "Checking."},
                        {"type": "tool_use", "id": tid, "name": name, "input": args}], "stop_reason": "tool_use"}


def twilio_sig(token: str, url: str, params: dict) -> str:
    """Independent implementation of Twilio's request signature (HMAC-SHA1 over url + sorted key/value pairs)."""
    s = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), s.encode(), hashlib.sha1).digest()).decode()


@contextlib.contextmanager
def isolated_app(brain=None, share_data=True):
    """A throwaway App (own temp state folder) swapped in as server.APP for risk-cutting tests. It shares the
    shared app's DataService (bars cache) so a fleet cycle is quick. server.APP is always restored."""
    d = tempfile.mkdtemp(prefix="jarvis_server_iso_")
    cfg = json.loads(json.dumps(_Shared.app.cfg))
    app = App(state_dir=d, cfg=cfg, brain=brain if brain is not None else FakeBrain([]), start_threads=False)
    if share_data:
        app.data = _Shared.app.data
        app.fleet.data = _Shared.app.data
    old = server.APP
    server.APP = app
    try:
        yield app
    finally:
        server.APP = old
        app.stop()
        app.tasks.pool.shutdown(wait=True, cancel_futures=True)
        shutil.rmtree(d, ignore_errors=True)


class ServerBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _start_shared()

    @property
    def app(self) -> App:
        return _Shared.app

    @property
    def brain(self) -> FakeBrain:
        return _Shared.brain

    def setUp(self):
        self.assertIs(server.APP, _Shared.app, "a previous test left a different App in server.APP")
        self.brain.script.clear()
        self.brain.calls.clear()

    def assertForbidden(self, r: Resp):
        self.assertEqual(r.status, 403, r.body[:200])
        self.assertNotIn(server.TOKEN.encode(), r.body)

    def ok_data(self, r: Resp) -> dict:
        self.assertEqual(r.status, 200, r.body[:300])
        j = r.json()
        self.assertTrue(j.get("ok"), j)
        return j["data"]


# ================================================================ page, static files, sites
class TestPage(ServerBase):
    def test_index_served_with_token_and_csp(self):
        r = get("/", token=False)
        self.assertEqual(r.status, 200)
        self.assertTrue(r.header("Content-Type", "").startswith("text/html"))
        html = r.body.decode("utf-8")
        self.assertIn(server.TOKEN, html)
        self.assertNotIn("{{JARVIS_TOKEN}}", html)
        csp = r.header("Content-Security-Policy")
        self.assertTrue(csp, "GET / must send a Content-Security-Policy header")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("connect-src 'self'", csp)                  # the page can only talk back to this server
        self.assertEqual(r.header("Cache-Control"), "no-store")   # the token page is never cached
        self.assertEqual(r.header("X-Content-Type-Options"), "nosniff")

    def test_index_html_alias_and_localhost_host(self):
        for path, host in (("/index.html", None), ("/", f"localhost:{_Shared.port}")):
            r = get(path, token=False, host=host)
            self.assertEqual(r.status, 200, (path, host))
            self.assertIn(server.TOKEN.encode(), r.body)

    def test_index_foreign_host_403(self):
        for host in ("evil.example", f"evil.example:{_Shared.port}", "127.0.0.1", f"127.0.0.1:{_Shared.port + 1}",
                     f"LOCALHOST.:{_Shared.port}", f"0.0.0.0:{_Shared.port}"):
            r = get("/", token=False, host=host)
            self.assertForbidden(r)

    def test_token_injection_with_controlled_page(self):
        web = tempfile.mkdtemp(prefix="jarvis_web_")
        self.addCleanup(shutil.rmtree, web, True)
        with open(os.path.join(web, "index.html"), "w", encoding="utf-8") as f:
            f.write('<meta name="jarvis-token" content="{{JARVIS_TOKEN}}"><script>var t="{{JARVIS_TOKEN}}"</script>')
        with open(os.path.join(web, "app.js"), "w", encoding="utf-8") as f:
            f.write("console.log('{{JARVIS_TOKEN}}')")
        with mock.patch.object(server, "WEB", web):
            r = get("/", token=False)
            self.assertEqual(r.status, 200)
            self.assertEqual(r.body.decode().count(server.TOKEN), 2)     # every placeholder replaced
            self.assertNotIn(b"{{JARVIS_TOKEN}}", r.body)
            s = get("/static/app.js", token=False)                       # static files are served raw, never with the token
            self.assertEqual(s.status, 200)
            self.assertTrue(s.header("Content-Type", "").startswith("text/javascript"))
            self.assertNotIn(server.TOKEN.encode(), s.body)

    def test_static_files_never_carry_the_token(self):
        for name in ("index.html", "app.js", "app.css"):
            if not os.path.isfile(os.path.join(server.WEB, name)):
                continue
            r = get("/static/" + name, token=False)
            self.assertEqual(r.status, 200, name)
            self.assertNotIn(server.TOKEN.encode(), r.body, name)

    def test_static_dotdot_and_missing_rejected(self):
        for path in ("/static/../server.py", "/static/../../etc/passwd", "/static/%2e%2e/server.py", "/static/nope.js",
                     "/static/", "/static/../state/HALT"):
            r = get(path, token=False)
            self.assertEqual(r.status, 404, path)
            self.assertNotIn(b"import", r.body)

    @unittest.expectedFailure
    def test_static_absolute_path_cannot_escape_web_dir(self):
        # SOURCE BUG: server.py do_GET /static/ - name = os.path.normpath(p[len("/static/"):]) keeps a leading "/" for
        # "/static//etc/passwd", and os.path.join(WEB, "/etc/passwd") DISCARDS WEB, so any absolute path on the
        # machine is served (no token, no Host check -> also readable through DNS rebinding). On Windows the same
        # happens with "/static/C:/Users/...". Fix: resolve with realpath and require the result to stay inside WEB,
        # and add the Host check (see source_bugs).
        outside = tempfile.mkdtemp(prefix="jarvis_secret_")
        self.addCleanup(shutil.rmtree, outside, True)
        secret = os.path.join(outside, "secret.txt")
        with open(secret, "w") as f:
            f.write("TOP-SECRET-FILE-CONTENT")
        for host in (None, "evil.example"):
            r = get("/static/" + secret, token=False, host=host)        # -> "/static//tmp/.../secret.txt"
            self.assertNotIn(b"TOP-SECRET-FILE-CONTENT", r.body, f"file outside web/ was served (host={host})")
            self.assertIn(r.status, (403, 404))

    @unittest.expectedFailure
    def test_index_cannot_be_framed(self):
        # SOURCE BUG: server.py GET / sends a CSP without frame-ancestors and no X-Frame-Options, so any web page the
        # owner visits can load the portal in an invisible iframe (the browser sends Host 127.0.0.1:PORT, so the Host
        # check passes) and click-jack the HALT / PAUSE / memory-delete buttons. HALT can only be cleared by hand.
        # Fix: add "frame-ancestors 'none'" to the CSP (and "X-Frame-Options: DENY").
        r = get("/", token=False)
        self.assertEqual(r.status, 200)
        csp = r.header("Content-Security-Policy", "")
        xfo = (r.header("X-Frame-Options") or "").upper()
        self.assertTrue("frame-ancestors 'none'" in csp or xfo in ("DENY", "SAMEORIGIN"), csp)

    def test_sites_served_sandboxed_and_host_checked(self):
        res = self.app.gateway.call("build_website", {"title": "Server Test Site", "html": "<html><body>hello site</body></html>"},
                                    {"id": "t", "channel": "gui", "auth_level": "owner_operate"})
        self.assertTrue(res["ok"], res)
        url = res["data"]["saved"]
        self.assertEqual(url, "/sites/server-test-site.html")
        r = get(url, token=False)
        self.assertEqual(r.status, 200)
        self.assertIn(b"hello site", r.body)
        self.assertIn("sandbox", r.header("Content-Security-Policy", ""))   # opaque origin: cannot call the API
        self.assertNotIn(server.TOKEN.encode(), r.body)
        self.assertForbidden(get(url, token=False, host="evil.example"))
        for bad in ("/sites/missing.html", "/sites/../jarvis.db", "/sites/../../server.py", "/sites/jarvis.db"):
            self.assertEqual(get(bad, token=False).status, 404, bad)
        st = get("/api/state?ui=0").json()
        self.assertTrue(any(e.get("type") == "open_url" and e.get("url") == url for e in st["ui_events"]))


# ================================================================ authentication: token, Host, Origin
class TestAuth(ServerBase):
    def test_health_needs_no_token_and_leaks_nothing(self):
        r = get("/api/health", token=False)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json(), {"ok": True, "paper_only": True})
        self.assertNotIn(server.TOKEN.encode(), r.body)

    def test_state_requires_token(self):
        self.assertForbidden(get("/api/state", token=False))
        self.assertForbidden(get("/api/state", token="wrong-token"))
        self.assertForbidden(get("/api/state", token=""))
        self.assertForbidden(get("/api/state", token=server.TOKEN[:-1]))
        self.assertForbidden(get("/api/state", token=server.TOKEN + "x"))
        r = get("/api/state")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.header("Content-Type"), "application/json")

    def test_token_is_random_and_long(self):
        self.assertGreaterEqual(len(server.TOKEN), 32)
        self.assertNotIn(server.TOKEN, open(os.path.join(server.WEB, "index.html"), encoding="utf-8").read())

    def test_token_only_accepted_in_header(self):
        self.assertForbidden(get(f"/api/state?token={server.TOKEN}", token=False))
        self.assertForbidden(get("/api/state", token=False, headers={"Authorization": f"Bearer {server.TOKEN}"}))
        self.assertForbidden(get("/api/state", token=False, headers={"Cookie": f"X-Jarvis-Token={server.TOKEN}"}))

    def test_every_api_endpoint_needs_token(self):
        gets = ["/api/state", "/api/bars?symbol=BTC-USD&bar=1h", "/api/fleet", "/api/strategies", "/api/task?id=x",
                "/api/tasks", "/api/alerts", "/api/memory", "/api/memory/export", "/api/activity", "/api/vol"]
        posts = ["/api/ask", "/api/backtest", "/api/role_test", "/api/alerts", "/api/alerts/disable", "/api/memory",
                 "/api/memory/delete", "/api/memory/correct", "/api/halt", "/api/pause", "/api/task/cancel", "/api/cycle"]
        for p in gets:
            self.assertForbidden(get(p, token=False))
        for p in posts:
            self.assertForbidden(post(p, {"text": "status", "bot_id": "A073", "reason": "x"}, token=False))
        self.assertFalse(os.path.exists(self.app.fleet.cfg.halt_file))

    def test_foreign_host_rejected_even_with_token(self):
        for host in ("evil.example", f"evil.example:{_Shared.port}", f"127.0.0.1:{_Shared.port + 1}", "127.0.0.1"):
            self.assertForbidden(get("/api/state", host=host))
            self.assertForbidden(post("/api/halt", {"reason": "rebinding"}, host=host))
        self.assertEqual(get("/api/state", host=f"localhost:{_Shared.port}").status, 200)
        self.assertFalse(os.path.exists(self.app.fleet.cfg.halt_file))

    def test_origin_checks(self):
        for o in ("http://evil.example", "null", f"http://127.0.0.1:{_Shared.port + 1}", f"https://127.0.0.1:{_Shared.port}",
                  f"http://127.0.0.1:{_Shared.port}.evil.example"):
            self.assertForbidden(get("/api/state", origin=o))
        for o in (f"http://127.0.0.1:{_Shared.port}", f"http://localhost:{_Shared.port}"):
            self.assertEqual(get("/api/state", origin=o).status, 200, o)

    def test_unknown_paths(self):
        self.assertForbidden(get("/api/nope", token=False))
        self.assertEqual(get("/api/nope").status, 404)
        self.assertEqual(post("/api/nope").status, 404)
        self.assertEqual(get("/nope", token=False).status, 404)
        self.assertEqual(get("/phone/voice", token=False).status, 404)

    def test_api_security_headers_and_no_cors(self):
        for r in (get("/api/state"), post("/api/ask", {"text": ""}), get("/api/health", token=False)):
            self.assertEqual(r.status, 200)
            self.assertEqual(r.header("Cache-Control"), "no-store")
            self.assertEqual(r.header("X-Content-Type-Options"), "nosniff")
            self.assertEqual(r.header("Referrer-Policy"), "no-referrer")
            self.assertIsNone(r.header("Access-Control-Allow-Origin"))          # no CORS: other sites cannot read replies
            self.assertNotIn(server.TOKEN.encode(), r.body)

    def test_cross_site_form_post_cannot_halt(self):
        raw = urlencode({"reason": "csrf"}).encode()
        r = req("POST", "/api/halt", raw=raw, token=False, origin="http://evil.example",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertForbidden(r)
        self.assertFalse(os.path.exists(self.app.fleet.cfg.halt_file))

    def test_bad_json_is_400(self):
        r = post("/api/ask", raw=b"{not json")
        self.assertEqual(r.status, 400)
        self.assertEqual(r.json(), {"error": "bad json"})

    def test_unsupported_methods(self):
        for m in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(req(m, "/api/halt", {}).status, 501, m)

    @unittest.expectedFailure
    def test_non_object_json_body_is_400(self):
        # SOURCE BUG: server.py do_POST - _body() returns whatever json.loads gives ([], "x", 5); the handlers then call
        # body.get(...) and the AttributeError becomes a 500 that echoes the Python exception. Fix: in do_POST,
        # `if not isinstance(body, dict): return self._send(400, {"error": "body must be a JSON object"})`.
        for raw in (b"[1, 2]", b'"halt"', b"5", b"null"):
            r = post("/api/ask", raw=raw)
            self.assertEqual(r.status, 400, (raw, r.body[:200]))

    @unittest.expectedFailure
    def test_non_numeric_content_length_is_400(self):
        # SOURCE BUG: server.py _body() does int(self.headers.get("Content-Length")) OUTSIDE the try block of do_POST,
        # so "Content-Length: abc" raises ValueError in the handler and the connection is dropped with no response.
        # Fix: wrap the int() in try/except in _body() and return None (-> 400 "bad json"); also reject n < 0.
        r = post("/api/memory", raw=b"{}", headers={"Content-Length": "abc"})
        self.assertEqual(r.status, 400)

    @unittest.expectedFailure
    def test_non_ascii_token_header_is_403(self):
        # SOURCE BUG: server.py _auth() calls secrets.compare_digest(header_str, TOKEN); for a header with a non-ASCII
        # character (latin-1 decoded by http.server) compare_digest raises TypeError, the handler crashes and the
        # client gets no response instead of 403 (still fails closed). Fix: compare bytes:
        # secrets.compare_digest(self.headers.get("X-Jarvis-Token", "").encode("utf-8", "surrogateescape"), TOKEN.encode()).
        r = get("/api/state", token="\u00e9" + server.TOKEN[1:])
        self.assertEqual(r.status, 403)


# ================================================================ state, fleet, bars, strategies, vol, activity
class TestStateAndData(ServerBase):
    def test_state_dir_is_temporary(self):
        self.assertEqual(os.path.realpath(self.app.state_dir), os.path.realpath(_STATE))
        self.assertEqual(os.path.realpath(self.app.fleet.cfg.state_dir), os.path.realpath(_STATE))
        self.assertNotEqual(os.path.realpath(self.app.state_dir), REPO_STATE)
        self.assertEqual(self.app.data.mode, "demo")
        for f in ("fleet.json", "report.json", "jarvis.db"):
            self.assertTrue(os.path.exists(os.path.join(_STATE, f)), f)

    def test_state_payload(self):
        r = get("/api/state")
        self.assertEqual(r.status, 200)
        s = r.json()
        for k in ("portfolio", "fleet", "totals", "quotes", "halt", "paper_only", "brain", "limits", "positions", "paused",
                  "data_mode", "phone", "tasks", "notifications", "ui_events", "events", "shadow", "gate_results", "tries"):
            self.assertIn(k, s)
        self.assertIs(s["paper_only"], True)
        self.assertIs(s["halt"], False)
        self.assertEqual(s["data_mode"], "demo")
        self.assertEqual(len(s["fleet"]), 74)                                  # 99 bots minus 25 PARKED
        self.assertEqual({r["bot_id"] for r in s["fleet"]},
                         {b["bot_id"] for b in library.bots() if b["status"] != "PARKED"})
        self.assertIs(s["totals"]["paper_only"], True)
        self.assertEqual(s["totals"]["active"], 74)
        self.assertGreaterEqual(s["cycles"], 1)
        self.assertEqual(s["brain"]["provider"], "fake")
        self.assertTrue(s["brain"]["ready"])
        self.assertEqual(s["phone"]["state"], "SETUP_REQUIRED")                # phone is off by default
        self.assertEqual({q["symbol"] for q in s["quotes"]}, set(self.app.cfg["watchlist"]))
        lim = s["limits"]
        for k in ("max_open_stock", "max_open_crypto", "max_trades_per_day", "day_loss_pct", "week_loss_pct", "cost_ok_r", "cost_max_r"):
            self.assertIn(k, lim)
        self.assertEqual((lim["cost_ok_r"], lim["cost_max_r"]), (0.20, 0.33))  # the cost rule is not loosened
        for k in ("equity", "pnl_today", "open_positions"):
            self.assertIn(k, s["portfolio"])

    def test_state_fail_closed_every_answer_is_wait(self):
        s = get("/api/state").json()
        for row in s["fleet"]:
            self.assertEqual(row["decision"], "WAIT", row)                    # no role has passed the gate
            self.assertIn(row["last_signal"], ("LONG", "NONE"))
        self.assertEqual(s["portfolio"]["open_positions"], 0)                  # nothing reached the paper book
        self.assertEqual(s["positions"], [])
        self.assertEqual(s["totals"]["buy_answers"], 0)
        self.assertEqual(s["gate_results"], {})

    def test_state_bad_query_numbers_do_not_kill_server(self):
        r = get("/api/state?ui=abc")
        self.assertIn(r.status, (400, 500))
        self.assertIn("error", r.json())
        self.assertEqual(get("/api/state?ui=999999&note=999999").status, 200)

    def test_fleet_endpoint(self):
        r = get("/api/fleet")
        self.assertEqual(r.status, 200)
        j = r.json()
        self.assertEqual(len(j["rows"]), 74)
        for row in j["rows"]:
            self.assertTrue(row.get("correlation_id"), row["bot_id"])
            self.assertTrue(row.get("heartbeat_time") or row.get("error"), row["bot_id"])
            self.assertEqual(row["decision"], "WAIT")
        self.assertIs(j["totals"]["paper_only"], True)

    def test_bars_btc_1h(self):
        r = get("/api/bars?symbol=BTC-USD&bar=1h")
        self.assertEqual(r.status, 200)
        b = r.json()
        self.assertTrue(b["ok"], b)
        self.assertEqual((b["symbol"], b["bar"]), ("BTC-USD", "1h"))
        self.assertIn("DEMO", b["source"])                                      # simulated prices are labelled
        self.assertIn("NOT real", b["note"])
        n = len(b["t"])
        self.assertEqual(n, 240)                                               # default n
        for k in ("o", "h", "l", "c", "v", "ema20", "ema50", "vwap"):
            self.assertEqual(len(b[k]), n, k)
        self.assertEqual(b["t"], sorted(b["t"]))
        for o, h, l, c in zip(b["o"], b["h"], b["l"], b["c"]):
            self.assertLessEqual(l, min(o, c) + 1e-9)
            self.assertGreaterEqual(h, max(o, c) - 1e-9)
        self.assertGreater(b["atr"], 0)
        self.assertIsInstance(b["signals"], list)

    def test_bars_symbol_case_and_n(self):
        b = get("/api/bars?symbol=eth-usd&bar=1h&n=50").json()
        self.assertTrue(b["ok"])
        self.assertEqual(b["symbol"], "ETH-USD")
        self.assertEqual(len(b["c"]), 50)
        big = get("/api/bars?symbol=BTC-USD&bar=1h&n=5000").json()
        self.assertTrue(big["ok"])
        self.assertEqual(len(big["t"]), 1500)                                 # capped
        dflt = get("/api/bars?bar=").json()                                    # empty/missing params -> BTC-USD 1h
        self.assertTrue(dflt["ok"])
        self.assertEqual((dflt["symbol"], dflt["bar"]), ("BTC-USD", "1h"))

    def test_bars_bad_bar(self):
        for bar in ("2h", "1w", "1H", "1h;rm", "%00"):
            r = get(f"/api/bars?symbol=BTC-USD&bar={bar}")
            self.assertEqual(r.status, 200)
            self.assertEqual(r.json(), {"ok": False, "error": "bad bar"})

    def test_bars_strategy_marks_are_bar_times(self):
        b = get("/api/bars?symbol=BTC-USD&bar=1h&n=400&strategy=donchian_trend.adx.trail").json()
        self.assertTrue(b["ok"])
        self.assertTrue(set(b["signals"]) <= set(b["t"]))
        unknown = get("/api/bars?symbol=BTC-USD&bar=1h&strategy=not_a_strategy").json()
        self.assertEqual(unknown["signals"], [])

    def test_bars_non_numeric_n_is_json_error(self):
        r = get("/api/bars?symbol=BTC-USD&bar=1h&n=abc")
        self.assertIn(r.status, (400, 500))
        self.assertIn("error", r.json())
        self.assertEqual(get("/api/health", token=False).status, 200)         # server still alive

    @unittest.expectedFailure
    def test_bars_non_positive_n(self):
        # SOURCE BUG: server.py bars_payload() - n = min(int(n), 1500) has no lower bound, and slice(-n, None) with
        # n=0 is slice(0, None) (ALL 400 bars) and with n=-3 is slice(3, None) (397 bars). Fix:
        # n = max(1, min(int(...), 1500)) (or return {"ok": False, "error": "bad n"} for n < 1).
        for n in ("0", "-3"):
            b = get(f"/api/bars?symbol=BTC-USD&bar=1h&n={n}").json()
            self.assertTrue((not b.get("ok")) or len(b["t"]) <= 1, (n, len(b.get("t", []))))

    def test_strategies(self):
        r = get("/api/strategies")
        self.assertEqual(r.status, 200)
        j = r.json()
        self.assertEqual(len(j["keep"]), 100)
        self.assertEqual(len(j["new"]), 11)
        self.assertEqual(len(j["bots"]), 99)
        self.assertEqual(len(j["roles"]), 7)
        for k in ("do_not_build", "measured", "day_trade_rules", "cost_in_r", "auto_pause_rules", "next_steps", "venues"):
            self.assertIn(k, j)
        self.assertEqual(set(j["bots"][0]), {"bot_id", "symbol", "role", "group", "status", "verdict", "bar", "candidate_1"})
        self.assertIn("donchian_trend.adx.trail", {s["id"] for s in j["keep"]})

    def test_vol(self):
        r = get("/api/vol?symbol=btc-usd")
        self.assertEqual(r.status, 200)
        v = r.json()
        self.assertIn(v.get("state"), ("LOUD", "NORMAL", "QUIET"), v)
        self.assertIn("DEMO", str(v.get("source")))

    def test_activity(self):
        self.app.gateway.call("get_time", {}, {"id": "activity-test", "channel": "gui", "auth_level": "owner_operate"})
        r = get("/api/activity")
        self.assertEqual(r.status, 200)
        j = r.json()
        for k in ("tool_audit", "events", "messages", "audit_chain_ok"):
            self.assertIn(k, j)
        self.assertIs(j["audit_chain_ok"], True)
        self.assertTrue(any(a["tool"] == "get_time" and a["session_id"] == "activity-test" for a in j["tool_audit"]))

    def test_cycle_endpoint(self):
        before = self.app.fleet.cycle_n
        r = post("/api/cycle")
        self.assertEqual(r.json(), {"ok": True})
        wait_for(lambda: self.app.fleet.running or self.app.fleet.cycle_n > before, 10)
        if self.app.fleet.running:
            r2 = post("/api/cycle").json()
            self.assertTrue(r2["ok"] is False or self.app.fleet.cycle_n > before, r2)
        self.assertTrue(wait_for(lambda: self.app.fleet.cycle_n > before and not self.app.fleet.running, 120),
                        "fleet cycle did not finish")
        s = get("/api/state").json()
        self.assertEqual(s["cycles"], self.app.fleet.cycle_n)
        self.assertEqual(len(s["fleet"]), 74)
        self.assertIs(s["halt"], False)


# ================================================================ /api/ask
class TestAsk(ServerBase):
    def test_ask_with_evil_origin_is_403(self):
        n_msgs = len(self.app.store.recent_messages(1000))
        r = post("/api/ask", {"text": "hello there"}, origin="http://evil.example")
        self.assertForbidden(r)
        self.assertEqual(self.brain.calls, [])
        self.assertEqual(len(self.app.store.recent_messages(1000)), n_msgs)

    def test_ask_reply_and_session(self):
        self.brain.script.append(text_resp("Hello Abhi, all systems on paper."))
        r = post("/api/ask", {"text": "hello there"})
        self.assertEqual(r.status, 200)
        j = r.json()
        self.assertEqual(j["reply"], "Hello Abhi, all systems on paper.")
        self.assertTrue(j["session_id"])
        self.assertIs(j["paper_only"], True)
        self.assertIn("speak", j)
        self.assertEqual(len(self.brain.calls), 1)

    def test_followup_keeps_session(self):
        self.brain.script += [text_resp("First answer about the moon."), text_resp("Second answer.")]
        a = post("/api/ask", {"text": "tell me about the moon"}).json()
        sid = a["session_id"]
        b = post("/api/ask", {"text": "and the sun", "session_id": sid}).json()
        self.assertEqual(b["session_id"], sid)
        self.assertEqual(b["reply"], "Second answer.")
        msgs = self.brain.calls[-1]["messages"]                               # history reached the model
        self.assertEqual(msgs[0], {"role": "user", "content": "tell me about the moon"})
        self.assertEqual(msgs[1], {"role": "assistant", "content": "First answer about the moon."})
        self.assertTrue(msgs[-1]["content"].endswith("and the sun"))
        hist = self.app.store.history(sid, 50)
        self.assertEqual([h["role"] for h in hist], ["user", "assistant", "user", "assistant"])
        c = post("/api/ask", {"text": "a new chat"}).json()                     # no session_id -> new session
        self.assertNotEqual(c["session_id"], sid)

    def test_fixed_command_bypasses_model(self):
        r = post("/api/ask", {"text": "status"}).json()
        self.assertEqual(r["route"], "command")
        self.assertEqual(r["tools"], ["command:STATUS"])
        self.assertTrue(r["reply"])
        self.assertEqual(self.brain.calls, [])

    def test_tool_loop_over_http_is_audited(self):
        self.brain.script += [tool_resp("get_portfolio", {}), text_resp("Your paper equity is fine.")]
        j = post("/api/ask", {"text": "what is my paper equity"}).json()
        self.assertEqual(j["reply"], "Your paper equity is fine.")
        self.assertEqual(j["tools"], ["get_portfolio"])
        audit = get("/api/activity").json()["tool_audit"]
        row = next(a for a in audit if a["tool"] == "get_portfolio" and a["session_id"] == j["session_id"])
        self.assertEqual(row["ok"], 1)
        self.assertEqual(row["channel"], "gui")

    def test_channel_cannot_be_forged_to_telephone(self):
        self.brain.script.append(text_resp("ok"))
        j = post("/api/ask", {"text": "hello again", "channel": "telephone"}).json()
        tools = set(self.brain.calls[-1]["tools"])
        self.assertTrue(T2_TOOLS & tools)                                       # desktop tool list, not the phone one
        msgs = self.app.store.history(j["session_id"], 10)
        self.assertTrue(msgs)
        rows = self.app.store.q("SELECT channel FROM messages WHERE session_id=?", (j["session_id"],))
        self.assertEqual({r["channel"] for r in rows}, {"gui"})

    def test_model_never_sees_order_tools(self):
        self.brain.script.append(text_resp("ok"))
        post("/api/ask", {"text": "buy me some bitcoin now"})
        for name in self.brain.calls[-1]["tools"]:
            self.assertNotRegex(name, re.compile(r"order|place|sell|^buy|resume|unhalt|clear", re.I))

    def test_empty_text(self):
        j = post("/api/ask", {"text": "   "}).json()
        self.assertEqual(j["reply"], "I didn't catch that.")
        self.assertEqual(self.brain.calls, [])

    def test_offline_brain_is_labelled_and_uses_real_data(self):
        with isolated_app(brain=AnthropicBrain({"strong": "x", "fast": "y"}, web=False)) as app:
            self.assertFalse(app.brain.ready()[0])
            j = post("/api/ask", {"text": "what's the price of BTC"}).json()
            self.assertEqual(j["route"], "offline")
            self.assertIn("SETUP_REQUIRED", j["note"])
            self.assertIn("BTC-USD last closed at", j["reply"])
            self.assertIn("DEMO", j["reply"])
            self.assertFalse(get("/api/state").json()["brain"]["ready"])


# ================================================================ backtests, role tests, tasks
class TestBacktestAndTasks(ServerBase):
    def test_backtest_task_round_trip(self):
        r = post("/api/backtest", {"strategy_id": "donchian_trend.adx.trail", "symbol": "BTC-USD", "bar": "1h"})
        d = self.ok_data(r)
        tid = d["task_id"]
        self.assertEqual(d["status"], "queued")
        end = time.time() + 120
        t = None
        while time.time() < end:
            tr = get(f"/api/task?id={tid}")
            self.assertEqual(tr.status, 200)
            t = tr.json()
            if t["status"] in ("done", "failed", "cancelled", "interrupted"):
                break
            self.assertGreaterEqual(t["progress"], 0.0)
            time.sleep(0.25)
        self.assertEqual(t["status"], "done", t.get("error"))
        res = t["result"]
        self.assertIsInstance(res["stats"], dict)
        self.assertIn("trades", res["stats"])
        self.assertIn(res["gate"]["verdict"], ("REJECT", "RESEARCH FURTHER", "PAPER TEST"))
        self.assertIsInstance(res["trades"], list)
        self.assertLessEqual(len(res["trades"]), 400)
        for tr in res["trades"]:                                              # long only, hard stop on every trade
            self.assertIsNotNone(tr["stop"])
            self.assertLess(tr["stop"], tr["entry"])
        self.assertIn("DEMO", res["source"])
        self.assertIn("NOT real", res["data_note"])
        self.assertTrue(os.path.exists(os.path.join(_STATE, "trials.json")))  # every try is counted, in the temp state
        self.assertIn(tid, [x["id"] for x in get("/api/tasks").json()["tasks"]])
        self.assertTrue(any(x["id"] == tid for x in get("/api/state").json()["tasks"]))

    def test_backtest_bad_arguments(self):
        cases = [({"strategy_id": "no_such_strategy", "symbol": "BTC-USD"}, "NOT_FOUND"),
                 ({"strategy_id": "donchian_trend.adx.trail"}, "BAD_ARGUMENTS"),
                 ({"strategy_id": "donchian_trend.adx.trail", "symbol": "BTC-USD", "bar": "4h"}, "BAD_ARGUMENTS"),
                 ({"strategy_id": "donchian_trend.adx.trail", "symbol": "btc usd"}, "BAD_ARGUMENTS"),
                 ({"strategy_id": "donchian_trend.adx.trail", "symbol": "BTC-USD", "venue": "binance_futures"}, "BAD_ARGUMENTS"),
                 ({"strategy_id": "donchian_trend.adx.trail", "symbol": "BTC-USD", "vol_gate": "no"}, "BAD_ARGUMENTS")]
        with mock.patch.object(self.app, "start_backtest", return_value="never") as sb:
            for body, code in cases:
                j = post("/api/backtest", body).json()
                self.assertFalse(j["ok"], body)
                self.assertEqual(j["error"]["code"], code, body)
            sb.assert_not_called()

    def test_backtest_forwards_only_known_fields(self):
        with mock.patch.object(self.app, "start_backtest", return_value="tid-123") as sb:
            body = {"strategy_id": "donchian_trend.adx.trail", "symbol": "ETH-USD", "bar": "15m", "vol_gate": False,
                    "leverage": 10, "side": "short", "live": True}
            d = self.ok_data(post("/api/backtest", body))
            self.assertEqual(d["task_id"], "tid-123")
            sb.assert_called_once()
            args = sb.call_args[0]
            self.assertEqual(args[:5], ("donchian_trend.adx.trail", "ETH-USD", "15m", None, False))

    @unittest.expectedFailure
    def test_symbol_pattern_rejects_trailing_newline(self):
        # SOURCE BUG: jarvis/gateway.py validate() checks patterns with re.match(p["pattern"], v); "$" also matches
        # just before a trailing "\n", so "BTC-USD\n" passes SYMBOL (and "A073\n" passes BOT) and reaches the data
        # service / file names (state/bars/"BTC-USD\n_1h.json"). Fix: use re.fullmatch(p["pattern"], v) (or end the
        # patterns with \Z instead of $).
        with mock.patch.object(self.app, "start_backtest", return_value="should-not-start") as sb:
            j = post("/api/backtest", {"strategy_id": "donchian_trend.adx.trail", "symbol": "BTC-USD\n"}).json()
            self.assertFalse(j["ok"], j)
            self.assertEqual(j["error"]["code"], "BAD_ARGUMENTS")
            sb.assert_not_called()

    def test_role_test_endpoint(self):
        j = post("/api/role_test", {"role": "Not a role"}).json()
        self.assertFalse(j["ok"])
        self.assertEqual(j["error"]["code"], "NOT_FOUND")
        role = library.roles()[0]["role"]
        with mock.patch.object(self.app, "start_role_test", return_value="role-tid") as srt:
            d = self.ok_data(post("/api/role_test", {"role": role}))
            self.assertEqual(d["task_id"], "role-tid")
            self.assertEqual(srt.call_args[0][0], role)

    def test_task_unknown_and_cancel(self):
        r = get("/api/task?id=nope")
        self.assertEqual(r.status, 404)
        self.assertEqual(get("/api/task").status, 404)
        self.assertEqual(post("/api/task/cancel", {"id": "nope"}).json(), {"ok": False})
        started = threading.Event()

        def slow(progress, cancel):
            started.set()
            for i in range(600):
                if cancel.wait(0.05):
                    raise InterruptedError()
                progress(i / 600, "working")
            return {"never": True}
        tid = self.app.tasks.submit("backtest", "slow test job", slow)
        self.assertTrue(started.wait(10))
        self.assertEqual(post("/api/task/cancel", {"id": tid}).json(), {"ok": True})
        t = wait_for(lambda: (lambda x: x if x["status"] == "cancelled" else None)(get(f"/api/task?id={tid}").json()), 15)
        self.assertTrue(t, "task was not cancelled")
        self.assertIsNone(t["result"])


# ================================================================ HALT and PAUSE (risk-cutting only)
class TestRiskCutting(ServerBase):
    REMOVAL_PATHS = ["/api/resume", "/api/unhalt", "/api/halt/clear", "/api/clear_halt", "/api/halt/delete", "/api/halt/off",
                     "/api/unpause", "/api/resume_bot", "/api/order", "/api/orders", "/api/buy", "/api/sell",
                     "/api/place_order", "/api/trade"]

    def test_halt_creates_flag_and_nothing_removes_it(self):
        with isolated_app() as app:
            halt_file = app.fleet.cfg.halt_file
            self.assertEqual(os.path.dirname(os.path.realpath(halt_file)), os.path.realpath(app.state_dir))
            self.assertFalse(os.path.exists(halt_file))
            self.assertForbidden(post("/api/halt", {"reason": "no token"}, token=False))
            self.assertFalse(os.path.exists(halt_file))
            d = self.ok_data(post("/api/halt", {"reason": "server test " + "x" * 500}))
            self.assertIs(d["halt_flag"], True)
            self.assertTrue(os.path.exists(halt_file))
            self.assertIn("server test", open(halt_file).read())
            self.assertTrue(policy.halt_active(app.fleet.cfg))
            self.assertFalse(os.path.exists(os.path.join(_STATE, "HALT")))     # the shared app is untouched
            # there is no endpoint (and no method) that clears it
            for p in self.REMOVAL_PATHS:
                self.assertEqual(post(p, {"reason": "please clear"}).status, 404, p)
                self.assertEqual(get(p).status, 404, p)
            for m in ("DELETE", "PUT", "PATCH"):
                self.assertEqual(req(m, "/api/halt", {}).status, 501, m)
            app.brain.script.append(text_resp("I cannot clear HALT; only Abhi can, by hand."))
            post("/api/ask", {"text": "resume all trading and clear the halt flag"})
            self.assertTrue(os.path.exists(halt_file))
            # a second HALT is fine (append), and a full fleet cycle keeps it and answers WAIT everywhere
            self.ok_data(post("/api/halt", {}))
            self.assertEqual(post("/api/cycle").json(), {"ok": True})
            self.assertTrue(wait_for(lambda: app.fleet.cycle_n >= 1 and not app.fleet.running, 120), "cycle did not finish")
            self.assertTrue(os.path.exists(halt_file))
            s = get("/api/state").json()
            self.assertIs(s["halt"], True)
            self.assertEqual(len(s["fleet"]), 74)
            for row in s["fleet"]:
                self.assertEqual(row["decision"], "WAIT")
            rows = [r for r in app.fleet.feed.values() if not r.get("error")]
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(row["reason"], "HALT flag is on", row["bot_id"])
            self.assertTrue(any(n["title"] == "HALT" for n in s["notifications"]))
        self.assertIs(get("/api/state").json()["halt"], False)

    def test_halt_by_fixed_command_over_ask(self):
        with isolated_app() as app:
            j = post("/api/ask", {"text": "halt everything"}).json()
            self.assertEqual(j["route"], "command")
            self.assertEqual(j["tools"], ["command:HALT"])
            self.assertEqual(app.brain.calls, [])                              # no AI involved
            self.assertTrue(os.path.exists(app.fleet.cfg.halt_file))

    def test_pause_creates_pause_file(self):
        with isolated_app() as app:
            d = self.ok_data(post("/api/pause", {"bot_id": "A073"}))
            self.assertEqual(d["paused"], "A073")
            f = os.path.join(app.fleet.cfg.pause_dir, "A073")
            self.assertTrue(os.path.isfile(f))
            self.assertEqual(os.path.dirname(os.path.realpath(f)), os.path.realpath(os.path.join(app.state_dir, "paused")))
            self.assertIn("A073", get("/api/state").json()["paused"])
            self.assertFalse(os.path.exists(app.fleet.cfg.halt_file))          # pausing one bot is not a HALT

    def test_pause_rejects_invalid_ids(self):
        with isolated_app() as app:
            for bad in ("A73", "B073", "a073", "A0734", "../HALT", "A073/../../x", "", " A073", "A07 3", 73, None):
                j = post("/api/pause", {"bot_id": bad}).json()
                self.assertFalse(j["ok"], bad)
                self.assertEqual(j["error"]["code"], "BAD_ARGUMENTS", bad)
            j = post("/api/pause", {"bot_id": "A999"}).json()                  # right shape, not a real bot
            self.assertFalse(j["ok"])
            self.assertEqual(j["error"]["code"], "NOT_FOUND")
            j = post("/api/pause", {}).json()
            self.assertFalse(j["ok"])
            pdir = app.fleet.cfg.pause_dir
            self.assertEqual(os.listdir(pdir) if os.path.isdir(pdir) else [], [])
            self.assertFalse(os.path.exists(app.fleet.cfg.halt_file))
            self.assertEqual(policy.paused_bots(app.fleet.cfg), set())

    def test_no_route_or_tool_can_clear_halt_or_place_orders(self):
        src = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
        routes = set(re.findall(r'p == "(/api/[^"]+)"', src))
        self.assertIn("/api/halt", routes)
        self.assertIn("/api/pause", routes)
        for r in routes:
            self.assertNotRegex(r, FORBIDDEN_WORDS, r)
        for name in self.app.gateway.tools:
            self.assertNotRegex(name, FORBIDDEN_WORDS, name)
        self.assertNotRegex(src, re.compile(r"os\.remove\([^)]*halt|unlink\([^)]*halt|rmtree\([^)]*paus", re.I))


# ================================================================ memory and alerts
class TestMemoryAndAlerts(ServerBase):
    def test_memory_round_trip(self):
        text = "I prefer short answers in the morning (server test)"
        d = self.ok_data(post("/api/memory", {"text": text, "kind": "preference"}))
        mid = d["memory_id"]
        mems = get("/api/memory").json()["memories"]
        m = next(x for x in mems if x["id"] == mid)
        self.assertEqual((m["text"], m["kind"]), (text, "preference"))
        self.assertIn("gui", m["source"])
        exp = get("/api/memory/export")
        self.assertEqual(exp.status, 200)
        self.assertIn("attachment", exp.header("Content-Disposition", ""))
        self.assertIn(mid, [x["id"] for x in exp.json()["memories"]])
        self.assertEqual(post("/api/memory/correct", {"id": mid, "text": "I prefer very short answers"}).json(), {"ok": True})
        m = next(x for x in get("/api/memory").json()["memories"] if x["id"] == mid)
        self.assertEqual((m["text"], m["corrected"]), ("I prefer very short answers", 1))
        d = self.ok_data(post("/api/memory/delete", {"id": mid}))
        self.assertEqual(d["forgotten"], mid)
        self.assertNotIn(mid, [x["id"] for x in get("/api/memory").json()["memories"]])
        audit = get("/api/activity").json()["tool_audit"]
        self.assertTrue(any(a["tool"] == "remember" and a["session_id"] == "ui" for a in audit))
        self.assertTrue(any(a["tool"] == "forget_memory" and a["session_id"] == "ui" for a in audit))

    def test_memory_rejects_bad_input(self):
        before = len(get("/api/memory").json()["memories"])
        for body in ({"text": "x", "kind": "secret"}, {"text": "y" * 501}, {"text": "z", "kind": None}):
            j = post("/api/memory", body).json()
            self.assertFalse(j["ok"], body)
            self.assertEqual(j["error"]["code"], "BAD_ARGUMENTS")
        self.assertEqual(len(get("/api/memory").json()["memories"]), before)

    @unittest.expectedFailure
    def test_memory_correct_unknown_id_is_not_ok(self):
        # SOURCE BUG: server.py POST /api/memory/correct ignores the result of store.correct_memory() and always
        # answers {"ok": true}; correct_memory() itself returns `self.x(...) is not None`, which is always True for an
        # UPDATE. It also bypasses the gateway (no tier check, no tool_audit row), unlike /api/memory and
        # /api/memory/delete. Fix: in jarvis/memory.py return cur.rowcount == 1 from the UPDATE, and in server.py
        # `ok = a.store.correct_memory(...); return self._send(200 if ok else 404, {"ok": bool(ok)})`.
        j = post("/api/memory/correct", {"id": "no-such-memory", "text": "rewritten"}).json()
        self.assertFalse(j["ok"])

    def test_alert_scheduled_reminder_create_list_disable(self):
        day = (datetime.now(MT) + timedelta(days=40)).date().isoformat()
        cond = {"kind": "scheduled", "local_time": "09:00", "local_date": day, "recurrence": "once",
                "message": "Check the BTC role test (server test)"}
        d = self.ok_data(post("/api/alerts", {"condition": cond, "channels": ["desktop"], "label": "Server test reminder",
                                              "severity": "info", "extra": "dropped by server"}))
        rid = d["rule_id"]
        self.assertIn("09:00", d["readback"])
        self.assertIn(day, d["readback"])
        self.assertIn("Check the BTC role test", d["readback"])
        j = get("/api/alerts").json()
        self.assertIn("phone", j)
        rule = next(r for r in j["rules"] if r["id"] == rid)
        self.assertTrue(rule["enabled"])
        self.assertEqual(rule["condition"]["kind"], "scheduled")
        self.assertEqual(rule["label"], "Server test reminder")
        self.assertEqual(rule["channels"], ["desktop"])
        self.assertIn("Remind by desktop at 09:00", rule["text"])
        self.ok_data(post("/api/alerts/disable", {"id": rid}))
        rule = next(r for r in get("/api/alerts").json()["rules"] if r["id"] == rid)
        self.assertFalse(rule["enabled"])

    def test_alert_fires_by_code_and_notifies(self):
        day = (datetime.now(MT) + timedelta(days=30)).date()
        cond = {"kind": "scheduled", "local_time": "12:00", "local_date": day.isoformat(), "recurrence": "once",
                "message": "Server test: lunch reminder"}
        rid = self.ok_data(post("/api/alerts", {"condition": cond, "label": "Lunch (server test)"}))["rule_id"]
        at = datetime(day.year, day.month, day.day, 12, 1, tzinfo=MT).timestamp()
        fired = self.app.alerts.evaluate(now=at)
        self.assertIn(rid, [f["rule"] for f in fired])
        notes = get("/api/state?note=0").json()["notifications"]
        self.assertTrue(any(n["title"] == "Lunch (server test)" and n["body"] == "Server test: lunch reminder" for n in notes))
        rule = next(r for r in get("/api/alerts").json()["rules"] if r["id"] == rid)
        self.assertFalse(rule["enabled"])                                      # a one-off reminder fires once

    def test_alert_rejects_bad_rules(self):
        n = len(get("/api/alerts").json()["rules"])
        bad = [{},
               {"condition": {"kind": "scheduled", "local_time": "25:99", "local_date": "2026-12-01", "recurrence": "once"}},
               {"condition": {"kind": "scheduled", "local_time": "09:00", "recurrence": "once"}},
               {"condition": {"kind": "python_eval", "expr": "__import__('os')"}},
               {"condition": {"kind": "metric_threshold", "metric": "price:BTC-USD", "operator": "==", "threshold": "1"}},
               {"condition": {"kind": "any_signal"}, "channels": ["sms:+15550000000"]},
               {"condition": {"kind": "any_signal"}, "severity": "apocalyptic"}]
        for body in bad:
            j = post("/api/alerts", body).json()
            self.assertFalse(j["ok"], body)
        self.assertEqual(len(get("/api/alerts").json()["rules"]), n)


# ================================================================ Twilio phone webhooks
class TestPhone(ServerBase):
    TOKEN = "tok_test_server"
    BASE = "https://jarvis.example"

    def phone(self, path, params, sig=None):
        raw = urlencode(params).encode()
        s = twilio_sig(self.TOKEN, self.BASE + path, params) if sig is None else sig
        return req("POST", path, raw=raw, token=False, host="jarvis.example",
                   headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": s})

    def test_phone_disabled_by_default(self):
        for p in ("/phone/voice", "/phone/pin", "/phone/speech"):
            r = self.phone(p, {"CallSid": "CA1"})
            self.assertEqual(r.status, 404, p)
            self.assertEqual(r.json(), {"error": "phone not enabled"})

    def test_phone_needs_flag_twilio_token_and_pin(self):
        with isolated_app() as app:
            with mock.patch.dict(os.environ, {"JARVIS_PHONE_INBOUND": "1"}):
                self.assertEqual(self.phone("/phone/voice", {"CallSid": "CA1"}).status, 404)     # no Twilio token
            with mock.patch.dict(os.environ, {"JARVIS_PHONE_INBOUND": "1", "TWILIO_AUTH_TOKEN": self.TOKEN}):
                self.assertEqual(self.phone("/phone/voice", {"CallSid": "CA1"}).status, 404)     # no PIN set
            app.pin.set_pin("4321")
            with mock.patch.dict(os.environ, {"JARVIS_PHONE_INBOUND": "0", "TWILIO_AUTH_TOKEN": self.TOKEN}):
                self.assertEqual(self.phone("/phone/voice", {"CallSid": "CA1"}).status, 404)     # flag off

    def test_phone_signed_flow_pin_then_speech(self):
        env = {"JARVIS_PHONE_INBOUND": "1", "TWILIO_AUTH_TOKEN": self.TOKEN, "JARVIS_PUBLIC_URL": self.BASE + "/"}
        with isolated_app() as app, mock.patch.dict(os.environ, env):
            app.pin.set_pin("4321")
            # bad / missing / wrong-key signatures are refused
            self.assertEqual(self.phone("/phone/voice", {"CallSid": "CA1"}, sig="bogus").status, 403)
            self.assertEqual(self.phone("/phone/voice", {"CallSid": "CA1"}, sig="").status, 403)
            wrong = twilio_sig("other-token", self.BASE + "/phone/voice", {"CallSid": "CA1"})
            self.assertEqual(self.phone("/phone/voice", {"CallSid": "CA1"}, sig=wrong).status, 403)
            # an unauthenticated caller gets nothing
            r = self.phone("/phone/speech", {"CallSid": "CA9", "SpeechResult": "halt"})
            self.assertEqual(r.status, 200)
            self.assertIn(b"authenticate", r.body)
            self.assertFalse(os.path.exists(app.fleet.cfg.halt_file))
            # signed call: PIN prompt -> wrong PIN -> right PIN -> speech
            r = self.phone("/phone/voice", {"CallSid": "CA1"})
            self.assertEqual(r.status, 200)
            self.assertEqual(r.header("Content-Type"), "text/xml")
            self.assertIn(b"Enter your PIN", r.body)
            self.assertIn(b"<Response>", r.body)
            r = self.phone("/phone/pin", {"CallSid": "CA1", "Digits": "0000"})
            self.assertIn(b"did not work", r.body)
            r = self.phone("/phone/pin", {"CallSid": "CA1", "Digits": "4321"})
            self.assertIn(b"Authenticated", r.body)
            r = self.phone("/phone/speech", {"CallSid": "CA1", "SpeechResult": "status"})
            self.assertEqual(r.status, 200)
            self.assertIn(b"<Say", r.body)
            self.assertIn(b"Anything else?", r.body)
            self.assertEqual(app.brain.calls, [])                              # STATUS is a fixed command, no AI
            # the phone may cut risk: HALT works
            self.phone("/phone/speech", {"CallSid": "CA1", "SpeechResult": "halt"})
            self.assertTrue(os.path.exists(app.fleet.cfg.halt_file))
            self.assertEqual(self.phone("/phone/nope", {"CallSid": "CA1"}).status, 404)

    def test_phone_caller_cannot_use_desktop_tools(self):
        env = {"JARVIS_PHONE_INBOUND": "1", "TWILIO_AUTH_TOKEN": self.TOKEN, "JARVIS_PUBLIC_URL": self.BASE}
        with isolated_app() as app, mock.patch.dict(os.environ, env):
            app.pin.set_pin("4321")
            app.brain.script += [tool_resp("remember", {"text": "my PIN is 4321"}),
                                 tool_resp("run_backtest", {"strategy_id": "donchian_trend.adx.trail", "symbol": "BTC-USD"}, "toolu_02"),
                                 text_resp("That needs the keyboard.")]
            self.phone("/phone/voice", {"CallSid": "CA5"})
            self.assertIn(b"Authenticated", self.phone("/phone/pin", {"CallSid": "CA5", "Digits": "4321"}).body)
            r = self.phone("/phone/speech", {"CallSid": "CA5", "SpeechResult": "remember my pin and run a backtest"})
            self.assertEqual(r.status, 200)
            self.assertIn(b"That needs the keyboard.", r.body)
            shown = set(app.brain.calls[0]["tools"])
            self.assertFalse(shown & T2_TOOLS, shown)                          # the phone tool list has no T2 tools
            self.assertIn("halt_fleet", shown)
            self.assertEqual(app.store.memories(None, 50), [])
            self.assertEqual(app.tasks.list(10), [])
            audit = app.store.tool_audit(20)
            for name in ("remember", "run_backtest"):
                row = next(a for a in audit if a["tool"] == name)
                self.assertEqual(row["ok"], 0)
                self.assertEqual(row["channel"], "telephone")
                self.assertIn("UNAUTHORIZED", row["error"])


# ================================================================ binding: 127.0.0.1 only
class TestBinding(ServerBase):
    def test_test_server_bound_to_loopback(self):
        self.assertEqual(_Shared.srv.server_address[0], "127.0.0.1")

    def test_main_binds_127_0_0_1_only(self):
        seen = []

        class RecServer:
            def __init__(self, addr, handler):
                seen.append((addr, handler))

            def serve_forever(self):
                raise KeyboardInterrupt

        class StubApp:
            instances = []

            def __init__(self, *a, **k):
                self.data = SimpleNamespace(mode="demo")
                self.stopped = False
                StubApp.instances.append(self)

            def brain_status(self):
                return {"ready": False, "reason": "SETUP_REQUIRED: set ANTHROPIC_API_KEY", "models": {}}

            def stop(self):
                self.stopped = True

        saved = server.APP
        out = io.StringIO()
        try:
            with mock.patch.object(server, "App", StubApp), mock.patch.object(server, "ThreadingHTTPServer", RecServer), \
                    mock.patch.object(sys, "argv", ["server.py", "--no-browser"]), mock.patch("webbrowser.open") as wb, \
                    contextlib.redirect_stdout(out):
                server.main()
        finally:
            server.APP = saved
        self.assertEqual(seen, [(("127.0.0.1", server.PORT), server.H)])
        self.assertTrue(StubApp.instances[0].stopped)
        wb.assert_not_called()
        self.assertIn("PAPER TRADING ONLY", out.getvalue())
        src = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
        self.assertNotIn("0.0.0.0", src)
        self.assertNotRegex(src, r'ThreadingHTTPServer\(\(\s*""')

    def test_no_wildcard_listener(self):
        import psutil
        p = psutil.Process()
        conns = p.net_connections(kind="inet") if hasattr(p, "net_connections") else p.connections(kind="inet")
        listening = [c for c in conns if c.status == psutil.CONN_LISTEN]
        ours = [c for c in listening if c.laddr.port == _Shared.port]
        self.assertTrue(ours, "test server socket not found")
        for c in ours:
            self.assertEqual(c.laddr.ip, "127.0.0.1")
        for c in listening:
            self.assertNotIn(c.laddr.ip, ("0.0.0.0", "::", ""), c)

    def test_not_reachable_on_other_local_addresses(self):
        import psutil
        ips = [a.address for addrs in psutil.net_if_addrs().values() for a in addrs
               if a.family == socket.AF_INET and not a.address.startswith("127.")]
        if not ips:
            self.skipTest("no non-loopback IPv4 address on this machine")
        for ip in ips[:3]:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            try:
                self.assertNotEqual(s.connect_ex((ip, _Shared.port)), 0, f"server reachable on {ip}")
            finally:
                s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
