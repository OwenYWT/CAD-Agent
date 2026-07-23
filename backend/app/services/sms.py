import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class SmsDeliveryError(Exception):
    """Raised when a verification code could not be delivered."""


@dataclass(frozen=True)
class SmsDeliveryResult:
    provider: str
    message_id: str | None = None


def _provider() -> str:
    return settings.sms_provider.strip().lower()


def _format_phone(phone: str) -> str:
    normalized = phone.strip()
    if normalized.startswith("+"):
        return normalized
    return settings.sms_default_country_code.strip() + normalized


def _template_params(code: str, expires_in_minutes: int) -> list[str]:
    values = {
        "code": code,
        "minutes": str(expires_in_minutes),
        "expires_in_minutes": str(expires_in_minutes),
    }
    names = [item.strip() for item in settings.tencent_sms_template_param_order.split(",") if item.strip()]
    return [values.get(name, name) for name in names]


def _tc3_sign(secret_key: str, date: str, service: str, string_to_sign: str) -> str:
    def sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    secret_date = sign(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = sign(secret_date, service)
    secret_signing = sign(secret_service, "tc3_request")
    return hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


async def _send_tencentcloud(phone: str, code: str, purpose: str) -> SmsDeliveryResult:
    endpoint = settings.tencent_sms_endpoint.strip() or "https://sms.tencentcloudapi.com"
    parsed = urlparse(endpoint)
    host = parsed.netloc or endpoint.replace("https://", "").replace("http://", "").split("/", 1)[0]
    url = endpoint if parsed.scheme else f"https://{endpoint}"
    timestamp = int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
    payload = {
        "PhoneNumberSet": [_format_phone(phone)],
        "SmsSdkAppId": settings.tencent_sms_sdk_app_id.strip(),
        "SignName": settings.tencent_sms_sign_name.strip(),
        "TemplateId": settings.tencent_sms_template_id.strip(),
        "TemplateParamSet": _template_params(code, settings.verification_code_ttl_minutes),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    hashed_payload = hashlib.sha256(payload_bytes).hexdigest()
    canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{host}\nx-tc-action:sendsms\n"
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = "\n".join([
        "POST",
        "/",
        "",
        canonical_headers,
        signed_headers,
        hashed_payload,
    ])
    service = "sms"
    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256",
        str(timestamp),
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = _tc3_sign(settings.tencent_secret_key.strip(), date, service, string_to_sign)
    authorization = (
        "TC3-HMAC-SHA256 "
        f"Credential={settings.tencent_secret_id.strip()}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-TC-Action": "SendSms",
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": "2021-01-11",
        "X-TC-Region": settings.tencent_sms_region.strip(),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, content=payload_bytes, headers=headers)
    except httpx.HTTPError as exc:
        raise SmsDeliveryError("短信服务连接失败，请稍后重试") from exc
    if response.status_code >= 400:
        logger.warning("Tencent SMS HTTP error %s for purpose=%s: %s", response.status_code, purpose, response.text)
        raise SmsDeliveryError("短信服务暂时不可用，请稍后重试")
    try:
        body = response.json()
    except ValueError as exc:
        raise SmsDeliveryError("短信服务返回异常，请稍后重试") from exc
    tc_response = body.get("Response", {}) if isinstance(body, dict) else {}
    if "Error" in tc_response:
        error = tc_response["Error"]
        logger.warning("Tencent SMS API error for purpose=%s: %s", purpose, error)
        raise SmsDeliveryError(str(error.get("Message") or "短信发送失败，请稍后重试"))
    statuses = tc_response.get("SendStatusSet") or []
    failed = [item for item in statuses if item.get("Code") != "Ok"]
    if failed:
        logger.warning("Tencent SMS send failure for purpose=%s: %s", purpose, failed)
        message = failed[0].get("Message") or "短信发送失败，请稍后重试"
        raise SmsDeliveryError(message)
    request_id = tc_response.get("RequestId")
    return SmsDeliveryResult(provider="tencentcloud", message_id=request_id)


async def _send_webhook(phone: str, code: str, purpose: str) -> SmsDeliveryResult:
    payload = {
        "phone": phone,
        "normalized_phone": _format_phone(phone),
        "code": code,
        "purpose": purpose,
        "expires_in_minutes": settings.verification_code_ttl_minutes,
        "message": f"您的 CAD Agent 验证码是 {code}，{settings.verification_code_ttl_minutes} 分钟内有效。",
    }
    headers: dict[str, str] = {"Content-Type": "application/json"}
    token = settings.sms_webhook_bearer_token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(settings.sms_webhook_url.strip(), json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise SmsDeliveryError("短信服务连接失败，请稍后重试") from exc
    if response.status_code >= 400:
        logger.warning("SMS webhook HTTP error %s: %s", response.status_code, response.text)
        raise SmsDeliveryError("短信服务暂时不可用，请稍后重试")
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict) and body.get("ok") is False:
        raise SmsDeliveryError(str(body.get("message") or "短信发送失败，请稍后重试"))
    message_id = str(body.get("message_id")) if isinstance(body, dict) and body.get("message_id") else None
    return SmsDeliveryResult(provider="webhook", message_id=message_id)


async def send_verification_code_sms(phone: str, code: str, purpose: str) -> SmsDeliveryResult:
    provider = _provider()
    if provider == "tencentcloud":
        return await _send_tencentcloud(phone, code, purpose)
    if provider == "webhook":
        return await _send_webhook(phone, code, purpose)
    if provider in {"log", "console"} and not settings.auth_required:
        logger.info("Verification code for %s (%s): %s", phone, purpose, code)
        return SmsDeliveryResult(provider=provider)
    raise SmsDeliveryError("短信服务未配置，无法发送验证码")
