"""Phase 3 web app integration tests."""
from pathlib import Path


def test_frontend_web_app_files_exist():
    root = Path(__file__).resolve().parents[2]
    frontend = root / "frontend"

    assert (frontend / "src" / "App.tsx").exists()
    assert (frontend / "src" / "components" / "ChatPanel.tsx").exists()
    assert (frontend / "src" / "components" / "Viewer3D.tsx").exists()
    assert (frontend / "src" / "components" / "Viewer2D.tsx").exists()
    assert (frontend / "src" / "components" / "DownloadPanel.tsx").exists()


def test_download_panel_describes_web_workflow():
    root = Path(__file__).resolve().parents[2]
    content = (root / "frontend" / "src" / "components" / "DownloadPanel.tsx").read_text(encoding="utf-8")

    assert "Web {" in content
    assert "\\u4e0b\\u8f7d" in content
    assert "/api/export" not in content


def test_backend_app_exposes_factory_for_web_hosting():
    root = Path(__file__).resolve().parents[1]
    content = (root / "app" / "main.py").read_text(encoding="utf-8")

    assert "def create_app()" in content
    assert "FRONTEND_DIST" in content
    assert "StaticFiles" in content
    assert "app = create_app()" in content
