"""Deploy-config correctness — the things that break a *fresh* deploy.

Angle: DEPLOY CONFIG. Hermetic — no Docker, no real LLM, no network.
Covers:
  1. Settings parses env vars (MOONSHOT_API_KEY, CORS_ORIGINS JSON list, API_KEYS,
     BUILD_VOLUME_MM, MIN_WALL_MM, TRUST_PROXY_HEADERS).
  2. make_llm_client raises without key, sets timeout/retries with key.
  3. _startup_self_check reports missing-key + docker problems; /ready reflects them.
  4. docker-compose.yml is valid YAML with backend+frontend + docker.sock + cad_data.
  5. backend/.env.example contains the documented keys.
  6. frontend/nginx.conf proxies /api and /ws with WS-upgrade headers; balanced braces.
  7. setup.sh passes `bash -n`.
  8. requirements.txt has no 'anthropic'; pins trimesh + numpy.
"""
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import Settings, make_llm_client


# Repo layout: this file is backend/tests/test_deploy_config.py
#   backend/   -> parents[1]
#   repo root  -> parents[2]   (docker-compose.yml, setup.sh live here)
BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
SETUP_SH = REPO_ROOT / "setup.sh"
NGINX_CONF = REPO_ROOT / "frontend" / "nginx.conf"
ENV_EXAMPLE = BACKEND_DIR / ".env.example"
REQUIREMENTS = BACKEND_DIR / "requirements.txt"


# ---------------------------------------------------------------------------
# 1. Settings loads from environment
# ---------------------------------------------------------------------------
# All Settings() constructions pass _env_file=None so the test is hermetic and
# independent of any backend/.env that may exist on the host.

def test_settings_defaults_are_safe(monkeypatch):
    """Fresh defaults: no auth, no key, sane print gate."""
    # Clear any env that could leak in.
    for k in ("MOONSHOT_API_KEY", "DASHSCOPE_API_KEY", "VISION_MODEL", "CORS_ORIGINS", "API_KEYS",
              "BUILD_VOLUME_MM", "MIN_WALL_MM", "TRUST_PROXY_HEADERS", "DEFAULT_INVITE_CODES"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.dashscope_api_key is None
    assert s.moonshot_api_key is None
    assert s.llm_provider == "moonshot"
    assert s.llm_model == "kimi-k2.7-code"
    assert s.vision_model == ""
    assert s.effective_vision_model == s.llm_model
    assert s.api_keys == []
    assert s.default_invite_codes == []  # access codes must be deployment-owned secrets
    assert s.has_llm_credentials is False
    assert s.trust_proxy_headers is False
    assert s.cors_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]
    assert s.build_volume_mm == pytest.approx(256.0)
    assert s.min_wall_mm == pytest.approx(0.8)


def test_settings_reads_moonshot_key_from_env(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-from-env")
    s = Settings(_env_file=None)
    assert s.moonshot_api_key == "sk-from-env"
    assert s.has_llm_credentials is True


def test_settings_parses_cors_origins_json_list(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", '["https://a.example", "https://b.example"]')
    s = Settings(_env_file=None)
    assert s.cors_origins == ["https://a.example", "https://b.example"]
    assert isinstance(s.cors_origins, list)


def test_settings_parses_api_keys_json_list(monkeypatch):
    monkeypatch.setenv("API_KEYS", '["k1", "k2"]')
    s = Settings(_env_file=None)
    assert s.api_keys == ["k1", "k2"]


def test_settings_empty_api_keys_list_means_auth_off(monkeypatch):
    monkeypatch.setenv("API_KEYS", "[]")
    s = Settings(_env_file=None)
    assert s.api_keys == []


def test_settings_parses_print_gate_floats(monkeypatch):
    monkeypatch.setenv("BUILD_VOLUME_MM", "180")
    monkeypatch.setenv("MIN_WALL_MM", "1.2")
    s = Settings(_env_file=None)
    assert s.build_volume_mm == pytest.approx(180.0)
    assert isinstance(s.build_volume_mm, float)
    assert s.min_wall_mm == pytest.approx(1.2)


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("1", True),
    ("false", False), ("False", False), ("0", False),
])
def test_settings_parses_trust_proxy_headers_bool(monkeypatch, raw, expected):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", raw)
    s = Settings(_env_file=None)
    assert s.trust_proxy_headers is expected


def test_settings_env_is_case_insensitive(monkeypatch):
    """pydantic-settings maps env vars case-insensitively (the .env.example uses
    UPPER_CASE while the fields are lower_case)."""
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-upper")
    monkeypatch.setenv("LLM_MODEL", "qwen-test")
    s = Settings(_env_file=None)
    assert s.moonshot_api_key == "sk-upper"
    assert s.llm_model == "qwen-test"


# ---------------------------------------------------------------------------
# 2. make_llm_client
# ---------------------------------------------------------------------------

