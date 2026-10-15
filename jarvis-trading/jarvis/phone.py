"""phone.py - telephone access (JARVIS PRIME sections 43-53). OFF until the owner configures it.

Outbound: Jarvis may call ONLY the owner's own number (JARVUS_MY_NUMBER), only from a typed alert rule, with caps,
  quiet hours and a durable outbox. The call itself says nothing private - just "you have a notification, call back
  and authenticate" - because voicemail or anyone can answer a phone.
Inbound (Twilio): the caller must enter a PIN by keypad (DTMF). The PIN is stored only as a salted PBKDF2 hash,
  is never logged, and never reaches the AI. After the PIN, the call gets READ access plus HALT/PAUSE only.
  Twilio signatures are verified on every webhook. A PIN never authorises anything that adds risk.

Status: implemented and unit-tested with fakes; NOT tested against a real carrier.
"""
from __future__ import annotations
import base64, hashlib, hmac, json, os, secrets, threading, time, urllib.parse, urllib.request
from xml.sax.saxutils import escape

GENERIC_CALL = ("This is your Jarvis assistant. You have a new notification. "
                "Please call back and enter your PIN to hear it. Goodbye.")


class PinAuth:
    def __init__(self, state_dir: str, max_fail: int = 5, window_s: int = 900):
        self.path = os.path.join(state_dir, "phone_auth.json")
        self.max_fail, self.window_s = max_fail, window_s
        self.lock = threading.Lock()

    def _load(self):
        try: return json.load(open(self.path))
        except Exception: return {"fails": []}

    def _save(self, d):
        json.dump(d, open(self.path, "w"))

    def configured(self) -> bool:
        return "hash" in self._load()

    def set_pin(self, pin: str):
        if not (pin.isdigit() and 4 <= len(pin) <= 10): raise ValueError("PIN must be 4-10 digits")
        salt = secrets.token_bytes(16)
        h = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000)
        with self.lock:
            d = self._load(); d.update(salt=salt.hex(), hash=h.hex(), iterations=200_000, fails=[]); self._save(d)

    def locked(self) -> bool:
        d = self._load(); now = time.time()
        return len([t for t in d.get("fails", []) if now - t < self.window_s]) >= self.max_fail

    def verify(self, pin: str) -> bool:
        with self.lock:
            d = self._load()
            if "hash" not in d or self.locked(): return False
            h = hashlib.pbkdf2_hmac("sha256", (pin or "").encode(), bytes.fromhex(d["salt"]), d.get("iterations", 200_000))
            ok = hmac.compare_digest(h.hex(), d["hash"])
            now = time.time()
            d["fails"] = [] if ok else [t for t in d.get("fails", []) if now - t < self.window_s] + [now]
            self._save(d)
            return ok


def twilio_signature_ok(auth_token: str, url: str, params: dict, signature: str) -> bool:
    """Twilio request validation: base64(HMAC-SHA1(token, url + sorted(key+value)))."""
    s = url + "".join(k + params[k] for k in sorted(params))
    mac = base64.b64encode(hmac.new(auth_token.encode(), s.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(mac, signature or "")


def twiml(*parts: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(parts) + "</Response>"


def say(text: str) -> str:
    return f"<Say voice=\"Polly.Brian\">{escape(text)}</Say>"


class OutboundCallManager:
    def __init__(self, store, cfg: dict):
        self.store, self.cfg = store, cfg
        self.lock = threading.Lock()

    def status(self) -> dict:
        need = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "JARVUS_MY_NUMBER"]
        missing = [k for k in need if not os.environ.get(k)]
        if missing: return {"ready": False, "state": "SETUP_REQUIRED", "missing": missing}
        if self.cfg.get("calls_off"): return {"ready": False, "state": "KILL_SWITCH_ON"}
        return {"ready": True, "state": "READY", "to": "owner (allowlisted)"}

    def _count_since(self, seconds: float) -> int:
        r = self.store.q("SELECT COUNT(*) n FROM outbox WHERE channel='phone' AND state IN ('dispatching','confirmed','unknown_outcome') "
                         "AND updated_at > ?", (time.time() - seconds,))
        return r[0]["n"]

    def request(self, key: str, rule_id: str, sender=None) -> dict:
        """Called by the alert engine with a durable occurrence key. Exactly one row per key (dedupe)."""
        with self.lock:
            now = time.time()
            self.store.x("INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,?,?,?,?)", (key, rule_id, "phone", "pending", GENERIC_CALL, now, now, None, None))
            row = self.store.q("SELECT * FROM outbox WHERE key=?", (key,))[0]
            if row["state"] != "pending": return {"ok": False, "state": row["state"], "reason": "already handled"}
            st = self.status()
            if not st["ready"]:
                self._set(key, "failed_before_send", error=st["state"]); return {"ok": False, "state": st["state"]}
            if self._count_since(3600) >= self.cfg.get("max_calls_per_hour", 2) or self._count_since(86400) >= self.cfg.get("max_calls_per_day", 6):
                self._set(key, "failed_before_send", error="call cap reached"); return {"ok": False, "state": "CAPPED"}
            self._set(key, "dispatching")
        try:
            sid = (sender or self._twilio_call)(GENERIC_CALL)
            self._set(key, "confirmed", provider_id=sid); return {"ok": True, "state": "confirmed", "provider_id": sid}
        except TimeoutError as e:
            self._set(key, "unknown_outcome", error=str(e)); return {"ok": False, "state": "unknown_outcome"}
        except Exception as e:
            self._set(key, "failed_before_send", error=f"{type(e).__name__}"); return {"ok": False, "state": "failed_before_send"}

    def _set(self, key, state, provider_id=None, error=None):
        self.store.x("UPDATE outbox SET state=?, updated_at=?, provider_id=COALESCE(?, provider_id), error=? WHERE key=?",
                     (state, time.time(), provider_id, error, key))

    def _twilio_call(self, text: str) -> str:
        sid, tok = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
        data = urllib.parse.urlencode({"To": os.environ["JARVUS_MY_NUMBER"], "From": os.environ["TWILIO_FROM_NUMBER"],
                                       "Twiml": twiml(say(text))}).encode()
        req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json", data)
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode())
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()).get("sid", "")


