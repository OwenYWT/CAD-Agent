"""Compare tool-call consistency between two live evaluation reports."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.evaluation.consistency import compare_evaluation_reports


def _load_report(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"report must contain a JSON object: {path}")
    return value


def _print_summary(comparison: Mapping[str, Any]) -> None:
    differences = comparison["differences"]
    base = comparison["base"]
    candidate = comparison["candidate"]
    print(f"Sequence match: {comparison['sequence_match']}")
    print(f"Structure match: {comparison['structure_match']}")
    print(
        "Calls: "
        f"{base['total_call_count']} -> {candidate['total_call_count']}; "
        f"retries: {base['retry_count']} -> {candidate['retry_count']}"
    )
    print(f"Added: {len(differences['added'])}")
    print(f"Removed: {len(differences['removed'])}")
    print(f"Reordered: {differences['reordered']}")
    print(f"Status changes: {len(differences['status_changes'])}")
    print(f"Input hash changes: {len(differences['input_hash_changes'])}")
    print(f"Output hash changes: {len(differences['output_hash_changes'])}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare tool-call consistency between two live reports."
    )
    parser.add_argument("base", type=Path, help="base live report JSON")
    parser.add_argument("candidate", type=Path, help="candidate live report JSON")
    parser.add_argument("--json", action="store_true", help="print JSON output")
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="return exit code 1 when normalized sequences differ",
    )
    args = parser.parse_args(argv)

    comparison = compare_evaluation_reports(
        _load_report(args.base),
        _load_report(args.candidate),
    )
    if args.json:
        print(json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_summary(comparison)
    return int(args.fail_on_drift and not comparison["sequence_match"])


if __name__ == "__main__":
    raise SystemExit(main())
