"""Verify backend and sandbox image bytes against the reviewed source snapshot."""
import argparse
import json
from pathlib import Path
import subprocess


def check_image(image, expected):
    code = """
import hashlib,json,sys
from pathlib import Path
result={}
for name in json.load(sys.stdin):
    p=Path(name)
    result[name]=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
print(json.dumps(result))
"""
    result = subprocess.run(
        ["docker", "run", "--rm", "-i", "--network", "none", "--read-only",
         "--entrypoint", "python", image, "-c", code],
        input=json.dumps(list(expected)), text=True, capture_output=True, check=True,
        timeout=120,
    )
    actual = json.loads(result.stdout)
    mismatches = [name for name, value in expected.items() if actual.get(name) != value]
    if mismatches:
        raise RuntimeError(f"Image content mismatch: {mismatches}")
    return {"image": image, "verified_files": len(expected), "mismatches": []}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--backend-image", required=True)
    parser.add_argument("--sandbox-image", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    sources = {f["path"]: f["sha256"] for f in json.loads(args.manifest.read_text())["files"]}
    backend = {}
    for path, digest in sources.items():
        if path.startswith(("backend/app/", "backend/examples/", "backend/alembic/")):
            backend["/app/" + path] = digest
        elif path.startswith("third_party/cadskills/"):
            backend["/app/" + path] = digest
    sandbox = {}
    names = (
        "executor_entry.py", "capability_entry.py", "freecad_entry.py", "freecad_api.py", "freecad_catalog.py",
        "freecad_edge_scope.py", "freecad_bom.py", "freecad_constraint_validation.py",
        "freecad_scene.py", "freecad_engineering.py", "freecad_cam.py", "freecad_release.py", "freecad_result_channel.py",
        "geometry_validation.py", "feature_verification.py", "mesh_normalization.py", "visual_render.py", "dfm_brep.py", "dfm_validation.py",
        "runtime_probe.py", "freecad_runtime_probe.py", "runtime-lock.json",
        "runtime-requirements.txt", "patch_build123d.py",
    )
    for name in names:
        sandbox["/opt/cad-agent/" + name] = sources["backend/sandbox/" + name]
    for source, target in (("state_projector.py", "freecad_state_projector.py"),
                           ("sketch_diagnostics.py", "freecad_sketch_diagnostics.py"),
                           ("failure_snapshot.py", "freecad_failure_snapshot.py"),
                           ("constraint_relationships.py", "freecad_constraint_relationships.py"),
                           ("sketch_relations.py", "freecad_sketch_relations.py"),
                           ("reference_geometry.py", "freecad_reference_geometry.py"),
                           ("topology.py", "freecad_topology.py"),
                           ("capabilities.json", "freecad-capabilities.json")):
        sandbox["/opt/cad-agent/" + target] = sources["backend/app/freecad/" + source]
    sandbox["/opt/cad-agent/geometry_request.py"] = sources["backend/app/contracts/geometry_request.py"]
    for path, digest in sources.items():
        if path.startswith("third_party/cadskills/skills/"):
            sandbox[path.replace("third_party/cadskills/", "/opt/cadskills/", 1)] = digest
    report = {
        "backend": check_image(args.backend_image, backend),
        "sandbox": check_image(args.sandbox_image, sandbox),
    }
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
