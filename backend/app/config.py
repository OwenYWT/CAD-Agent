from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    dashscope_api_key: str | None = None
    moonshot_api_key: str | None = None
    llm_provider: str = "moonshot"
    llm_base_url: str = "https://api.moonshot.cn/v1"
    llm_model: str = "kimi-k2.7-code"
    llm_reasoning_effort: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: str | None = None
    azure_openai_api_version: str = "2025-03-01-preview"
    sandbox_runtime: str = "docker"
    sandbox_command: str | None = None
    sandbox_image: str = "cad-agent-sandbox:latest"
    sandbox_timeout_s: int = 60
    sandbox_memory_limit: str = "512m"
    sandbox_max_concurrent: int = 4  # cap simultaneous container spawns (each = CPU+RAM)
    file_storage_dir: str = "./data/files"
    history_db_path: str = "./data/history.db"
    file_ttl_hours: int = 24  # generated files older than this are cleaned up
    capability_upload_max_bytes: int = 64 * 1024 * 1024
    # Physical-device networking is deployment opt-in. Request parameters and UI
    # confirmations cannot turn this on.
    cadskills_enable_bambu_lan: bool = False
    # Optional deployment-owned command prefix for untrusted Python/JS generators.
    # Leave empty to keep source execution blocked; never accept this from an API.
    cadskills_isolated_executor: list[str] = []
    cors_origins: list[str] = ["http://localhost:5173"]
    log_level: str = "info"
    api_keys: list[str] = []  # empty = no legacy API-key auth required
    auth_required: bool = True
    # HMAC signing key for login/session tokens. MUST be set to a long random value
    # in any deployment. Empty by default ON PURPOSE so a misconfigured prod fails
    # loud (see assert_auth_config_safe) instead of silently signing with a value
    # that is public in the repo. Never commit a real value here or in .env.example.
    auth_token_secret: str = ""
    auth_token_ttl_hours: int = 24 * 14
    verification_code_ttl_minutes: int = 10
    # When True, /api/auth/code/request echoes the verification code back in the HTTP
    # response (dev convenience, NO SMS). MUST be False in production — otherwise
    # anyone who knows a phone number can obtain its code and take over the account.
    auth_dev_expose_code: bool = False
    # Temporary invite-only mode: keep SMS/code registration disabled until a real SMS
    # provider is ready for production. Password login still works for registered users.
    auth_code_flows_enabled: bool = False
    # Verification-code delivery. Production auth must configure a real provider.
    # Supported values:
    #   tencentcloud - sends via Tencent Cloud SMS SendSms API
    #   webhook      - POSTs the code payload to an operator-owned delivery endpoint
    #   log/disabled - local development only; rejected by assert_auth_config_safe()
    sms_provider: str = "disabled"
    sms_default_country_code: str = "+86"
    sms_webhook_url: str = ""
    sms_webhook_bearer_token: str = ""
    tencent_secret_id: str = ""
    tencent_secret_key: str = ""
    tencent_sms_sdk_app_id: str = ""
    tencent_sms_sign_name: str = ""
    tencent_sms_template_id: str = ""
    tencent_sms_region: str = "ap-guangzhou"
    tencent_sms_endpoint: str = "https://sms.tencentcloudapi.com"
    # Comma-separated template variable order. Most Tencent templates use the
    # verification code and expiry minutes, but operators can adapt this without code.
    tencent_sms_template_param_order: str = "code,minutes"
    # Fixed private-beta invite codes. When non-empty, registration accepts only these
    # seeded codes; each is single-use by default.
    default_invite_codes: list[str] = [
        "CAD1-A7K9",
        "CAD2-M4Q8",
        "CAD3-Z6P2",
        "CAD4-H9R5",
        "CAD5-T2N7",
        "CAD6-W8L3",
        "CAD7-Q5X1",
        "CAD8-B3V6",
        "CAD9-J2Y4",
        "CAD0-S9D8",
    ]
    # Legacy single invite seed. Prefer DEFAULT_INVITE_CODES for new deployments.
    default_invite_code: str = ""
    default_invite_max_uses: int = 1
    # Bootstrap password for the built-in "admin" account. Empty = admin is NOT
    # auto-created (the safe default). Set to a strong value to provision admin.
    admin_password: str = ""
    rate_limit_per_minute: int = 30
    # Behind a trusted reverse proxy / tunnel (nginx, cloudflared), the direct client
    # IP is the proxy's, so all users share one rate-limit bucket. Enable ONLY when a
    # trusted proxy sets X-Forwarded-For (otherwise the header is spoofable).
    trust_proxy_headers: bool = False

    # Autodesk Fusion 360 connector. Backend and Add-in use different Runtime
    # credentials; real values belong in deployment-owned secret files/env only.
    fusion_runtime_url: str = "http://127.0.0.1:8765"
    fusion_runtime_backend_secret: str = ""
    fusion_runtime_backend_secret_file: str = ""
    fusion_runtime_timeout_s: float = 305.0
    fusion_cloud_enabled: bool = False
    fusion_aps_client_id: str = ""
    fusion_aps_client_secret: str = ""
    fusion_aps_redirect_uri: str = ""
    fusion_token_encryption_key: str = ""
    fusion_token_db_path: str = "./data/fusion360/tokens.db"
    # Direct Fusion Desktop -> HTTPS Cloud Agent. These locations contain only
    # bounded audit metadata and explicitly authorized export uploads.
    fusion_agent_db_path: str = "./data/fusion360/agent-audit.db"
    fusion_agent_artifact_dir: str = "./data/fusion360/agent-artifacts"
    fusion_agent_artifact_max_bytes: int = Field(default=64 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)
    fusion_agent_plan_ttl_s: int = Field(default=300, ge=30, le=3_600)
    # Empty means use LLM_MODEL; no separate credentials or hidden fallback.
    fusion_agent_model: str = ""

    # 3D-printing feasibility gate (used by GeometryValidator in print/default mode)
    build_volume_mm: float = 256.0  # cubic build volume edge (Bambu A1 mini ≈ 180, generic FDM ≈ 256)
    min_wall_mm: float = 0.8  # minimum printable wall thickness (2x 0.4mm nozzle line)

    # LLM call resilience (SDK default read timeout is 600s — far too long for an
    # interactive product; one slow call would block the whole generate request).
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 1
    # Overall hard deadline for one generate()/modify() pipeline (planning + N×LLM +
    # N×sandbox). Beyond this we fail fast instead of hanging a tester's request.
    generate_deadline_s: float = 180.0

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def normalized_llm_provider(self) -> str:
        return self.llm_provider.strip().lower()

    @property
    def has_llm_credentials(self) -> bool:
        if self.normalized_llm_provider == "azure":
            return bool(self.azure_openai_endpoint and self.azure_openai_api_key)
        return bool(self.llm_api_key)

    @property
    def llm_api_key(self) -> str | None:
        if self.normalized_llm_provider == "moonshot":
            return self.moonshot_api_key
        return self.dashscope_api_key

    @property
    def llm_credentials_error(self) -> str:
        if self.normalized_llm_provider == "azure":
            return "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY are required for Azure OpenAI operations"
        if self.normalized_llm_provider == "moonshot":
            return "MOONSHOT_API_KEY is required when LLM_PROVIDER=moonshot"
        return "LLM credentials are required: set Azure OpenAI variables when LLM_PROVIDER=azure, MOONSHOT_API_KEY when LLM_PROVIDER=moonshot, or DASHSCOPE_API_KEY when LLM_PROVIDER=openai_compatible"

    # Values that previously shipped as defaults and would silently weaken auth if
    # left in place. Refuse to boot with auth on while any of these is in effect.
    _INSECURE_SECRETS = {"", "change-me-in-production", "cad-agent-dev-secret"}

    def auth_config_problems(self) -> list[str]:
        """Return human-readable reasons the auth config is unsafe for a real
        deployment. Empty list = safe. Only meaningful when auth_required is True."""
        problems: list[str] = []
        if not self.auth_required:
            return problems
        if (self.auth_token_secret or "").strip() in self._INSECURE_SECRETS:
            problems.append(
                "AUTH_TOKEN_SECRET is empty or a known placeholder. Set it to a long "
                "random value (e.g. `python -c \"import secrets; print(secrets.token_urlsafe(48))\"`). "
                "Tokens are HMAC-signed with this key; a public/empty key lets anyone forge any user's session."
            )
        if self.auth_dev_expose_code:
            problems.append(
                "AUTH_DEV_EXPOSE_CODE is True: verification codes are returned in HTTP responses. "
                "This must be False in production (it bypasses SMS and enables account takeover)."
            )
        if self.auth_code_flows_enabled:
            provider_problem = self.sms_config_problem()
            if provider_problem:
                problems.append(provider_problem)
        return problems

    def assert_auth_config_safe(self) -> None:
        """Fail loud at startup if auth is enabled with an unsafe config."""
        problems = self.auth_config_problems()
        if problems:
            raise RuntimeError(
                "Unsafe auth configuration (set AUTH_REQUIRED=false only for local dev):\n  - "
                + "\n  - ".join(problems)
            )

    def sms_config_problem(self) -> str | None:
        if not self.auth_required:
            return None
        provider = self.sms_provider.strip().lower()
        if provider in {"", "disabled", "none", "log", "console"}:
            return (
                "SMS_PROVIDER is not configured for a real delivery service. "
                "Set SMS_PROVIDER=tencentcloud with Tencent Cloud SMS credentials, "
                "or SMS_PROVIDER=webhook with SMS_WEBHOOK_URL. Without this, production "
                "users cannot receive verification codes."
            )
        if provider == "webhook":
            if not self.sms_webhook_url.strip():
                return "SMS_PROVIDER=webhook requires SMS_WEBHOOK_URL."
            return None
        if provider == "tencentcloud":
            missing = [
                name for name, value in {
                    "TENCENT_SECRET_ID": self.tencent_secret_id,
                    "TENCENT_SECRET_KEY": self.tencent_secret_key,
                    "TENCENT_SMS_SDK_APP_ID": self.tencent_sms_sdk_app_id,
                    "TENCENT_SMS_SIGN_NAME": self.tencent_sms_sign_name,
                    "TENCENT_SMS_TEMPLATE_ID": self.tencent_sms_template_id,
                }.items()
                if not value.strip()
            ]
            if missing:
                return "SMS_PROVIDER=tencentcloud requires: " + ", ".join(missing) + "."
            return None
        return f"SMS_PROVIDER={self.sms_provider!r} is unsupported. Use tencentcloud or webhook."


settings = Settings()


def make_llm_client():
    """Single factory for the LLM client.

    Supports both Azure OpenAI and OpenAI-compatible providers. The returned
    adapter also normalizes GPT-5 parameters such as max_completion_tokens and
    reasoning_effort.
    """
    from app.llm import create_llm_client

    return create_llm_client(settings)
