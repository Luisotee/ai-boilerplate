"""cached_input_tokens (agent/response.py): where each provider's cache hits land."""

from openai.types.chat import ChatCompletion
from pydantic_ai.models.openai import _map_usage
from pydantic_ai.usage import RunUsage

from ai_api.agent.response import cached_input_tokens, log_usage


def _deepseek_usage(**usage) -> RunUsage:
    """Run one real DeepSeek-shaped response through pydantic-ai's own mapping."""
    response = ChatCompletion.model_validate(
        {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-chat",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "oi"},
                }
            ],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "total_tokens": 1010,
                **usage,
            },
        }
    )
    run = RunUsage()
    run.incr(_map_usage(response, "deepseek", "https://api.deepseek.com", "deepseek-chat"))
    return run


def test_deepseek_hit_tokens_are_read_from_details():
    # Without prompt_tokens_details, pydantic-ai leaves cache_read_tokens at 0.
    usage = _deepseek_usage(prompt_cache_hit_tokens=800, prompt_cache_miss_tokens=200)
    assert usage.cache_read_tokens == 0
    assert cached_input_tokens(usage) == 800


def test_openai_style_cached_tokens_fill_cache_read_tokens():
    usage = _deepseek_usage(prompt_tokens_details={"cached_tokens": 600})
    assert cached_input_tokens(usage) == 600


def test_null_values_count_as_zero():
    usage = RunUsage(input_tokens=100, details={"prompt_cache_hit_tokens": None})
    assert cached_input_tokens(usage) == 0
    log_usage(usage)  # must not raise
