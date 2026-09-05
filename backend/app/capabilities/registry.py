"""Manifest registry for the 11 vendored cadskills capabilities.

This module deliberately performs only local, bounded dependency checks. Network
services and physical devices are never probed by a catalog GET request.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from app.capabilities.models import (
    CapabilityAction,
    CapabilityDependency,
    CapabilityManifest,
    UpstreamProvenance,
)
from app.config import settings
from app.execution.composition import get_execution_backend


UPSTREAM_URL = "https://github.com/earthtojake/text-to-cad"


def _find_repo_root() -> Path:
    """Locate the repository independently of cwd or the process entry point."""
    source = Path(__file__).resolve()
    for parent in source.parents:
        if (parent / "backend" / "app").is_dir() and (parent / "third_party" / "cadskills").is_dir():
            return parent
    # Keep paths deterministic even in a partial package/test installation.
    return source.parents[3]


REPO_ROOT = _find_repo_root()
VENDORED_ROOT = REPO_ROOT / "third_party" / "cadskills"
SKILLS_ROOT = VENDORED_ROOT / "skills"


def _upstream() -> UpstreamProvenance:
    version = "0.3.9"
    commit = "fdbb4b4fb62d95ae298cfe9a46fdc7092bdaf423"
    try:
        text = (VENDORED_ROOT / "UPSTREAM.md").read_text(encoding="utf-8")
        version_match = re.search(r"Upstream release:\s*`([^`]+)`", text)
        commit_match = re.search(r"Upstream commit:\s*`([0-9a-f]{40})`", text)
        if version_match:
            version = version_match.group(1)
        if commit_match:
            commit = commit_match.group(1)
    except OSError:
        # The missing provenance file is also exposed as a blocked vendored-runtime
        # dependency; retain the pinned values so the API contract stays traceable.
        pass
    return UpstreamProvenance(version=version, commit=commit, url=UPSTREAM_URL)


def _action(
    action_id: str,
    name: str,
    mode: str = "compute",
    *,
    confirm: bool = False,
) -> CapabilityAction:
    return CapabilityAction(
        id=action_id,
        name=name,
        mode=mode,
        requires_confirmation=confirm,
    )


def _dependency(
    dependency_id: str,
    label: str,
    kind: str,
    required: bool,
    available: bool | None,
    detail: str,
) -> CapabilityDependency:
    return CapabilityDependency(
        id=dependency_id,
        label=label,
        kind=kind,
        required=required,
        available=available,
        detail=detail,
    )


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _command_path(*names: str) -> str | None:
    for name in names:
        if not name:
            continue
        configured = Path(name).expanduser()
        if configured.is_absolute() and configured.is_file():
            return str(configured)
        found = shutil.which(name)
        if found:
            return found
    return None


def _vendored_dependency(skill_id: str) -> CapabilityDependency:
    skill_file = SKILLS_ROOT / skill_id / "SKILL.md"
    provenance_file = VENDORED_ROOT / "UPSTREAM.md"
    available = skill_file.is_file() and provenance_file.is_file()
    detail = (
        f"Pinned runtime found at {skill_file.relative_to(REPO_ROOT)}."
        if available
        else f"Missing pinned runtime or provenance under {VENDORED_ROOT}."
    )
    return _dependency("vendored-runtime", "Pinned cadskills runtime", "vendored", True, available, detail)


def _cad_dependencies() -> list[CapabilityDependency]:
    return [
        _execution_backend_dependency(
            "cad-runtime",
            "Unified STEP CAD execution runtime",
        )
    ]


def _dxf_dependencies() -> list[CapabilityDependency]:
    return [
        _execution_backend_dependency(
            "dxf-runtime",
            "Unified ezdxf execution runtime",
        )
    ]


def _execution_backend_dependency(
    dependency_id: str,
    label: str,
) -> CapabilityDependency:
    try:
        snapshot = get_execution_backend().runtime_snapshot()
    except Exception as exc:
        return _dependency(
            dependency_id,
            label,
            "runtime",
            True,
            False,
            f"Configured ExecutionBackend is unavailable: {str(exc)[:240]}",
        )
    versions = ", ".join(
        f"{name}={version}"
        for name, version in sorted(snapshot.versions.items())
    )
    detail = (
        f"ExecutionBackend ready at {snapshot.image_digest} "
        f"({snapshot.platform}; {versions or 'runtime versions recorded by worker'})."
    )
    return _dependency(
        dependency_id,
        label,
        "runtime",
        True,
        True,
        detail,
    )


def _node_dependency() -> CapabilityDependency:
    path = _command_path("node")
    return _dependency(
        "node",
        "Node.js runtime",
        "executable",
        True,
        path is not None,
        f"Executable found at {path}." if path else "The node executable was not found on PATH.",
    )


def _python_runtime_dependency() -> CapabilityDependency:
    available = sys.version_info >= (3, 11)
    return _dependency(
        "python",
        "Python runtime",
        "executable",
        True,
        available,
        f"Running Python {sys.version_info.major}.{sys.version_info.minor}; 3.11 or newer is required.",
    )


def _viewer_dependencies() -> list[CapabilityDependency]:
    runtime = SKILLS_ROOT / "cad-viewer" / "scripts" / "viewer"
    files_ok = all((runtime / path).is_file() for path in ("package.json", "backend/server.mjs", "dist/index.html"))
    launcher_available = False
    try:
        package = json.loads((runtime / "package.json").read_text(encoding="utf-8"))
        launcher_available = "agent:start" in package.get("scripts", {})
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return [
        _node_dependency(),
        _dependency(
            "viewer-bundle",
            "CAD Viewer server and dist bundle",
            "vendored",
            True,
            files_ok,
            f"Viewer bundle found at {runtime.relative_to(REPO_ROOT)}."
            if files_ok
            else f"Viewer runtime is incomplete under {runtime}.",
        ),
        _dependency(
            "viewer-agent-launcher",
            "CAD Viewer agent:start launcher",
            "vendored",
            False,
            launcher_available,
            "The package defines the documented agent:start reuse/activation launcher."
            if launcher_available
            else "Optional reuse/activation launcher is absent; the adapter can still start the bundled server directly.",
        ),
    ]


def _implicit_dependencies() -> list[CapabilityDependency]:
    return [
        _execution_backend_dependency(
            "implicit-runtime",
            "Unified implicit-CAD execution runtime",
        ),
    ]


def _gcode_dependencies() -> list[CapabilityDependency]:
    candidates = ("OrcaSlicer", "orca-slicer", "orcaslicer", "prusa-slicer", "PrusaSlicer", "CuraEngine", "curaengine")
    path = _command_path(*candidates)
    return [
        _python_runtime_dependency(),
        _dependency(
            "slicer-cli",
            "FDM slicer CLI",
            "executable",
            False,
            path is not None,
            f"Compatible slicer found at {path}." if path else "No OrcaSlicer, PrusaSlicer, or CuraEngine CLI was found.",
        ),
    ]


def _network_dependency(dependency_id: str, label: str, detail: str) -> CapabilityDependency:
    # A catalog GET must not perform DNS/network traffic, and network reachability
    # cannot be inferred from a vendored client script.
    return _dependency(dependency_id, label, "network", True, None, detail)


def _bambu_dependencies() -> list[CapabilityDependency]:
    config_path = REPO_ROOT / "bambu-printers.json"
    configured = False
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            configured = bool(data.get("printers")) if isinstance(data, dict) else False
        except (OSError, json.JSONDecodeError):
            configured = False
    return [
        _python_runtime_dependency(),
        _dependency(
            "bambu-printer-config",
            "Bambu LAN printer configuration",
            "configuration",
            True,
            configured,
            f"Printer configuration found at {config_path}."
            if configured
            else "No usable bambu-printers.json printer configuration was found at the repository root.",
        ),
        _network_dependency(
            "bambu-lan",
            "Reachable Bambu Lab printer on the local network",
            "Printer reachability and credentials are verified only during an explicitly requested action.",
        ),
    ]


def _step_parts_dependencies() -> list[CapabilityDependency]:
    return [
        _python_runtime_dependency(),
        _network_dependency(
            "step-parts-api",
            "api.step.parts service",
            "API reachability is checked at action time; the read-only catalog endpoint does not probe external services.",
        ),
    ]


def _sendcutsend_dependencies() -> list[CapabilityDependency]:
    return [
        _network_dependency(
            "sendcutsend-sources",
            "SendCutSend ordering guide and material/spec feeds",
            "Official source reachability and current schema are checked at review time, not by the catalog endpoint.",
        )
    ]


def _robot_dependencies() -> list[CapabilityDependency]:
    return [_python_runtime_dependency(), _isolated_executor_dependency()]


def _isolated_executor_dependency() -> CapabilityDependency:
    configured = bool(settings.cadskills_isolated_executor)
    return _dependency(
        "isolated-executor",
        "Deployment-owned isolated generator executor",
        "configuration",
        True,
        configured,
        "An isolated generator command prefix is configured by the deployment."
        if configured
        else "Uploaded Python/JavaScript generator execution is disabled until CADSKILLS_ISOLATED_EXECUTOR is configured.",
    )


_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "cad",
        "name": "Parametric CAD",
        "group": "geometry",
        "summary": "Create, modify, inspect, validate, and export STEP-first parametric parts and assemblies.",
        "maturity": "stable",
        "risk_level": "compute",
        "actions": [
            _action("generate", "Generate STEP-first CAD"),
            _action("inspect", "Inspect geometry and references", "read_only"),
            _action("snapshot", "Render validation snapshots"),
            _action("export", "Export STL, 3MF, or GLB"),
        ],
        "accepts": ["text/plain", ".py", ".step", ".stp"],
        "produces": [".py", ".step", ".stp", ".stl", ".3mf", ".glb", ".png", ".gif"],
        "dependencies": _cad_dependencies,
    },
    {
        "id": "dxf",
        "name": "DXF Drawings",
        "group": "geometry",
        "summary": "Generate and deterministically validate 1:1 2D DXF drawings from Python ezdxf sources.",
        "maturity": "stable",
        "risk_level": "compute",
        "actions": [_action("generate", "Generate DXF"), _action("validate", "Validate DXF geometry", "read_only")],
        "accepts": ["text/plain", ".py", ".step", ".stp", ".dxf"],
        "produces": [".py", ".dxf"],
        "dependencies": _dxf_dependencies,
    },
    {
        "id": "cad-viewer",
        "name": "CAD Viewer",
        "group": "review",
        "summary": "Start or reuse a local viewer and provide review links for CAD, robot-description, implicit, and G-code files.",
        "maturity": "stable",
        "risk_level": "compute",
        "actions": [_action("start", "Start or reuse viewer"), _action("review", "Create file review link", "read_only")],
        "accepts": [".step", ".stp", ".glb", ".stl", ".3mf", ".gcode", ".dxf", ".urdf", ".srdf", ".sdf", ".implicit.js", ".implicit.mjs"],
        "produces": ["text/html", "http-url"],
        "dependencies": _viewer_dependencies,
    },
    {
        "id": "implicit-cad",
        "name": "Implicit CAD",
        "group": "geometry",
        "summary": "Author, render, snapshot, and mesh-export browser-native GLSL signed-distance-field models.",
        "maturity": "experimental",
        "risk_level": "compute",
        "actions": [_action("snapshot", "Render implicit model snapshot"), _action("export", "Export implicit model mesh")],
        "accepts": ["text/plain", ".implicit.js", ".implicit.mjs"],
        "produces": [".implicit.js", ".implicit.mjs", ".glb", ".stl", ".3mf", ".png", ".gif"],
        "dependencies": _implicit_dependencies,
    },
    {
        "id": "gcode",
        "name": "FDM G-code",
        "group": "manufacturing",
        "summary": "Discover slicers and inspect, slice, and statically validate plain FDM G-code.",
        "maturity": "beta",
        "risk_level": "compute",
        "actions": [
            _action("discover", "Discover slicer backends", "read_only"),
            _action("inspect", "Inspect mesh slice readiness", "read_only"),
            _action("dry-run", "Preview slicer command", "read_only"),
            _action("slice", "Slice mesh to G-code"),
            _action("validate", "Validate G-code", "read_only"),
        ],
        "accepts": [".stl", ".obj", ".3mf", ".ply", ".glb", ".gltf", ".gcode", ".json"],
        "produces": [".gcode", "application/json"],
        "dependencies": _gcode_dependencies,
    },
    {
        "id": "bambu-labs",
        "name": "Bambu Lab LAN Printing",
        "group": "manufacturing",
        "summary": "Dry-run, upload, monitor, and cautiously control validated Bambu Lab print jobs over LAN FTPS/MQTT.",
        "maturity": "beta",
        "risk_level": "physical_action",
        "actions": [
            _action("dry-run", "Plan printer handoff", "read_only"),
            _action("serial", "Read printer serial", "read_only", confirm=True),
            _action("status", "Read printer status", "read_only"),
            _action("upload", "Upload print job", "external_write", confirm=True),
            _action("start-print", "Start print job", "physical_action", confirm=True),
            _action("pause-print", "Pause print job", "physical_action", confirm=True),
            _action("cancel-print", "Cancel print job", "physical_action", confirm=True),
            _action("clear-error", "Clear printer error", "external_write", confirm=True),
        ],
        "accepts": [".gcode", ".gcode.3mf", ".json"],
        "produces": [".gcode.3mf", "application/json", "printer-job"],
        "dependencies": _bambu_dependencies,
    },
    {
        "id": "step-parts",
        "name": "STEP Parts Catalog",
        "group": "catalog",
        "summary": "Search step.parts for purchasable components and download checksum-verified canonical STEP files.",
        "maturity": "stable",
        "risk_level": "external_write",
        "actions": [_action("search", "Search parts catalog", "read_only"), _action("download", "Download and verify STEP part", "external_write")],
        "accepts": ["text/plain", "part-id"],
        "produces": ["application/json", ".step"],
        "dependencies": _step_parts_dependencies,
    },
    {
        "id": "sendcutsend",
        "name": "SendCutSend Preflight",
        "group": "manufacturing",
        "summary": "Review DXF and STEP uploads against current SendCutSend ordering, material, and service evidence.",
        "maturity": "beta",
        "risk_level": "read_only",
        "actions": [_action("preflight", "Run SendCutSend upload preflight", "read_only")],
        "accepts": [".dxf", ".step", ".stp", "material-sku", "service-selection"],
        "produces": ["application/json", "text/markdown"],
        "dependencies": _sendcutsend_dependencies,
    },
    {
        "id": "urdf",
        "name": "URDF Robot Description",
        "group": "robotics",
        "summary": "Generate and validate URDF robot structures, joints, limits, inertials, geometry, and mesh references.",
        "maturity": "stable",
        "risk_level": "compute",
        "actions": [_action("generate", "Generate and validate URDF")],
        "accepts": ["text/plain", ".py", ".urdf", ".step", ".stp", ".stl", ".dae", ".obj"],
        "produces": [".py", ".urdf"],
        "dependencies": _robot_dependencies,
    },
    {
        "id": "srdf",
        "name": "SRDF Planning Semantics",
        "group": "robotics",
        "summary": "Generate and validate MoveIt SRDF planning groups, states, end effectors, and collision semantics against URDF.",
        "maturity": "stable",
        "risk_level": "compute",
        "actions": [_action("generate", "Generate and validate SRDF")],
        "accepts": ["text/plain", ".py", ".srdf", ".urdf"],
        "produces": [".py", ".srdf"],
        "dependencies": _robot_dependencies,
    },
    {
        "id": "sdf",
        "name": "SDFormat",
        "group": "robotics",
        "summary": "Generate and validate SDFormat models and worlds for Gazebo and compatible simulators.",
        "maturity": "stable",
        "risk_level": "compute",
        "actions": [_action("generate", "Generate and validate SDF"), _action("gz-check", "Run optional Gazebo SDF check", "read_only")],
        "accepts": ["text/plain", ".py", ".sdf", ".urdf", ".step", ".stp", ".stl", ".dae", ".obj"],
        "produces": [".py", ".sdf"],
        "dependencies": _robot_dependencies,
    },
)

_DISPLAY_ORDER = {
    capability_id: index
    for index, capability_id in enumerate(
        (
            "cad",
            "cad-viewer",
            "step-parts",
            "dxf",
            "urdf",
            "srdf",
            "sdf",
            "sendcutsend",
            "gcode",
            "bambu-labs",
            "implicit-cad",
        )
    )
}


def _build_manifest(spec: dict[str, Any]) -> CapabilityManifest:
    dependency_factory = spec["dependencies"]
    dependencies = [_vendored_dependency(spec["id"]), *dependency_factory()]
    blocked_reasons = [dependency.detail for dependency in dependencies if dependency.required and dependency.available is False]
    actions = [action.model_copy() for action in spec["actions"]]
    if blocked_reasons:
        reason = "; ".join(blocked_reasons)
        for action in actions:
            action.available = False
            action.blocked_reason = reason
    elif spec["id"] == "dxf":
        for action in actions:
            if action.id == "validate":
                action.available = False
                action.blocked_reason = "当前 vendored DXF 包没有独立验证器 CLI。"
    elif spec["id"] == "gcode":
        slicer = next(dependency for dependency in dependencies if dependency.id == "slicer-cli")
        if not slicer.available:
            for action in actions:
                if action.id in {"dry-run", "slice"}:
                    action.available = False
                    action.blocked_reason = slicer.detail
    has_available_action = any(action.available for action in actions)
    if not blocked_reasons and not has_available_action:
        blocked_reasons = list(dict.fromkeys(
            action.blocked_reason for action in actions if action.blocked_reason
        ))
    return CapabilityManifest(
        **{key: value for key, value in spec.items() if key not in {"actions", "dependencies"}},
        actions=actions,
        dependencies=dependencies,
        available=not blocked_reasons and has_available_action,
        blocked_reasons=blocked_reasons,
        upstream=_upstream(),
    )


def list_capabilities() -> list[CapabilityManifest]:
    """Return all manifests in stable display order with fresh availability."""
    specs = sorted(_SPECS, key=lambda spec: _DISPLAY_ORDER[spec["id"]])
    return [_build_manifest(spec) for spec in specs]


def get_capability(capability_id: str) -> CapabilityManifest | None:
    """Return one manifest by its exact, case-sensitive public identifier."""
    for spec in _SPECS:
        if spec["id"] == capability_id:
            return _build_manifest(spec)
    return None
