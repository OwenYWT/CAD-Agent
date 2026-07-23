from openai import APITimeoutError

from app.config import settings


def public_generation_error(exc: Exception) -> dict[str, str]:
    """Convert provider exceptions into stable, actionable API errors."""
    if isinstance(exc, APITimeoutError):
        return {
            "type": "TimeoutError",
            "message": (
                f"模型服务请求超过 {int(settings.llm_timeout_s)} 秒。"
                "请稍后重试；若持续发生，请检查服务器到模型 API 的网络，"
                "或适当提高 LLM_TIMEOUT_S。"
            ),
        }
    return {"type": type(exc).__name__, "message": str(exc)}
