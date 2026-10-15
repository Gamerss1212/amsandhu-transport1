"""brain.py - model providers behind one small interface (JARVIS PRIME sections 8-11, 67).

AnthropicBrain  - Claude via the official SDK. Two routes:
                  strong (default claude-opus-5-5) for reasoning, analysis, planning, research;
                  fast   (default claude-sonnet-5-5) for greetings, short follow-ups, simple status.
                  Server-side web search/fetch for current information (with sources).
                  Refusal fallback: fallbacks="default" (beta server-side-fallback-2026-07-01).
OfflineBrain    - clearly labelled degraded mode when no API key is set: only local, deterministic answers.
FakeBrain       - tests only.
Models are explicit IDs from config. Nothing is "unlimited": calls can hit rate limits, cost money, or fail.
"""
from __future__ import annotations
import os, re, time

FALLBACK_BETA = "server-side-fallback-2026-07-01"
STRONG_HINTS = re.compile(r"\b(why|how come|compare|comparison|analy[sz]|explain|plan|research|strategy|strategies|backtest|"
                          r"should i|think|deep|detail|report|summar|difference|pros and cons|evaluate|review|design|write|draft|"
                          r"what if|predict|forecast|teach|walk me through|step by step)\b", re.I)


def route_for(text: str, state: dict) -> str:
    t = text.strip()
    if len(t) > 220 or STRONG_HINTS.search(t): return "strong"
    if t.lower().rstrip("?.! ") in ("why", "how", "and then") and state.get("last_route") == "strong": return "strong"
    return "fast"


class BrainError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message); self.code = code


class AnthropicBrain:
    name = "anthropic"

    def __init__(self, models: dict, web: bool = True, client=None, effort: dict | None = None):
        self.models, self.web, self._client = models, web, client
        self.effort = effort or {"strong": "medium", "fast": "low"}
        self.fallbacks_ok = True
        self.last_error = None

    def ready(self) -> tuple:
        if self._client is not None: return True, "ok"
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            return False, "SETUP_REQUIRED: set ANTHROPIC_API_KEY"
        try: import anthropic  # noqa: F401
        except ImportError: return False, "SETUP_REQUIRED: pip install anthropic"
        return True, "ok"

    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(max_retries=2, timeout=120)
        return self._client

    def server_tools(self) -> list:
        if not self.web: return []
        return [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3}]

    def create(self, route: str, system: str, messages: list, tools: list, max_tokens: int = 16000) -> dict:
        """One Messages API call. Returns a normalised dict: content (list of plain dicts), stop_reason, model, usage."""
        c = self.client()
        model = self.models[route]
        kw = dict(model=model, max_tokens=max_tokens, system=system, messages=messages, tools=tools + self.server_tools(),
                  output_config={"effort": self.effort.get(route, "medium")}, cache_control={"type": "ephemeral"})
        try:
            import anthropic
        except ImportError:
            anthropic = None
        try:
            if self.fallbacks_ok:
                try:
                    r = c.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kw)
                except TypeError:
                    self.fallbacks_ok = False; r = c.messages.create(**kw)
                except Exception as e:
                    if anthropic and isinstance(e, anthropic.BadRequestError) and "fallback" in str(e).lower():
                        self.fallbacks_ok = False; r = c.messages.create(**kw)
                    else: raise
            else:
                r = c.messages.create(**kw)
        except Exception as e:
            self.last_error = f"{type(e).__name__}"
            if anthropic is None: raise BrainError("UNAVAILABLE", str(e))
            if isinstance(e, anthropic.AuthenticationError): raise BrainError("AUTH", "The API key was rejected.")
            if isinstance(e, anthropic.PermissionDeniedError): raise BrainError("AUTH", "The API key lacks permission for this model.")
            if isinstance(e, anthropic.NotFoundError): raise BrainError("MODEL", f"Model {model} is not available to this account.")
            if isinstance(e, anthropic.RateLimitError): raise BrainError("RATE_LIMIT", "Rate limited by the AI provider.")
            if isinstance(e, anthropic.APIStatusError): raise BrainError("PROVIDER", f"AI provider error {e.status_code}.")
            if isinstance(e, anthropic.APIConnectionError): raise BrainError("NETWORK", "Cannot reach the AI provider.")
            raise BrainError("UNAVAILABLE", str(e)[:200])
        content = [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else dict(b) for b in r.content]
        usage = r.usage.model_dump(exclude_none=True) if hasattr(r.usage, "model_dump") else {}
        self.last_error = None
        return {"content": content, "stop_reason": r.stop_reason, "model": getattr(r, "model", model), "usage": usage}


class OfflineBrain:
    """Degraded mode: no model. Answers only from local tools with fixed patterns. Says so plainly."""
    name = "offline"

    def ready(self): return False, "SETUP_REQUIRED: set ANTHROPIC_API_KEY for full conversation"


class FakeBrain:
    """Tests only: replays scripted responses."""
    name = "fake"

    def __init__(self, script: list):
        self.script, self.calls = list(script), []
        self.models = {"strong": "fake-strong", "fast": "fake-fast"}

    def ready(self): return True, "ok"

    def create(self, route, system, messages, tools, max_tokens=16000):
        self.calls.append({"route": route, "messages": [dict(m) for m in messages], "tools": [t["name"] for t in tools], "system": system})
        nxt = self.script.pop(0) if self.script else {"content": [{"type": "text", "text": "OK."}], "stop_reason": "end_turn"}
        return {"model": f"fake-{route}", "usage": {}, **nxt}