class InboundCalls:
    """Twilio voice webhooks: /phone/voice -> PIN -> speech turns into the SAME Jarvis Core (channel 'telephone')."""
    def __init__(self, pin: PinAuth, core_handle):
        self.pin, self.core_handle = pin, core_handle
        self.calls: dict = {}                    # CallSid -> {"auth": bool, "exp": t, "session": id, "tries": n}

    def enabled(self) -> bool:
        return os.environ.get("JARVIS_PHONE_INBOUND") == "1" and bool(os.environ.get("TWILIO_AUTH_TOKEN")) and self.pin.configured()

    def verify(self, url: str, params: dict, signature: str) -> bool:
        return twilio_signature_ok(os.environ.get("TWILIO_AUTH_TOKEN", ""), url, params, signature)

    def voice(self, params: dict) -> str:
        sid = params.get("CallSid", "")
        self.calls[sid] = {"auth": False, "exp": time.time() + 600, "session": None, "tries": 0}
        return twiml('<Gather input="dtmf" numDigits="10" finishOnKey="#" timeout="8" action="/phone/pin" method="POST">',
                     say("Jarvis here. Enter your PIN, then press pound."), "</Gather>", say("Goodbye."), "<Hangup/>")

    def pin_entered(self, params: dict) -> str:
        sid = params.get("CallSid", ""); c = self.calls.get(sid)
        if not c or time.time() > c["exp"]: return twiml(say("Session expired. Goodbye."), "<Hangup/>")
        if self.pin.verify(params.get("Digits", "")):          # digits are never logged or sent to the AI
            c["auth"] = True; c["exp"] = time.time() + 900
            return twiml('<Gather input="speech" speechTimeout="auto" action="/phone/speech" method="POST">',
                         say("Authenticated. What do you need?"), "</Gather>")
        c["tries"] += 1
        if c["tries"] >= 3 or self.pin.locked(): return twiml(say("Goodbye."), "<Hangup/>")
        return twiml('<Gather input="dtmf" numDigits="10" finishOnKey="#" timeout="8" action="/phone/pin" method="POST">',
                     say("That did not work. Try again."), "</Gather>")

    def speech(self, params: dict) -> str:
        sid = params.get("CallSid", ""); c = self.calls.get(sid)
        if not c or not c["auth"] or time.time() > c["exp"]: return twiml(say("Please call back and authenticate."), "<Hangup/>")
        text = (params.get("SpeechResult") or "").strip()
        if not text:
            reply = "I did not catch that."
        else:
            out = self.core_handle(text, channel="telephone", session_id=c["session"], auth_level="owner_read")
            c["session"] = out.get("session_id"); reply = out.get("speak") or out.get("reply") or "Done."
        return twiml(say(reply[:900]), '<Gather input="speech" speechTimeout="auto" action="/phone/speech" method="POST">',
                     say("Anything else?"), "</Gather>", say("Goodbye."), "<Hangup/>")
