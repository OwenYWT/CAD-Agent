"""Application policy must never impose an output-token cap."""
import pytest

from app.config import Settings
from app.llm import build_chat_params


@pytest.mark.parametrize("provider,model", [("moonshot", "kimi-k2.7-code"), ("openai_compatible", "qwen-plus"), ("azure", "gpt-5")])
def test_legacy_callers_cannot_reintroduce_output_caps(provider, model):
    params = build_chat_params(
        model=model, messages=[{"role": "user", "content": "plan"}],
        max_tokens=2048, max_completion_tokens=4096, max_output_tokens=8192,
        llm_settings=Settings(_env_file=None, llm_provider=provider),
    )
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & params.keys()


def test_provider_extra_body_cannot_reintroduce_caps():
    params = build_chat_params(model="model", messages=[],
        extra_body={"max_tokens": 1, "max_completion_tokens": 2, "max_output_tokens": 3, "thinking": {"type": "enabled"}},
        llm_settings=Settings(_env_file=None))
    assert params["extra_body"] == {"thinking": {"type": "enabled"}}