def test_make_llm_client_raises_without_key(monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "dashscope_api_key", None)
    monkeypatch.setattr(config.settings, "moonshot_api_key", None)
    monkeypatch.setattr(config.settings, "llm_provider", "moonshot")
    with pytest.raises(RuntimeError, match="MOONSHOT_API_KEY"):
        config.make_llm_client()


def test_make_llm_client_disables_timeout_and_sets_retries_with_key(monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "llm_provider", "moonshot")
    monkeypatch.setattr(config.settings, "moonshot_api_key", "sk-present")
    monkeypatch.setattr(config.settings, "llm_base_url", "https://api.moonshot.cn/v1")
    monkeypatch.setenv("LLM_TIMEOUT_S", "42")
    monkeypatch.setattr(config.settings, "llm_max_retries", 1)
    client = config.make_llm_client()
    assert client.timeout is None
    assert client.max_retries == 1
    assert str(client.base_url).startswith(config.settings.llm_base_url.rstrip("/"))


def test_make_llm_client_importable_from_module():
    # make_llm_client must be importable as the single factory (no per-module dupes).
    assert callable(make_llm_client)


# ---------------------------------------------------------------------------
# 3. _startup_self_check + /ready
# ---------------------------------------------------------------------------

def test_startup_self_check_reports_missing_key(monkeypatch):
    from app import config, main
    monkeypatch.setattr(config.settings, "dashscope_api_key", None)
    monkeypatch.setattr(config.settings, "moonshot_api_key", None)
    monkeypatch.setattr(config.settings, "llm_provider", "moonshot")
    backend = type("Backend", (), {"runtime_snapshot": lambda self: object()})()
    problems = main._startup_self_check(backend)
    assert any("MOONSHOT_API_KEY" in p for p in problems)


def test_startup_self_check_reports_docker_problem(monkeypatch):
    """An unavailable execution backend degrades readiness with its real error."""
    from app import config, main
    monkeypatch.setattr(config.settings, "llm_provider", "moonshot")
    monkeypatch.setattr(config.settings, "moonshot_api_key", "sk-present")
    backend = type(
        "Backend",
        (),
        {"runtime_snapshot": lambda self: (_ for _ in ()).throw(RuntimeError("runtime down"))},
    )()
    problems = main._startup_self_check(backend)
    assert any("ExecutionBackend" in p and "runtime down" in p for p in problems)


def test_startup_self_check_reports_execution_backend_timeout(monkeypatch):
    """A stuck runtime must degrade readiness instead of blocking API startup."""
    from app import config, main

    monkeypatch.setattr(config.settings, "llm_provider", "moonshot")
    monkeypatch.setattr(config.settings, "moonshot_api_key", "sk-present")
    backend = type(
        "Backend",
        (),
        {"runtime_snapshot": lambda self: (_ for _ in ()).throw(TimeoutError("runtime probe timed out"))},
    )()
    problems = main._startup_self_check(backend)
    assert any(
        "ExecutionBackend" in problem and "timed out" in problem
        for problem in problems
    )


def test_startup_self_check_clean_when_key_and_docker_ok(monkeypatch):
    """When the key and shared execution backend are healthy, readiness is clean."""
    from app import config, main

    monkeypatch.setattr(config.settings, "llm_provider", "moonshot")
    monkeypatch.setattr(config.settings, "moonshot_api_key", "sk-present")

    backend = type("Backend", (), {"runtime_snapshot": lambda self: object()})()
    problems = main._startup_self_check(backend)
    assert problems == []


