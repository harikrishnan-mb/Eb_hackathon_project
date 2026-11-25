import io
import json
import urllib.error

import pytest

from assistant.llm import LLMError, MockLLM, OpenRouterLLM, from_env

SECRET = "sk-or-v1-TESTONLY-0000"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener_returning(payload):
    captured = {}

    def opener(req, timeout):
        captured["req"] = req
        captured["timeout"] = timeout
        return _Resp(json.dumps(payload).encode())

    return opener, captured


def test_openrouter_request_shape_and_parse():
    opener, cap = _opener_returning({"choices": [{"message": {"content": "hello"}}]})
    llm = OpenRouterLLM(SECRET, opener=opener)
    out = llm.generate("ping", max_tokens=32, timeout_s=5)
    assert out == "hello"
    req = cap["req"]
    assert req.get_header("Authorization") == f"Bearer {SECRET}"
    body = json.loads(req.data)
    assert body["model"] == "anthropic/claude-sonnet-5.5"
    assert body["temperature"] == 0.0 and body["max_tokens"] == 32
    assert cap["timeout"] == 5


def test_http_error_never_leaks_key():
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    llm = OpenRouterLLM(SECRET, opener=opener)
    with pytest.raises(LLMError) as exc:
        llm.generate("x", max_tokens=8, timeout_s=1)
    assert "401" in str(exc.value)
    assert SECRET not in str(exc.value)


def test_malformed_response_is_llm_error():
    opener, _ = _opener_returning({"error": "nope"})
    with pytest.raises(LLMError):
        OpenRouterLLM(SECRET, opener=opener).generate("x", max_tokens=8, timeout_s=1)


def test_empty_key_rejected():
    with pytest.raises(LLMError):
        OpenRouterLLM("")


def test_from_env_prefers_openrouter_when_key_present():
    client = from_env({"OPENROUTER_API_KEY": SECRET})
    assert isinstance(client, OpenRouterLLM)


def test_from_env_defaults_to_mock_without_keys():
    assert isinstance(from_env({}), MockLLM)


def test_from_env_unknown_provider():
    with pytest.raises(LLMError):
        from_env({"LLM_PROVIDER": "nonsense"})
