"""Current browser-workspace integration tests."""
from pathlib import Path


def test_frontend_web_app_files_exist():
    root = Path(__file__).resolve().parents[2]
    frontend = root / "frontend" / "src"

    assert (frontend / "App.tsx").exists()
    assert (frontend / "components" / "workspace" / "EngineeringWorkspace.tsx").exists()
    assert (frontend / "components" / "project" / "ProjectStart.tsx").exists()
    assert (frontend / "components" / "viewer" / "MechanicalWorkspace.tsx").exists()
    assert (frontend / "components" / "export" / "ExportDialog.tsx").exists()


def test_export_dialog_uses_real_artifact_download_service():
    root = Path(__file__).resolve().parents[2]
    content = (root / "frontend" / "src" / "components" / "export" / "ExportDialog.tsx").read_text(encoding="utf-8")

    assert "downloadEngineeringArtifact" in content
    assert "buildArtifactManifest" not in content


def test_backend_app_exposes_factory_for_web_hosting():
    root = Path(__file__).resolve().parents[1]
    content = (root / "app" / "main.py").read_text(encoding="utf-8")

    assert "def create_app()" in content
    assert "FRONTEND_DIST" in content
    assert "StaticFiles" in content
    assert "app = create_app()" in content
