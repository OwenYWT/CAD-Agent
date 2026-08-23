from __future__ import annotations

import argparse
import json
from pathlib import Path

from e2e_runner import (
    cadcoder_quality_rows,
    run_generation_pass,
    select_samples,
    write_named_json_outputs,
    write_quality_and_report,
    write_selected_samples,
)
from direct_algorithm import DirectAlgorithmClient
from evaluator import BackendClient, load_cadcoder_samples
from eval_config import default_cadcoder_file, default_storage_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="End-to-end CAD-Coder evaluation: reference code baseline + prompt generation + STL quality comparison.")
    parser.add_argument("--cadcoder-file", type=Path, default=default_cadcoder_file())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20, help="Maximum samples to load before optional --sample-ids filtering.")
    parser.add_argument("--sample-ids", nargs="*", help="Specific CAD-Coder sample IDs, e.g. 00357061 00352432.")
    parser.add_argument("--runner", choices=["direct", "http"], default="direct", help="direct bypasses FastAPI auth and calls backend algorithm modules in-process.")
    parser.add_argument("--backend-url", default="http://localhost:8000")
    parser.add_argument("--bearer-token")
    parser.add_argument("--api-key")
    parser.add_argument("--timeout-s", type=int, default=240)
    parser.add_argument("--max-prompt-chars", type=int, default=10000)
    parser.add_argument("--allow-large", action="store_true")
    parser.add_argument("--output-formats", nargs="+", default=["step", "stl"])
    parser.add_argument("--storage-root", type=Path, default=default_storage_root())
    parser.add_argument("--quality-sample-count", type=int, default=2048)
    parser.add_argument("--skip-reference", action="store_true", help="Skip reference execution; quality comparisons will fail without reference STL files.")
    parser.add_argument("--dry-run", action="store_true", help="Only write selected_samples.json; do not call backend.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.cadcoder_file = args.cadcoder_file.resolve()
    samples = select_samples(
        load_cadcoder_samples(
            args.cadcoder_file,
            limit=args.limit,
            max_prompt_chars=args.max_prompt_chars,
            allow_large=args.allow_large,
        ),
        args.sample_ids,
    )
    args.output_dir = args.output_dir.resolve()
    args.storage_root = args.storage_root.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_selected_samples(args.output_dir, samples)

    if args.dry_run:
        summary = {"dataset": "CAD-Coder", "selected_samples": len(samples), "source_file": str(args.cadcoder_file)}
        (args.output_dir / "dry_run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.runner == "direct":
        client = DirectAlgorithmClient(timeout_s=args.timeout_s)
    else:
        client = BackendClient(args.backend_url, timeout_s=args.timeout_s, api_key=args.api_key, bearer_token=args.bearer_token)
    reference_rows = []
    reference_summary = None
    if not args.skip_reference:
        reference_rows = run_generation_pass(client, samples, mode="reference", output_formats=args.output_formats, label="reference")
        reference_summary = write_named_json_outputs(args.output_dir, "reference", reference_rows)

    generation_rows = run_generation_pass(client, samples, mode="generate", output_formats=args.output_formats, label="generation")
    generation_summary = write_named_json_outputs(args.output_dir, "generation", generation_rows)
    quality_rows = cadcoder_quality_rows(reference_rows, generation_rows, storage_root=args.storage_root, sample_count=args.quality_sample_count)
    quality_summary = write_quality_and_report(args.output_dir, "CAD-Coder", len(samples), generation_summary, quality_rows, reference_summary=reference_summary)

    final_summary = {
        "dataset": "CAD-Coder",
        "selected_samples": len(samples),
        "reference": reference_summary,
        "generation": generation_summary,
        "quality": quality_summary,
    }
    (args.output_dir / "end_to_end_summary.json").write_text(json.dumps(final_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(final_summary, ensure_ascii=False, indent=2))
    failed = generation_summary["failed"] or quality_summary["quality_failed"] or (reference_summary and reference_summary["failed"])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
