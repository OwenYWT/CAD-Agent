from app.config import Settings


# Regression: FUSION-007 — development advertised a disabled durable control
# plane even though the browser write route had no non-durable implementation.


def test_interactive_development_fails_closed_when_durable_is_disabled() -> None:
    settings = Settings(
        _env_file=None,
        app_environment="development",
        durable_control_plane_enabled=False,
    )

    problems = settings.durable_control_plane_config_problems()

    assert any(
        "DURABLE_CONTROL_PLANE_ENABLED" in problem
        and "browser" in problem
        for problem in problems
    )


def test_hermetic_test_process_may_explicitly_disable_durable() -> None:
    settings = Settings(
        _env_file=None,
        app_environment="test",
        durable_control_plane_enabled=False,
    )

    assert settings.durable_control_plane_config_problems() == []
