from __future__ import annotations

import argparse
import json
from pathlib import Path

from e2e_runner import (
    cadprompt_quality_rows,
    run_generation_pass,
    select_samples,
    write_named_json_outputs,
    write_quality_and_report,
    write_selected_samples,
)
from direct_algorithm import DirectAlgorithmClient
from evaluator import BackendClient, load_cadprompt_samples
from eval_config import default_cadprompt_dir, default_storage_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="End-to-end CADPrompt evaluation: prompt generation + STL quality against Ground_Truth.stl.")
    parser.add_argument("--cadprompt-dir", type=Path, default=default_cadprompt_dir())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20, help="Maximum samples to load before optional --sample-ids filtering.")
    parser.add_argument("--sample-ids", nargs="*", help="Specific CADPrompt sample IDs, e.g. 00002221 00004935.")
    parser.add_argument("--runner", choices=["direct", "http"], default="direct", help="direct bypasses FastAPI auth and calls backend algorithm modules in-process.")
    parser.add_argument("--backend-url", default="http://localhost:8000")
    parser.add_argument("--bearer-token")
    parser.add_argument("--api-key")
    parser.add_argument("--timeout-s", type=int, default=240)
    parser.add_argument("--output-formats", nargs="+", default=["step", "stl"])
    parser.add_argument("--storage-root", type=Path, default=default_storage_root())
    parser.add_argument("--quality-sample-count", type=int, default=2048)
    parser.add_argument("--dry-run", action="store_true", help="Only write selected_samples.json; do not call backend.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.cadprompt_dir = args.cadprompt_dir.resolve()
    samples = select_samples(load_cadprompt_samples(args.cadprompt_dir, limit=args.limit), args.sample_ids)
    args.output_dir = args.output_dir.resolve()
    args.storage_root = args.storage_root.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_selected_samples(args.output_dir, samples)

    if args.dry_run:
        summary = {"dataset": "CADPrompt", "selected_samples": len(samples)}
        (args.output_dir / "dry_run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.runner == "direct":
        client = DirectAlgorithmClient(timeout_s=args.timeout_s)
    else:
        client = BackendClient(args.backend_url, timeout_s=args.timeout_s, api_key=args.api_key, bearer_token=args.bearer_token)
    generation_rows = run_generation_pass(client, samples, mode="generate", output_formats=args.output_formats, label="generation")
    generation_summary = write_named_json_outputs(args.output_dir, "generation", generation_rows)
    quality_rows = cadprompt_quality_rows(generation_rows, storage_root=args.storage_root, sample_count=args.quality_sample_count)
    quality_summary = write_quality_and_report(args.output_dir, "CADPrompt", len(samples), generation_summary, quality_rows)

    final_summary = {"dataset": "CADPrompt", "selected_samples": len(samples), "generation": generation_summary, "quality": quality_summary}
    (args.output_dir / "end_to_end_summary.json").write_text(json.dumps(final_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(final_summary, ensure_ascii=False, indent=2))
    return 1 if generation_summary["failed"] or quality_summary["quality_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
