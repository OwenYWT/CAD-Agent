import httpx
from openai import APITimeoutError

from app.api.error_messages import public_generation_error
from app.config import settings


def test_model_timeout_has_actionable_public_message(monkeypatch):
    monkeypatch.setattr(settings, "llm_timeout_s", 75.0)
    error = APITimeoutError(request=httpx.Request("POST", "https://api.example.test/v1/chat/completions"))

    payload = public_generation_error(error)

    assert payload["type"] == "TimeoutError"
    assert "75 秒" in payload["message"]
    assert "LLM_TIMEOUT_S" in payload["message"]


def test_unexpected_error_keeps_original_type_and_message():
    payload = public_generation_error(ValueError("bad input"))

    assert payload == {"type": "ValueError", "message": "bad input"}
