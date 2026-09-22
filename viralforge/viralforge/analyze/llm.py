"""One small, defensive wrapper around the Claude API.

Three stages need a model: clip scoring, trend-rubric writing, and caption
copywriting.  They all want the same thing - schema-valid JSON back, with a
graceful path when a feature is not available on the account.

The degradation ladder matters more than it looks.  A hard failure here would
sink a 40-minute pipeline run at the last step, so each optional feature is
dropped one at a time rather than all at once, and a plain-text JSON parse is
the final safety net.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from ..utils import info, warn

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMUnavailable(RuntimeError):
    """No usable credentials, or the SDK is not installed."""


class LLMRefused(RuntimeError):
    """The model declined the request and no fallback rescued it."""


def _extract_json(text: str) -> Any:
    """Parse JSON out of a response that may be wrapped in prose or a fence."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON object found in response: {text[:200]}")


def _has_credentials() -> bool:
    """Mirror the SDK's own resolution order without making a request.

    The SDK constructs happily with no credentials and only fails at request
    time, which would mean discovering the problem an hour into a run.
    """
    if any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")):
        return True
    # Workload identity federation
    wif = ("ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
           "ANTHROPIC_SERVICE_ACCOUNT_ID")
    if all(os.environ.get(k) for k in wif) and (
            os.environ.get("ANTHROPIC_IDENTITY_TOKEN")
            or os.environ.get("ANTHROPIC_IDENTITY_TOKEN_FILE")):
        return True
    # An `ant auth login` profile on disk
    from pathlib import Path
    for candidate in (Path.home() / ".config" / "anthropic",
                      Path(os.environ.get("XDG_CONFIG_HOME", "")) / "anthropic"
                      if os.environ.get("XDG_CONFIG_HOME") else None):
        if candidate and candidate.is_dir() and any(candidate.iterdir()):
            return True
    return False


class LLMClient:
    def __init__(self, model: str = "claude-opus-5", effort: str = "high",
                 max_tokens: int = 16000, use_fallbacks: bool = True,
                 api_key: Optional[str] = None):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.use_fallbacks = use_fallbacks
        self._client = None
        self._api_key = api_key
        self._anthropic = None
        # Set once a 400 tells us the account cannot use a given feature.
        self._no_fallbacks = not use_fallbacks
        self._no_effort = False
        self._no_schema = False
        self._no_thinking = False

    # ------------------------------------------------------------------ #

    @property
    def available(self) -> bool:
        try:
            self._ensure_client()
            return True
        except LLMUnavailable:
            return False

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:
            raise LLMUnavailable(
                "The `anthropic` package is not installed - run `pip install anthropic`."
            ) from exc
        self._anthropic = anthropic

        if self._api_key is None and not _has_credentials():
            raise LLMUnavailable(
                "No Claude credentials found. Set ANTHROPIC_API_KEY, or run `ant auth login`. "
                "You can also run with --no-llm for heuristics only."
            )
        try:
            self._client = (anthropic.Anthropic(api_key=self._api_key) if self._api_key
                            else anthropic.Anthropic())
        except Exception as exc:
            raise LLMUnavailable(f"Could not create a Claude client: {exc}") from exc
        return self._client

    # ------------------------------------------------------------------ #

    def json(self, system: str, user: str, schema: Dict[str, Any],
             *, max_tokens: Optional[int] = None, cache_system: bool = True) -> Any:
        """Ask for one JSON document matching ``schema``."""
        client = self._ensure_client()
        anthropic = self._anthropic
        budget = max_tokens or self.max_tokens

        system_blocks: Any = system
        if cache_system and len(system) > 2000:
            # The trend profile and rubric are identical across every batch in a
            # run; caching them turns N scoring calls into one paid prefix.
            system_blocks = [{"type": "text", "text": system,
                              "cache_control": {"type": "ephemeral"}}]

        last_error: Optional[Exception] = None
        for _ in range(5):                      # one pass per feature we might drop
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "max_tokens": budget,
                "system": system_blocks,
                "messages": [{"role": "user", "content": user}],
            }
            if not self._no_thinking:
                kwargs["thinking"] = {"type": "adaptive"}
            output_config: Dict[str, Any] = {}
            if not self._no_effort:
                output_config["effort"] = self.effort
            if not self._no_schema:
                output_config["format"] = {"type": "json_schema", "schema": schema}
            if output_config:
                kwargs["output_config"] = output_config

            use_beta = not self._no_fallbacks
            if use_beta:
                kwargs["betas"] = [FALLBACK_BETA]
                kwargs["fallbacks"] = "default"

            try:
                target = client.beta.messages if use_beta else client.messages
                if budget > 16000:
                    with target.stream(**kwargs) as stream:
                        response = stream.get_final_message()
                else:
                    response = target.create(**kwargs)
            except anthropic.BadRequestError as exc:
                last_error = exc
                if self._downgrade(str(exc)):
                    continue
                raise
            except anthropic.AuthenticationError as exc:
                raise LLMUnavailable(
                    "Claude rejected the credentials. Check ANTHROPIC_API_KEY, or run "
                    "`ant auth login`."
                ) from exc

            if getattr(response, "stop_reason", None) == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                raise LLMRefused(
                    f"Claude declined to process this content{f' ({category})' if category else ''}."
                )
            if getattr(response, "stop_reason", None) == "max_tokens":
                warn("Model response hit the token cap - the result may be truncated.")

            text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
            return _extract_json(text)

        raise last_error or RuntimeError("Claude request failed for an unknown reason.")

    def _downgrade(self, message: str) -> bool:
        """Drop one optional feature the account clearly cannot use. True = retry."""
        low = message.lower()
        if not self._no_fallbacks and ("fallback" in low or "server-side-fallback" in low
                                       or "beta" in low):
            self._no_fallbacks = True
            info("Server-side refusal fallbacks are unavailable here - continuing without them.")
            return True
        if not self._no_schema and ("output_config" in low or "json_schema" in low
                                    or "format" in low):
            self._no_schema = True
            warn("Structured output is unavailable - falling back to parsing JSON from text.")
            return True
        if not self._no_effort and "effort" in low:
            self._no_effort = True
            return True
        if not self._no_thinking and "thinking" in low:
            self._no_thinking = True
            return True
        return False
