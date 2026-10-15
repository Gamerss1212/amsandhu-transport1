"""alerts.py - typed alert rules evaluated by CODE with exact decimals (JARVIS PRIME sections 49-52).
The AI only turns a request into a typed rule and reads it back; it never decides when a rule fires.

Condition kinds (no free-form expressions, ever):
  metric_threshold  {metric: price:<SYMBOL> | paper_pnl_today | paper_equity | open_positions | paused_bots,
                     operator: > >= < <=, threshold: "decimal string", initial_true_policy: trigger|wait_for_crossing}
  vol_state         {symbol, state: LOUD|NORMAL|QUIET}
  any_signal        {}                       a bot printed a new LONG signal (shadow or paper)
  task_completed    {task_id}
  scheduled         {local_time: "HH:MM", local_date?: "YYYY-MM-DD", recurrence: once|daily, message}
"""
from __future__ import annotations
import json, time, uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}
KINDS = {"metric_threshold", "vol_state", "any_signal", "task_completed", "scheduled"}


def validate(rule: dict) -> dict:
    c = rule.get("condition") or {}
    k = c.get("kind")
    if k not in KINDS: raise ValueError(f"unknown condition kind {k!r}")
    if k == "metric_threshold":
        m = str(c.get("metric", ""))
        if not (m in ("paper_pnl_today", "paper_equity", "open_positions", "paused_bots") or
                (m.startswith("price:") and 3 <= len(m) <= 30)):
            raise ValueError("unsupported metric")
        if c.get("operator") not in OPS: raise ValueError("operator must be one of > >= < <=")
        try: Decimal(str(c.get("threshold")))
        except InvalidOperation: raise ValueError("threshold must be a decimal number")
        c.setdefault("initial_true_policy", "wait_for_crossing")
    if k == "vol_state" and c.get("state") not in ("LOUD", "NORMAL", "QUIET"): raise ValueError("state must be LOUD/NORMAL/QUIET")
    if k == "scheduled":
        try: datetime.strptime(c.get("local_time", ""), "%H:%M")
        except ValueError: raise ValueError("local_time must be HH:MM")
        if c.get("recurrence", "once") not in ("once", "daily"): raise ValueError("recurrence must be once or daily")
        if c.get("recurrence", "once") == "once" and not c.get("local_date"): raise ValueError("a one-off reminder needs local_date")
    chans = rule.get("channels") or ["desktop"]
    if any(ch not in ("desktop", "phone") for ch in chans): raise ValueError("channels: desktop and/or phone")
    if int(rule.get("cooldown_s", 0)) < 0: raise ValueError("cooldown must be >= 0")
    return {"id": rule.get("id") or uuid.uuid4().hex[:8], "version": int(rule.get("version", 1)), "condition": c,
            "label": str(rule.get("label") or "")[:120], "timezone": rule.get("timezone") or "America/Edmonton",
            "severity": rule.get("severity") if rule.get("severity") in ("info", "warning", "critical") else "info",
            "channels": chans, "cooldown_s": int(rule.get("cooldown_s", 600)), "max_data_age_s": int(rule.get("max_data_age_s", 5400)),
            "quiet_hours_behavior": rule.get("quiet_hours_behavior") if rule.get("quiet_hours_behavior") in ("defer", "suppress", "authorized_override") else "defer",
            "expires_at": rule.get("expires_at"), "created_at": time.time()}


def describe(r: dict) -> str:
    c = r["condition"]; k = c["kind"]
    ch = " and ".join(r["channels"])
    if k == "metric_threshold":
        what = c["metric"].replace("price:", "price of ").replace("_", " ")
        mode = "when it crosses" if c.get("initial_true_policy") == "wait_for_crossing" else "as soon as"
        return f"Notify by {ch} {mode} {what} {c['operator']} {c['threshold']} (data no older than {r['max_data_age_s'] // 60} min)."
    if k == "vol_state": return f"Notify by {ch} when the volatility gate for {c['symbol']} reads {c['state']}."
    if k == "any_signal": return f"Notify by {ch} when any bot prints a new long signal."
    if k == "task_completed": return f"Notify by {ch} when task {c['task_id']} finishes."
    if k == "scheduled":
        when = c["local_time"] + (f" on {c['local_date']}" if c.get("local_date") else " every day")
        return f"Remind by {ch} at {when} ({r['timezone']}): {c.get('message', '')}"
    return "unknown rule"


