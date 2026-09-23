"""Thin wrapper around the Claude API for schema-validated JSON answers."""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import anthropic

log = logging.getLogger("clipper.llm")

# Models documented to accept the server-side `fallbacks: "default"` parameter.
_FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}


class LLMError(RuntimeError):
    pass


def image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


class Claude:
    def __init__(self, model: str, effort: str = "high") -> None:
        # Credentials resolve from ANTHROPIC_API_KEY (or an `ant auth login` profile).
        self.client = anthropic.Anthropic(max_retries=4)
        self.model = model
        self.effort = effort

    def json(self, system: str, content: list[dict] | str, schema: dict,
             effort: str | None = None, max_tokens: int = 64000) -> dict:
        """Ask Claude and get back a dict that is guaranteed to match `schema`."""
        kwargs: dict = {}
        if self.model in _FALLBACK_MODELS:
            # If a safety classifier declines, the API re-runs on a recommended fallback model.
            kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        for attempt in range(2):
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": effort or self.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                # The (large, stable) system prompt is cached across the many chunk calls.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                **kwargs,
            ) as stream:
                message = stream.get_final_message()

            if message.stop_reason == "refusal":
                category = getattr(message.stop_details, "category", None) if message.stop_details else None
                raise LLMError(f"Claude declined the request (category: {category})")
            if message.stop_reason == "max_tokens":
                raise LLMError("Claude's answer was cut off (max_tokens reached)")
            text = next((b.text for b in message.content if b.type == "text"), "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                log.warning("invalid JSON from Claude (attempt %d), retrying", attempt + 1)
        raise LLMError("Claude returned invalid JSON twice")
