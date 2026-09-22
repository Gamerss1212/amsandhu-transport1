"""The Claude wrapper, exercised with a stub SDK - no network, no key."""

import sys
import types

import pytest

from viralforge.analyze.llm import LLMClient, LLMRefused, LLMUnavailable, _extract_json


def test_json_is_recovered_from_prose_and_fences():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert _extract_json('Sure!\n{"a": 3}\nHope that helps.') == {"a": 3}
    assert _extract_json('[1, 2, 3]') == [1, 2, 3]
    with pytest.raises(ValueError):
        _extract_json("no json at all")


class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Response:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block(text)]
        self.stop_reason = stop_reason
        self.stop_details = None


class _Messages:
    def __init__(self, owner, beta):
        self.owner, self.beta = owner, beta

    def create(self, **kwargs):
        self.owner.calls.append(kwargs)
        failure = self.owner.fail_on.pop(0) if self.owner.fail_on else None
        if failure:
            raise self.owner.errors.BadRequestError(failure)
        return _Response('{"ok": true}')


class _Namespace:
    def __init__(self, owner, beta):
        self.messages = _Messages(owner, beta)


class _StubClient:
    def __init__(self, errors, fail_on=None):
        self.calls = []
        self.errors = errors
        self.fail_on = list(fail_on or [])
        self.messages = _Namespace(self, False).messages
        self.beta = _Namespace(self, True)


class _BadRequestError(Exception):
    pass


def _stub_module(client_factory):
    module = types.ModuleType("anthropic")
    module.Anthropic = client_factory
    module.BadRequestError = _BadRequestError
    module.AuthenticationError = type("AuthenticationError", (Exception,), {})
    return module


@pytest.fixture
def stub(monkeypatch):
    holder = {}

    def install(fail_on=None):
        client = _StubClient(types.SimpleNamespace(BadRequestError=_BadRequestError), fail_on)
        holder["client"] = client
        monkeypatch.setitem(sys.modules, "anthropic",
                            _stub_module(lambda **kw: client))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        return client
    return install


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}


def test_happy_path_sends_schema_effort_and_fallbacks(stub):
    client = stub()
    out = LLMClient(effort="high").json("sys", "user", SCHEMA)
    assert out == {"ok": True}
    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["effort"] == "high"
    assert call["output_config"]["format"]["schema"] == SCHEMA
    assert call["fallbacks"] == "default"


def test_drops_fallbacks_when_the_account_cannot_use_them(stub):
    client = stub(fail_on=["server-side-fallback beta is not enabled"])
    assert LLMClient().json("sys", "user", SCHEMA) == {"ok": True}
    assert len(client.calls) == 2
    assert "fallbacks" not in client.calls[1]


def test_drops_structured_output_before_giving_up(stub):
    client = stub(fail_on=["fallbacks not supported", "output_config.format is unsupported"])
    assert LLMClient().json("sys", "user", SCHEMA) == {"ok": True}
    assert "format" not in client.calls[-1].get("output_config", {})


def test_refusal_is_surfaced_not_swallowed(stub, monkeypatch):
    client = stub()

    def refuse(**kwargs):
        return _Response("", stop_reason="refusal")
    monkeypatch.setattr(client.beta.messages, "create", refuse)
    with pytest.raises(LLMRefused):
        LLMClient().json("sys", "user", SCHEMA)


def test_missing_credentials_reported_before_any_work(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FEDERATION_RULE_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("viralforge.analyze.llm._has_credentials", lambda: False)
    client = LLMClient()
    assert client.available is False
    with pytest.raises(LLMUnavailable):
        client.json("sys", "user", SCHEMA)


def test_large_system_prompt_is_cached(stub):
    client = stub()
    LLMClient().json("x" * 3000, "user", SCHEMA)
    system = client.calls[0]["system"]
    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}
