import json, os, shutil, tempfile, threading, unittest, urllib.request
os.environ["JARVUS_DIR"] = tempfile.mkdtemp()
os.environ["JARVIS_PORT"] = "8799"
os.environ.pop("ANTHROPIC_API_KEY", None)
import server, policy

class B:
    def __init__(self, **kw): self.d = kw
    def model_dump(self): return dict(self.d)
class Resp:
    def __init__(self, blocks, stop): self.content = blocks; self.stop_reason = stop
class FakeClient:
    """Pretends to be Claude: first asks to build a website, then answers."""
    def __init__(self): self.calls = []; self.messages = self
    def create(self, **kw):
        self.calls.append(kw)
        if len(self.calls) == 1:
            return Resp([B(type="text", text="Building."), B(type="tool_use", id="t1", name="build_website",
                         input={"title": "Cafe Edmonton!", "html": "<h1>Cafe</h1>"})], "tool_use")
        return Resp([B(type="text", text="Your cafe site is open.")], "end_turn")

class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.copy(os.path.join("..", "kit", "demo", "report.json"), server.REPORT)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 8799), server.H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()
    def post(self, text, origin=None):
        req = urllib.request.Request("http://127.0.0.1:8799/api/ask", json.dumps({"text": text}).encode(), {"Content-Type": "application/json"})
        if origin: req.add_header("Origin", origin)
        return json.loads(urllib.request.urlopen(req).read())
    def test_page_and_stats(self):
        self.assertIn(b"JARVUS", urllib.request.urlopen("http://127.0.0.1:8799/").read())
        s = json.loads(urllib.request.urlopen("http://127.0.0.1:8799/api/stats").read())
        self.assertTrue(s["ok"]); self.assertTrue(s["paper_only"]); self.assertEqual(s["pnl_today"], -22.75)
    def test_fixed_commands_without_ai(self):
        self.assertEqual(self.post("status")["command"], "STATUS")
        self.assertIn("22.75 dollars", self.post("how did today go")["reply"])
    def test_chat_off_without_key(self):
        self.assertIn("API key", self.post("tell me a joke")["reply"])
    def test_fleet_and_history(self):
        f = json.loads(urllib.request.urlopen("http://127.0.0.1:8799/api/fleet").read())
        self.assertEqual(f["total"], 99); self.assertEqual(f["allowed"], 0); self.assertEqual(f["counts"]["WATCH"], 3)
        shutil.copy(os.path.join("..", "kit", "demo", "paper_log.csv"), os.path.join(server.STATE, "paper_log.csv"))
        h = json.loads(urllib.request.urlopen("http://127.0.0.1:8799/api/history").read())["points"]
        self.assertGreater(len(h), 3); self.assertAlmostEqual(h[-1]["cum"], -85.5, 1)
    def test_other_websites_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as e: self.post("halt", origin="https://evil.example")
        self.assertEqual(e.exception.code, 403); self.assertFalse(policy.halt_active(server.CFG))
    def test_claude_builds_website(self):
        fc = FakeClient()
        out = server.handle_ask("build a website for a cafe", client=fc)
        self.assertEqual(out["open_url"], "/sites/cafe-edmonton.html"); self.assertIn("open", out["reply"])
        self.assertEqual(urllib.request.urlopen("http://127.0.0.1:8799/sites/cafe-edmonton.html").read(), b"<h1>Cafe</h1>")
        names = [t["name"] for t in fc.calls[0]["tools"]]
        self.assertFalse([n for n in names if any(w in n for w in ("order", "buy", "sell", "trade", "resume"))])
        self.assertIn("Paper", fc.calls[0]["system"])
    def test_path_traversal_blocked(self):
        with self.assertRaises(urllib.error.HTTPError): urllib.request.urlopen("http://127.0.0.1:8799/sites/../server.py")
    def test_zz_halt_last(self):
        self.post("halt"); self.assertTrue(policy.halt_active(server.CFG))
        self.assertIn("HALT", self.post("status")["reply"])

if __name__ == "__main__":
    unittest.main(verbosity=1)
