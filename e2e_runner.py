from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

from evaluator import BackendClient, EvalSample, evaluate_sample, summarize_results
from quality_eval import (
    compare_mesh_paths,
    evaluate_quality_rows,
    file_url_to_local_path,
    summarize_quality,
    write_quality_outputs,
)


def select_samples(samples: Sequence[EvalSample], sample_ids: Sequence[str] | None = None) -> list[EvalSample]:
    if not sample_ids:
        return list(samples)
    wanted = {str(sample_id) for sample_id in sample_ids}
    return [sample for sample in samples if sample.sample_id in wanted]


def summarize_named_results(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return summarize_results(rows)


def write_named_json_outputs(output_dir: str | Path, prefix: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    summary = summarize_named_results(rows)

    with (output_path / f"{prefix}_results.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    (output_path / f"{prefix}_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_named_failures(output_path / f"{prefix}_failures.csv", rows)
    return summary


def _write_named_failures(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["dataset", "sample_id", "mode", "error_type", "error_message", "source_path"])
        writer.writeheader()
        for row in rows:
            if row.get("success"):
                continue
            writer.writerow({key: row.get(key, "") for key in writer.fieldnames})


def run_generation_pass(
    client: BackendClient,
    samples: Sequence[EvalSample],
    mode: str,
    output_formats: list[str],
    label: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total = len(samples)
    for index, sample in enumerate(samples, start=1):
        print(f"START {label} [{index}/{total}] {sample.dataset}/{sample.sample_id}", flush=True)
        row = evaluate_sample(client, sample, mode=mode, output_formats=output_formats)
        rows.append(row)
        status = "OK" if row.get("success") else f"FAIL {row.get('error_type') or 'UnknownError'}"
        print(f"DONE  {label} [{index}/{total}] {sample.dataset}/{sample.sample_id} -> {status} in {row.get('wall_time_ms')} ms", flush=True)
    return rows


def cadprompt_quality_rows(generation_rows: list[dict[str, Any]], storage_root: str | Path, sample_count: int) -> list[dict[str, Any]]:
    return evaluate_quality_rows(generation_rows, storage_root=storage_root, sample_count=sample_count)


def cadcoder_quality_rows(
    reference_rows: list[dict[str, Any]],
    generation_rows: list[dict[str, Any]],
    storage_root: str | Path,
    sample_count: int,
) -> list[dict[str, Any]]:
    reference_by_id = {row.get("sample_id"): row for row in reference_rows}
    quality_rows: list[dict[str, Any]] = []
    for generated in generation_rows:
        base = {
            "dataset": generated.get("dataset"),
            "sample_id": generated.get("sample_id"),
            "mode": generated.get("mode"),
            "request_id": generated.get("request_id"),
            "source_path": generated.get("source_path"),
        }
        reference = reference_by_id.get(generated.get("sample_id"))
        if not reference:
            quality_rows.append({**base, "success": False, "error_type": "MissingReference", "error_message": "No reference result for sample"})
            continue
        if not reference.get("success"):
            quality_rows.append({**base, "success": False, "error_type": "ReferenceFailed", "error_message": reference.get("error_message")})
            continue
        if not generated.get("success"):
            quality_rows.append({**base, "success": False, "error_type": "GenerationFailed", "error_message": generated.get("error_message")})
            continue
        try:
            reference_url = (reference.get("files") or {}).get("stl")
            generated_url = (generated.get("files") or {}).get("stl")
            if not reference_url:
                raise FileNotFoundError("Reference result has no STL file URL")
            if not generated_url:
                raise FileNotFoundError("Generated result has no STL file URL")
            reference_path = file_url_to_local_path(reference_url, storage_root=storage_root)
            generated_path = file_url_to_local_path(generated_url, storage_root=storage_root)
            if not reference_path.exists():
                raise FileNotFoundError(f"Reference STL not found: {reference_path}")
            if not generated_path.exists():
                raise FileNotFoundError(f"Generated STL not found: {generated_path}")
            metrics = compare_mesh_paths(reference_path, generated_path, sample_count=sample_count)
            quality_rows.append({**base, "success": True, "reference_stl": str(reference_path), "generated_stl": str(generated_path), **metrics})
        except Exception as exc:
            quality_rows.append({**base, "success": False, "error_type": type(exc).__name__, "error_message": str(exc)})
    return quality_rows


def write_end_to_end_report(
    output_dir: str | Path,
    dataset: str,
    selected_count: int,
    generation_summary: dict[str, Any],
    quality_summary: dict[str, Any],
    reference_summary: dict[str, Any] | None = None,
) -> None:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {dataset} End-to-End Evaluation Report",
        "",
        f"Selected samples: {selected_count}",
        "",
    ]
    if reference_summary is not None:
        lines.extend([
            "## Reference",
            f"- Total: {reference_summary.get('total', 0)}",
            f"- Success: {reference_summary.get('success', 0)}",
            f"- Failed: {reference_summary.get('failed', 0)}",
            f"- Success rate: {_pct(reference_summary.get('success_rate'))}",
            "",
        ])
    lines.extend([
        "## Generation",
        f"- Total: {generation_summary.get('total', 0)}",
        f"- Success: {generation_summary.get('success', 0)}",
        f"- Failed: {generation_summary.get('failed', 0)}",
        f"- Success rate: {_pct(generation_summary.get('success_rate'))}",
        "",
        "## Quality",
        f"- Comparable: {quality_summary.get('quality_success', 0)}",
        f"- Failed comparisons: {quality_summary.get('quality_failed', 0)}",
        f"- Average quality score: {_num(quality_summary.get('avg_quality_score'))}",
        f"- Average shape score: {_num(quality_summary.get('avg_shape_score'))}",
        f"- Average dimension score: {_num(quality_summary.get('avg_dimension_score'))}",
        f"- Average normalized Chamfer: {_num(quality_summary.get('avg_normalized_chamfer'))}",
        "",
        "## Error Types",
    ])
    errors = Counter()
    errors.update(generation_summary.get("error_types") or {})
    errors.update(quality_summary.get("error_types") or {})
    if reference_summary is not None:
        errors.update(reference_summary.get("error_types") or {})
    if errors:
        for name, count in sorted(errors.items()):
            lines.append(f"- {name}: {count}")
    else:
        lines.append("- None")
    (output_path / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.2%}"


def _num(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.4f}"


def write_selected_samples(output_dir: str | Path, samples: Sequence[EvalSample]) -> None:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "sample_id": sample.sample_id,
            "dataset": sample.dataset,
            "source_path": sample.source_path,
            "metadata": sample.metadata,
            "prompt_chars": len(sample.prompt),
            "reference_code_chars": len(sample.reference_code),
        }
        for sample in samples
    ]
    (output_path / "selected_samples.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def write_quality_and_report(
    output_dir: str | Path,
    dataset: str,
    selected_count: int,
    generation_summary: dict[str, Any],
    quality_rows: list[dict[str, Any]],
    reference_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality_summary = write_quality_outputs(quality_rows, output_dir)
    write_end_to_end_report(output_dir, dataset, selected_count, generation_summary, quality_summary, reference_summary=reference_summary)
    return quality_summary