class AlertEngine:
    def __init__(self, store, metrics, calls=None, quiet_hours=("22:00", "07:00")):
        """metrics(name) -> (value, observed_epoch) or None. calls = OutboundCallManager."""
        self.store, self.metrics, self.calls, self.quiet = store, metrics, calls, quiet_hours
        self.events: list = []                            # app events: ("task_completed", id) / ("signal", bot)

    # ---------- CRUD ----------
    def add(self, rule: dict) -> dict:
        r = validate(rule)
        self.store.x("INSERT INTO alert_rules VALUES (?,?,?,?,?,?)", (r["id"], r["version"], json.dumps(r), 1, r["created_at"], "{}"))
        return r

    def list(self) -> list:
        out = []
        for row in self.store.q("SELECT * FROM alert_rules ORDER BY created_at DESC"):
            r = json.loads(row["rule"]); r["enabled"] = bool(row["enabled"]); r["text"] = describe(r)
            r["state"] = json.loads(row["eval_state"] or "{}"); out.append(r)
        return out

    def disable(self, rid: str) -> bool:
        self.store.x("UPDATE alert_rules SET enabled=0 WHERE id=?", (rid,)); return True

    def emit(self, kind: str, ref: str):
        self.events.append((kind, ref, time.time())); del self.events[:-200]

    # ---------- evaluation ----------
    def _in_quiet(self, now_local: datetime) -> bool:
        a, b = (datetime.strptime(x, "%H:%M").time() for x in self.quiet)
        t = now_local.time()
        return (a <= t or t < b) if a > b else (a <= t < b)

    def evaluate(self, now: float | None = None) -> list:
        now = now or time.time(); fired = []
        for row in self.store.q("SELECT * FROM alert_rules WHERE enabled=1"):
            r = json.loads(row["rule"]); st = json.loads(row["eval_state"] or "{}")
            if r.get("expires_at") and now > float(r["expires_at"]):
                self.store.x("UPDATE alert_rules SET enabled=0 WHERE id=?", (r["id"],)); continue
            tz = ZoneInfo(r["timezone"]); local = datetime.fromtimestamp(now, tz)
            try:
                occ, msg = self._check(r, st, now, local)
            except Exception as e:
                st["last_error"] = f"{type(e).__name__}: {e}"; occ = None
            if occ:
                if now - st.get("last_fired", 0) >= r["cooldown_s"] or r["condition"]["kind"] in ("scheduled", "task_completed"):
                    if self._deliver(r, occ, msg, local): st["last_fired"] = now; fired.append({"rule": r["id"], "message": msg})
            st["checked_at"] = now
            self.store.x("UPDATE alert_rules SET eval_state=? WHERE id=?", (json.dumps(st), r["id"]))
        return fired

    def _check(self, r, st, now, local):
        c = r["condition"]; k = c["kind"]
        if k == "metric_threshold":
            got = self.metrics(c["metric"])
            if got is None: st["note"] = "metric unavailable"; return None, None
            val, obs = got
            if obs is not None and now - obs > r["max_data_age_s"]: st["note"] = "data stale - not evaluated"; return None, None
            ok = OPS[c["operator"]](Decimal(str(val)), Decimal(str(c["threshold"])))
            prev = st.get("prev_true")
            st["prev_true"], st["last_value"], st["note"] = ok, str(val), ""
            if prev is None and c.get("initial_true_policy") == "wait_for_crossing": return None, None
            if ok and not prev:
                return f"{local.date()}:{st.get('n', 0) + 1}", f"{c['metric'].replace('price:', '')} is {val} ({c['operator']} {c['threshold']})"
            return None, None
        if k == "vol_state":
            got = self.metrics(f"vol:{c['symbol']}")
            if got is None: return None, None
            ok = got[0] == c["state"]; prev = st.get("prev_true"); st["prev_true"] = ok
            if ok and prev is False: return f"{now // 3600}", f"Volatility gate for {c['symbol']} now reads {c['state']}"
            return None, None
        if k in ("any_signal", "task_completed"):
            seen = st.get("seen_t", now if k == "any_signal" and "seen_t" not in st else 0)
            for kind, ref, t in self.events:
                if t <= seen: continue
                if k == "any_signal" and kind == "signal":
                    st["seen_t"] = t; return f"sig:{ref}:{int(t)}", f"New long signal: {ref}"
                if k == "task_completed" and kind == "task_completed" and ref == c["task_id"]:
                    st["seen_t"] = t; return f"task:{ref}", f"Task {ref} finished"
            st["seen_t"] = max(seen, now - 1) if k == "any_signal" else seen
            return None, None
        if k == "scheduled":
            hh, mm = map(int, c["local_time"].split(":"))
            if c.get("recurrence", "once") == "once":
                d = datetime.fromisoformat(c["local_date"]).date()
                target = datetime(d.year, d.month, d.day, hh, mm, tzinfo=local.tzinfo)
                if local >= target and not st.get("done") and local - target < timedelta(hours=6):
                    st["done"] = True; self.store.x("UPDATE alert_rules SET enabled=0 WHERE id=?", (r["id"],))
                    return f"once:{d}", c.get("message") or "Reminder"
                if local - target >= timedelta(hours=6): self.store.x("UPDATE alert_rules SET enabled=0 WHERE id=?", (r["id"],))
                return None, None
            target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            key = local.date().isoformat()
            if local >= target and st.get("last_day") != key and local - target < timedelta(hours=1):
                st["last_day"] = key; return f"daily:{key}", c.get("message") or "Reminder"
            return None, None
        return None, None

    def _deliver(self, r, occ, msg, local) -> bool:
        quiet = self._in_quiet(local)
        delivered = False
        for ch in r["channels"]:
            key = f"{r['id']}:{r['version']}:{occ}:{ch}"
            if ch == "desktop":
                self.store.x("INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,?,?,?,?)", (key, r["id"], ch, "pending", msg, time.time(), time.time(), None, None))
                row = self.store.q("SELECT state FROM outbox WHERE key=?", (key,))[0]
                if row["state"] != "pending": continue
                self.store.notify(r.get("label") or "Alert", msg, "info" if quiet else r["severity"])
                self.store.x("UPDATE outbox SET state='confirmed', updated_at=? WHERE key=?", (time.time(), key))
                delivered = True
            elif ch == "phone" and self.calls:
                if quiet and not (r["quiet_hours_behavior"] == "authorized_override" and r["severity"] == "critical"):
                    if r["quiet_hours_behavior"] == "suppress": continue
                    self.store.notify("Call deferred (quiet hours)", msg, "info"); continue
                res = self.calls.request(key, r["id"])
                if not res.get("ok"):
                    self.store.notify("Phone alert not sent", f"{msg} - phone state: {res.get('state')}", "warning")
                delivered = True
        return delivered
