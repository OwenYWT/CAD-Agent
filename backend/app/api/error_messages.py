from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from app.llm import find_provider_exception, is_provider_quota_error


def public_generation_error(exc: Exception) -> dict[str, str]:
    """Convert provider exceptions into stable, actionable API errors."""
    provider_error = find_provider_exception(exc)
    if isinstance(provider_error, APITimeoutError):
        return {
            "type": "TimeoutError",
            "message": (
                "模型服务调用发生超时，本次未收到完整结果。"
                "应用未设置模型 API 等待时限；请重试，"
                "若持续发生，请管理员检查模型服务与网络链路。"
            ),
        }
    if is_provider_quota_error(exc):
        return {
            "type": "ProviderQuotaError",
            "message": "模型服务额度不足，当前无法生成或修改。请联系管理员补充服务额度。",
        }
    if isinstance(provider_error, RateLimitError):
        return {
            "type": "ProviderRateLimitError",
            "message": "模型服务当前请求过多，请稍后重试。",
        }
    if isinstance(provider_error, AuthenticationError):
        return {
            "type": "ProviderAuthenticationError",
            "message": "模型服务认证失败，请管理员检查服务凭据。",
        }
    if isinstance(provider_error, PermissionDeniedError):
        return {
            "type": "ProviderPermissionError",
            "message": "模型服务拒绝了当前请求，请管理员检查模型权限。",
        }
    if isinstance(provider_error, (BadRequestError, NotFoundError)):
        return {
            "type": "ProviderConfigurationError",
            "message": "模型服务配置与当前请求不兼容，请管理员检查模型和接口配置。",
        }
    if isinstance(provider_error, APIConnectionError):
        return {
            "type": "ProviderConnectionError",
            "message": "暂时无法连接模型服务，请稍后重试。",
        }
    if isinstance(provider_error, APIStatusError):
        return {
            "type": "ProviderUnavailableError",
            "message": "模型服务暂时不可用，请稍后重试。",
        }
    return {"type": type(exc).__name__, "message": str(exc)}


def generation_error_http_status(error: dict[str, str]) -> int:
    error_type = error.get("type")
    if error_type == "TimeoutError":
        return 504
    if error_type == "ProviderRateLimitError":
        return 429
    if error_type and error_type.startswith("Provider"):
        return 503
    return 500
