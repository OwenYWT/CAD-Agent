from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import trimesh

from eval_config import default_storage_root


QUALITY_VERSION = "mesh-v1"


def score_from_relative_error(error: float, tolerance: float) -> float:
    if not math.isfinite(error):
        return 0.0
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")
    return max(0.0, min(1.0, 1.0 - (error / tolerance)))


def file_url_to_local_path(file_url: str, storage_root: str | Path | None = None) -> Path:
    prefix = "/api/files/"
    if not file_url.startswith(prefix):
        raise ValueError(f"Unsupported file URL: {file_url}")
    relative = file_url[len(prefix) :].replace("/", "\\")
    return Path(storage_root or default_storage_root()) / relative


def load_mesh(path: str | Path) -> trimesh.Trimesh:
    mesh = trimesh.load_mesh(str(path), force="mesh")
    if isinstance(mesh, trimesh.Scene):
        mesh = mesh.dump(concatenate=True)
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError(f"Could not load mesh from {path}")
    return mesh


def compare_mesh_paths(reference_path: str | Path, generated_path: str | Path, sample_count: int = 2048) -> dict[str, Any]:
    return compare_meshes(load_mesh(reference_path), load_mesh(generated_path), sample_count=sample_count)


def compare_meshes(reference: trimesh.Trimesh, generated: trimesh.Trimesh, sample_count: int = 2048) -> dict[str, Any]:
    reference_bbox = np.asarray(reference.extents, dtype=float)
    generated_bbox = np.asarray(generated.extents, dtype=float)
    bbox_relative_error = _relative_l2_error(reference_bbox, generated_bbox)

    reference_volume = float(abs(reference.volume)) if reference.volume is not None else 0.0
    generated_volume = float(abs(generated.volume)) if generated.volume is not None else 0.0
    volume_relative_error = _relative_scalar_error(reference_volume, generated_volume)
    bbox_ratio_relative_error = _relative_l2_error(_normalized_extents(reference_bbox), _normalized_extents(generated_bbox))
    reference_fill_ratio = _fill_ratio(reference_volume, reference_bbox)
    generated_fill_ratio = _fill_ratio(generated_volume, generated_bbox)
    fill_ratio_relative_error = _relative_scalar_error(reference_fill_ratio, generated_fill_ratio)

    reference_area = float(reference.area) if reference.area is not None else 0.0
    generated_area = float(generated.area) if generated.area is not None else 0.0
    area_relative_error = _relative_scalar_error(reference_area, generated_area)

    chamfer = normalized_chamfer_distance(reference, generated, sample_count=sample_count)

    validity_score = 1.0 if bool(generated.is_watertight) and generated_volume > 0 and len(generated.faces) > 0 else 0.0
    bbox_score = score_from_relative_error(bbox_relative_error, tolerance=0.35)
    volume_score = score_from_relative_error(volume_relative_error, tolerance=0.60)
    chamfer_score = score_from_relative_error(chamfer, tolerance=0.15)
    area_score = score_from_relative_error(area_relative_error, tolerance=0.60)
    bbox_ratio_score = score_from_relative_error(bbox_ratio_relative_error, tolerance=0.20)
    fill_ratio_score = score_from_relative_error(fill_ratio_relative_error, tolerance=0.50)
    dimension_score = 0.55 * bbox_score + 0.35 * volume_score + 0.10 * area_score
    shape_score = 0.35 * validity_score + 0.30 * chamfer_score + 0.25 * bbox_ratio_score + 0.10 * fill_ratio_score
    quality_score = 0.55 * shape_score + 0.45 * dimension_score

    return {
        "quality_version": QUALITY_VERSION,
        "reference_watertight": bool(reference.is_watertight),
        "generated_watertight": bool(generated.is_watertight),
        "reference_faces": int(len(reference.faces)),
        "generated_faces": int(len(generated.faces)),
        "reference_volume": reference_volume,
        "generated_volume": generated_volume,
        "reference_area": reference_area,
        "generated_area": generated_area,
        "reference_bbox": reference_bbox.tolist(),
        "generated_bbox": generated_bbox.tolist(),
        "bbox_relative_error": float(bbox_relative_error),
        "volume_relative_error": float(volume_relative_error),
        "area_relative_error": float(area_relative_error),
        "bbox_ratio_relative_error": float(bbox_ratio_relative_error),
        "fill_ratio_relative_error": float(fill_ratio_relative_error),
        "reference_fill_ratio": float(reference_fill_ratio),
        "generated_fill_ratio": float(generated_fill_ratio),
        "normalized_chamfer": float(chamfer),
        "validity_score": float(validity_score),
        "bbox_score": float(bbox_score),
        "volume_score": float(volume_score),
        "chamfer_score": float(chamfer_score),
        "area_score": float(area_score),
        "bbox_ratio_score": float(bbox_ratio_score),
        "fill_ratio_score": float(fill_ratio_score),
        "dimension_score": float(dimension_score),
        "shape_score": float(shape_score),
        "quality_score": float(quality_score),
    }


