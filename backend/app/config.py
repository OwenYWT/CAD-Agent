from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    dashscope_api_key: str | None = None
    llm_provider: str = "openai_compatible"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen-plus"
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
    cors_origins: list[str] = ["http://localhost:5173"]
    log_level: str = "info"
    api_keys: list[str] = []  # empty = no legacy API-key auth required
    auth_required: bool = True
    auth_token_secret: str = "change-me-in-production"
    auth_token_ttl_hours: int = 24 * 14
    verification_code_ttl_minutes: int = 10
    # Local-dev invite code. Leave empty to disable default invite seeding.
    default_invite_code: str = "CAD-AGENT-2026"
    default_invite_max_uses: int = 100
    admin_password: str = "admin123456"
    rate_limit_per_minute: int = 30
    # Behind a trusted reverse proxy / tunnel (nginx, cloudflared), the direct client
    # IP is the proxy's, so all users share one rate-limit bucket. Enable ONLY when a
    # trusted proxy sets X-Forwarded-For (otherwise the header is spoofable).
    trust_proxy_headers: bool = False

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
        return bool(self.dashscope_api_key)

    @property
    def llm_credentials_error(self) -> str:
        if self.normalized_llm_provider == "azure":
            return "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY are required for Azure OpenAI operations"
        return "LLM credentials are required: set Azure OpenAI variables when LLM_PROVIDER=azure, or DASHSCOPE_API_KEY when LLM_PROVIDER=openai_compatible"


settings = Settings()


def make_llm_client():
    """Single factory for the LLM client.

    Supports both Azure OpenAI and OpenAI-compatible providers. The returned
    adapter also normalizes GPT-5 parameters such as max_completion_tokens and
    reasoning_effort.
    """
    from app.llm import create_llm_client

    return create_llm_client(settings)