def test_ready_endpoint_reflects_problems():
    """/ready returns 503 + problem list when startup_problems are set on state."""
    from app.main import app
    with TestClient(app) as client:
        app.state.startup_problems = ["MOONSHOT_API_KEY is required — test"]
        resp = client.get("/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert any("MOONSHOT_API_KEY" in p for p in body["problems"])


def test_ready_endpoint_ok_when_no_problems():
    from app.main import app
    with TestClient(app) as client:
        app.state.startup_problems = []
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"


def test_health_endpoint_always_ok():
    from app.main import app
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# 4. docker-compose.yml
# ---------------------------------------------------------------------------

def test_compose_file_exists():
    assert COMPOSE.is_file(), f"missing {COMPOSE}"


def test_compose_is_valid_yaml_with_services():
    data = yaml.safe_load(COMPOSE.read_text())
    assert isinstance(data, dict)
    services = data.get("services")
    assert isinstance(services, dict)
    assert "backend" in services
    assert "frontend" in services


def test_compose_backend_mounts_docker_sock_and_cad_data():
    data = yaml.safe_load(COMPOSE.read_text())
    backend = data["services"]["backend"]
    volumes = backend.get("volumes", [])
    joined = "\n".join(volumes)
    # sibling-container mode: host docker socket must be mounted
    assert "/var/run/docker.sock:/var/run/docker.sock" in joined
    # named volume for persistent data
    assert any(v.startswith("cad_data:") for v in volumes), volumes


def test_compose_declares_cad_data_named_volume():
    data = yaml.safe_load(COMPOSE.read_text())
    assert "cad_data" in data.get("volumes", {})


def test_compose_frontend_publishes_a_port():
    data = yaml.safe_load(COMPOSE.read_text())
    frontend = data["services"]["frontend"]
    ports = frontend.get("ports", [])
    assert ports, "frontend must publish a port"
    assert any(":80" in str(p) for p in ports), ports


def test_compose_backend_reads_env_file():
    data = yaml.safe_load(COMPOSE.read_text())
    backend = data["services"]["backend"]
    # env_file points at backend/.env (the file setup.sh creates from the template)
    assert backend.get("env_file") == "./backend/.env"


# ---------------------------------------------------------------------------
# 5. backend/.env.example
# ---------------------------------------------------------------------------

def test_env_example_exists():
    assert ENV_EXAMPLE.is_file(), f"missing {ENV_EXAMPLE}"


def test_env_example_contains_required_keys():
    text = ENV_EXAMPLE.read_text()
    for key in ("MOONSHOT_API_KEY", "DASHSCOPE_API_KEY", "CORS_ORIGINS", "API_KEYS"):
        assert f"{key}=" in text, f"{key} missing from .env.example"


def test_env_example_documents_print_gate_keys():
    text = ENV_EXAMPLE.read_text()
    assert "BUILD_VOLUME_MM=" in text
    assert "MIN_WALL_MM=" in text


def test_env_example_keys_are_all_recognized_by_settings():
    """Every KEY=... in .env.example must map to a real Settings field — otherwise
    a deployer sets it and it is silently ignored (extra='ignore')."""
    valid = {f.upper() for f in Settings.model_fields}
    unknown = []
    for raw in ENV_EXAMPLE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.upper() not in valid:
            unknown.append(key)
    assert not unknown, f".env.example sets unknown Settings fields: {unknown}"


# ---------------------------------------------------------------------------
# 6. frontend/nginx.conf
# ---------------------------------------------------------------------------

def test_nginx_conf_exists():
    assert NGINX_CONF.is_file(), f"missing {NGINX_CONF}"


def test_nginx_braces_balanced():
    text = NGINX_CONF.read_text()
    assert text.count("{") == text.count("}"), "unbalanced braces in nginx.conf"


def test_nginx_proxies_api_and_ws_to_backend():
    text = NGINX_CONF.read_text()
    assert "location /api/" in text
    assert "location /ws/" in text
    assert "set $cad_backend http://backend:8000;" in text
    assert "proxy_pass $cad_backend;" in text


def test_nginx_ws_has_upgrade_headers():
    """The /ws/ block must carry the WebSocket upgrade handshake headers, or the
    socket connection silently fails behind the proxy."""
    text = NGINX_CONF.read_text()
    # Isolate the /ws/ location block.
    idx = text.index("location /ws/")
    rest = text[idx:]
    # block ends at its closing brace; grab a generous window
    block = rest[: rest.index("}") + 1]
    assert "proxy_http_version 1.1" in block
    assert "Upgrade $http_upgrade" in block
    assert 'Connection "upgrade"' in block


# ---------------------------------------------------------------------------
# 7. setup.sh
# ---------------------------------------------------------------------------

def test_setup_sh_exists_and_is_bash():
    assert SETUP_SH.is_file(), f"missing {SETUP_SH}"
    first = SETUP_SH.read_text().splitlines()[0]
    assert first.startswith("#!") and "bash" in first


def test_setup_sh_passes_bash_syntax_check():
    result = subprocess.run(
        ["bash", "-n", str(SETUP_SH)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


# ---------------------------------------------------------------------------
# 8. requirements.txt
# ---------------------------------------------------------------------------

def test_requirements_has_no_anthropic():
    """anthropic was removed (LLM is Qwen via openai SDK). A leftover dep would
    pull an unused package and confuse the deploy story."""
    text = REQUIREMENTS.read_text().lower()
    assert "anthropic" not in text


def test_requirements_pins_trimesh_and_numpy():
    lines = [l.strip() for l in REQUIREMENTS.read_text().splitlines()
             if l.strip() and not l.strip().startswith("#")]
    pkgs = {}
    for line in lines:
        # split on the first version specifier char
        name = line
        for sep in ("==", ">=", "<=", "~=", "<", ">"):
            if sep in line:
                name = line.split(sep, 1)[0]
                break
        pkgs[name.strip().lower()] = line

    assert "trimesh" in pkgs, "trimesh not in requirements"
    assert "==" in pkgs["trimesh"], f"trimesh not pinned: {pkgs['trimesh']}"
    assert "numpy" in pkgs, "numpy not in requirements"
    assert "==" in pkgs["numpy"], f"numpy not pinned: {pkgs['numpy']}"


def test_requirements_has_openai_not_anthropic():
    """The LLM client is built on the openai SDK (Qwen compatible-mode)."""
    text = REQUIREMENTS.read_text().lower()
    assert "openai" in text