def normalized_chamfer_distance(reference: trimesh.Trimesh, generated: trimesh.Trimesh, sample_count: int = 2048) -> float:
    reference_points = _normalized_surface_points(reference, sample_count, seed=12345)
    generated_points = _normalized_surface_points(generated, sample_count, seed=12345)
    if len(reference_points) == 0 or len(generated_points) == 0:
        return float("inf")
    ref_to_gen = _nearest_distances(reference_points, generated_points)
    gen_to_ref = _nearest_distances(generated_points, reference_points)
    return float(np.mean(ref_to_gen) + np.mean(gen_to_ref)) / 2.0


def _normalized_surface_points(mesh: trimesh.Trimesh, sample_count: int, seed: int) -> np.ndarray:
    state = np.random.get_state()
    try:
        np.random.seed(seed)
        points, _ = trimesh.sample.sample_surface(mesh, sample_count)
    finally:
        np.random.set_state(state)
    centroid = points.mean(axis=0)
    points = points - centroid
    scale = float(np.linalg.norm(np.asarray(mesh.extents, dtype=float)))
    if scale <= 0 or not math.isfinite(scale):
        return points
    return points / scale


def _nearest_distances(source: np.ndarray, target: np.ndarray, chunk_size: int = 512) -> np.ndarray:
    distances = []
    for start in range(0, len(source), chunk_size):
        chunk = source[start : start + chunk_size]
        diff = chunk[:, None, :] - target[None, :, :]
        distances.append(np.sqrt(np.sum(diff * diff, axis=2)).min(axis=1))
    return np.concatenate(distances) if distances else np.array([], dtype=float)


def _normalized_extents(extents: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(extents))
    if norm <= 1e-12 or not math.isfinite(norm):
        return extents
    return extents / norm


def _fill_ratio(volume: float, extents: np.ndarray) -> float:
    bbox_volume = float(np.prod(extents))
    if bbox_volume <= 1e-12 or not math.isfinite(bbox_volume):
        return 0.0
    return float(volume / bbox_volume)


def _relative_l2_error(reference: np.ndarray, generated: np.ndarray) -> float:
    denom = float(np.linalg.norm(reference))
    if denom <= 1e-12:
        return 0.0 if float(np.linalg.norm(generated)) <= 1e-12 else float("inf")
    return float(np.linalg.norm(reference - generated) / denom)


