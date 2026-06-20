"""Tests for serving the built web frontend."""
from fastapi.testclient import TestClient


def test_web_build_is_served_when_dist_exists(tmp_path, monkeypatch):
    frontend_dist = tmp_path / "frontend" / "dist"
    frontend_dist.mkdir(parents=True)
    (frontend_dist / "index.html").write_text("<div id=\"root\"></div>", encoding="utf-8")

    import app.main as main

    monkeypatch.setattr(main, "FRONTEND_DIST", frontend_dist)
    app = main.create_app()

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "root" in response.text


def test_api_routes_still_take_precedence(tmp_path, monkeypatch):
    frontend_dist = tmp_path / "frontend" / "dist"
    frontend_dist.mkdir(parents=True)
    (frontend_dist / "index.html").write_text("web app", encoding="utf-8")

    import app.main as main

    monkeypatch.setattr(main, "FRONTEND_DIST", frontend_dist)
    app = main.create_app()

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