def _relative_scalar_error(reference: float, generated: float) -> float:
    denom = abs(reference)
    if denom <= 1e-12:
        return 0.0 if abs(generated) <= 1e-12 else float("inf")
    return abs(generated - reference) / denom


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def evaluate_quality_rows(
    generation_results: Iterable[dict[str, Any]],
    storage_root: str | Path,
    sample_count: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in generation_results:
        base = {
            "dataset": result.get("dataset"),
            "sample_id": result.get("sample_id"),
            "mode": result.get("mode"),
            "request_id": result.get("request_id"),
            "source_path": result.get("source_path"),
        }
        if not result.get("success"):
            rows.append({**base, "success": False, "error_type": "GenerationFailed", "error_message": result.get("error_message")})
            continue
        try:
            reference_path = reference_mesh_path(result)
            generated_url = (result.get("files") or {}).get("stl")
            if not generated_url:
                raise FileNotFoundError("Generated result has no STL file URL")
            generated_path = file_url_to_local_path(generated_url, storage_root=storage_root)
            if not generated_path.exists():
                raise FileNotFoundError(f"Generated STL not found: {generated_path}")
            if not reference_path.exists():
                raise FileNotFoundError(f"Reference STL not found: {reference_path}")
            metrics = compare_mesh_paths(reference_path, generated_path, sample_count=sample_count)
            rows.append({**base, "success": True, "reference_stl": str(reference_path), "generated_stl": str(generated_path), **metrics})
        except Exception as exc:
            rows.append({**base, "success": False, "error_type": type(exc).__name__, "error_message": str(exc)})
    return rows


def reference_mesh_path(result: dict[str, Any]) -> Path:
    dataset = result.get("dataset")
    source_path = Path(str(result.get("source_path") or ""))
    if dataset == "CADPrompt":
        return source_path / "Ground_Truth.stl"
    if dataset == "CAD-Coder":
        raise ValueError("CAD-Coder quality evaluation requires reference STL results; use CADPrompt first.")
    raise ValueError(f"Unsupported dataset for quality evaluation: {dataset}")


def summarize_quality(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(rows)
    scored = [row for row in items if row.get("success") and row.get("quality_score") is not None]
    by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in items:
        by_dataset[str(row.get("dataset"))].append(row)
    return {
        "total": len(items),
        "quality_success": len(scored),
        "quality_failed": len(items) - len(scored),
        "avg_quality_score": _avg(row["quality_score"] for row in scored),
        "avg_shape_score": _avg(row.get("shape_score") for row in scored if row.get("shape_score") is not None),
        "avg_dimension_score": _avg(row.get("dimension_score") for row in scored if row.get("dimension_score") is not None),
        "avg_bbox_relative_error": _avg(row.get("bbox_relative_error") for row in scored if row.get("bbox_relative_error") is not None),
        "avg_volume_relative_error": _avg(row.get("volume_relative_error") for row in scored if row.get("volume_relative_error") is not None),
        "avg_normalized_chamfer": _avg(row.get("normalized_chamfer") for row in scored if row.get("normalized_chamfer") is not None),
        "error_types": dict(Counter(str(row.get("error_type") or "UnknownError") for row in items if not row.get("success"))),
        "datasets": {
            name: {
                "total": len(dataset_rows),
                "quality_success": sum(1 for row in dataset_rows if row.get("success")),
                "avg_quality_score": _avg(row["quality_score"] for row in dataset_rows if row.get("success") and row.get("quality_score") is not None),
            }
            for name, dataset_rows in sorted(by_dataset.items())
        },
    }


def _avg(values: Iterable[float]) -> float | None:
    numbers = [float(value) for value in values]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def write_quality_outputs(rows: list[dict[str, Any]], output_dir: str | Path) -> dict[str, Any]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    summary = summarize_quality(rows)
    with (output_path / "quality_results.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (output_path / "quality_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_quality_failures(output_path / "quality_failures.csv", rows)
    _write_quality_report(output_path / "quality_report.md", summary)
    return summary


def _write_quality_failures(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["dataset", "sample_id", "mode", "error_type", "error_message", "source_path"])
        writer.writeheader()
        for row in rows:
            if row.get("success"):
                continue
            writer.writerow({key: row.get(key, "") for key in writer.fieldnames})


def _write_quality_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# CAD Quality Evaluation Report",
        "",
        f"Total: {summary['total']}",
        f"Quality success: {summary['quality_success']}",
        f"Quality failed: {summary['quality_failed']}",
        f"Average quality score: {_fmt(summary['avg_quality_score'])}",
        f"Average shape score: {_fmt(summary['avg_shape_score'])}",
        f"Average dimension score: {_fmt(summary['avg_dimension_score'])}",
        f"Average bbox relative error: {_fmt(summary['avg_bbox_relative_error'])}",
        f"Average volume relative error: {_fmt(summary['avg_volume_relative_error'])}",
        f"Average normalized chamfer: {_fmt(summary['avg_normalized_chamfer'])}",
        "",
        "## Datasets",
    ]
    for name, item in summary["datasets"].items():
        lines.append(f"- {name}: {item['quality_success']}/{item['total']}, avg score {_fmt(item['avg_quality_score'])}")
    lines.extend(["", "## Error Types"])
    for name, count in summary["error_types"].items():
        lines.append(f"- {name}: {count}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare generated STL files against reference STL files.")
    parser.add_argument("--results-jsonl", type=Path, required=True, help="Generation results.jsonl from evaluator.py.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--storage-root", type=Path, default=default_storage_root())
    parser.add_argument("--sample-count", type=int, default=2048)
    parser.add_argument("--dataset", choices=["cadprompt"], default="cadprompt")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = read_jsonl(args.results_jsonl)
    if args.dataset == "cadprompt":
        rows = [row for row in rows if row.get("dataset") == "CADPrompt"]
    quality_rows = evaluate_quality_rows(rows, storage_root=args.storage_root, sample_count=args.sample_count)
    summary = write_quality_outputs(quality_rows, args.output_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["quality_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
