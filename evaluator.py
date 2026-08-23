from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from direct_algorithm import DirectAlgorithmClient
from eval_config import default_cadcoder_file, default_cadprompt_dir, default_data_root as configured_data_root, default_results_root


@dataclass(frozen=True)
class EvalSample:
    sample_id: str
    dataset: str
    prompt: str
    reference_code: str
    source_path: str
    metadata: dict[str, Any] = field(default_factory=dict)


def load_cadprompt_samples(root: str | Path, limit: int | None = None) -> list[EvalSample]:
    root_path = Path(root)
    samples: list[EvalSample] = []
    for sample_dir in sorted(p for p in root_path.iterdir() if p.is_dir()):
        prompt_path = sample_dir / "Natural_Language_Descriptions_Prompt_with_specific_measurements.txt"
        if not prompt_path.exists():
            prompt_path = sample_dir / "Natural_Language_Descriptions_Prompt.txt"
        code_path = sample_dir / "Python_Code.py"
        if not prompt_path.exists() or not code_path.exists():
            continue
        samples.append(
            EvalSample(
                sample_id=sample_dir.name,
                dataset="CADPrompt",
                prompt=prompt_path.read_text(encoding="utf-8", errors="ignore").strip(),
                reference_code=code_path.read_text(encoding="utf-8", errors="ignore"),
                source_path=str(sample_dir),
                metadata={"prompt_file": prompt_path.name, "code_file": code_path.name},
            )
        )
        if limit is not None and len(samples) >= limit:
            break
    return samples


def load_cadcoder_samples(
    json_path: str | Path,
    limit: int | None = None,
    max_prompt_chars: int = 10000,
    allow_large: bool = False,
) -> list[EvalSample]:
    path = Path(json_path)
    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb > 250 and not allow_large:
        raise ValueError(f"Refusing to load large CAD-Coder file {path} ({size_mb:.1f} MB). Pass --allow-large to override.")

    rows = json.loads(path.read_text(encoding="utf-8"))
    samples: list[EvalSample] = []
    for index, row in enumerate(rows):
        messages = row.get("messages", [])
        if len(messages) < 2:
            continue
        prompt = str(messages[0].get("content", "")).strip()
        if not prompt or len(prompt) > max_prompt_chars:
            continue
        code = _extract_fenced_code(str(messages[1].get("content", "")))
        model_path = str(row.get("model_path") or f"row_{index:06d}")
        sample_id = Path(model_path).stem
        samples.append(
            EvalSample(
                sample_id=sample_id,
                dataset="CAD-Coder",
                prompt=prompt,
                reference_code=code,
                source_path=str(path),
                metadata={"model_path": model_path, "row_index": index, "source_file": path.name},
            )
        )
        if limit is not None and len(samples) >= limit:
            break
    return samples


def _extract_fenced_code(text: str) -> str:
    match = re.search(r"```(?:python)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else text.strip()


def adapt_reference_code(code: str) -> str:
    clean = _extract_fenced_code(code)
    if _has_active_show_object(clean) or _assigns_name(clean, "result"):
        return clean
    assigned = _assigned_names(clean)
    if "r" in assigned:
        return clean.rstrip() + "\n\nresult = r\n"
    if "part" in assigned:
        return clean.rstrip() + "\n\nresult = part\n"
    return clean


def _has_active_show_object(code: str) -> bool:
    return any(line.strip().startswith("show_object(") for line in code.splitlines())


def _assigns_name(code: str, name: str) -> bool:
    return name in _assigned_names(code)


def _assigned_names(code: str) -> set[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


class BackendClient:
    def __init__(self, base_url: str, timeout_s: int = 240, api_key: str | None = None, bearer_token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.api_key = api_key
        self.bearer_token = bearer_token

    def get_json(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(self.base_url + path, headers=self._headers())
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method="POST",
            headers={**self._headers(), "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"detail": body}
            return {"success": False, "error": {"type": f"HTTP{exc.code}", "message": parsed}}

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers


def evaluate_sample(
    client: BackendClient,
    sample: EvalSample,
    mode: str,
    output_formats: list[str],
) -> dict[str, Any]:
    start = time.time()
    try:
        if mode == "reference":
            payload = {"code": adapt_reference_code(sample.reference_code), "output_formats": output_formats, "sample_id": sample.sample_id, "dataset": sample.dataset, "mode": mode}
            response = client.post_json("/api/execute", payload)
        elif mode == "generate":
            payload = {"prompt": sample.prompt, "output_formats": output_formats, "sample_id": sample.sample_id, "dataset": sample.dataset, "mode": mode}
            response = client.post_json("/api/generate", payload)
        else:
            raise ValueError(f"Unsupported mode: {mode}")
    except Exception as exc:
        response = {"success": False, "error": {"type": type(exc).__name__, "message": str(exc)}}

    error = response.get("error") or {}
    return {
        "dataset": sample.dataset,
        "sample_id": sample.sample_id,
        "mode": mode,
        "success": bool(response.get("success")),
        "request_id": response.get("request_id"),
        "files": response.get("files") or {},
        "code": response.get("code"),
        "workflow_run_id": response.get("workflow_run_id"),
        "task_status": response.get("task_status"),
        "project_id": response.get("project_id"),
        "branch_id": response.get("branch_id"),
        "expected_base_revision_id": response.get("expected_base_revision_id"),
        "revision_id": response.get("revision_id"),
        "change_set_id": response.get("change_set_id"),
        "inspect_report": response.get("inspect_report"),
        "repair_history": response.get("repair_history"),
        "validation": response.get("validation"),
        "execution_time_ms": response.get("execution_time_ms") or int((time.time() - start) * 1000),
        "wall_time_ms": int((time.time() - start) * 1000),
        "attempts": response.get("attempts"),
        "error_type": error.get("type") if isinstance(error, dict) else None,
        "error_message": error.get("message") if isinstance(error, dict) else str(error),
        "source_path": sample.source_path,
        "metadata": sample.metadata,
    }


def summarize_results(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(results)
    total = len(rows)
    successes = sum(1 for row in rows if row.get("success"))
    error_types = Counter(
        str(row.get("error_type") or "UnknownError")
        for row in rows
        if not row.get("success")
    )
    times = [int(row.get("execution_time_ms") or 0) for row in rows]
    return {
        "total": total,
        "success": successes,
        "failed": total - successes,
        "success_rate": successes / total if total else 0.0,
        "avg_execution_time_ms": sum(times) / total if total else 0,
        "error_types": dict(sorted(error_types.items())),
    }


def result_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("dataset")), str(row.get("sample_id")), str(row.get("mode")))


def sample_key(sample: EvalSample, mode: str) -> tuple[str, str, str]:
    return (sample.dataset, sample.sample_id, mode)


def append_result(output_dir: str | Path, row: dict[str, Any]) -> None:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    with (output_path / "results.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()


def read_results(output_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(output_dir) / "results.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def completed_keys(output_dir: str | Path) -> set[tuple[str, str, str]]:
    return {result_key(row) for row in read_results(output_dir)}


def write_outputs(results: list[dict[str, Any]], output_dir: str | Path, samples: list[EvalSample] | None = None) -> dict[str, Any]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    summary = summarize_results(results)

    with (output_path / "results.jsonl").open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    (output_path / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_failures_csv(output_path / "failures.csv", results)
    _write_report(output_path / "report.md", summary, results, samples or [])
    return summary


def _write_failures_csv(path: Path, results: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["dataset", "sample_id", "mode", "error_type", "error_message", "source_path"])
        writer.writeheader()
        for row in results:
            if row.get("success"):
                continue
            writer.writerow({key: row.get(key, "") for key in writer.fieldnames})


def _write_report(path: Path, summary: dict[str, Any], results: list[dict[str, Any]], samples: list[EvalSample]) -> None:
    by_dataset = Counter(row.get("dataset") for row in results)
    lines = [
        "# CAD Algorithm Evaluation Report",
        "",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Samples selected: {len(samples)}",
        f"Results total: {summary['total']}",
        f"Success: {summary['success']}",
        f"Failed: {summary['failed']}",
        f"Success rate: {summary['success_rate']:.2%}",
        f"Average execution time: {summary['avg_execution_time_ms']:.1f} ms",
        "",
        "## Dataset Counts",
    ]
    for name, count in sorted(by_dataset.items()):
        lines.append(f"- {name}: {count}")
    lines.extend(["", "## Error Types"])
    for name, count in summary["error_types"].items():
        lines.append(f"- {name}: {count}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_samples(args: argparse.Namespace) -> list[EvalSample]:
    samples: list[EvalSample] = []
    if args.dataset in {"cadprompt", "both"}:
        samples.extend(load_cadprompt_samples(args.cadprompt_dir, args.limit))
    if args.dataset in {"cadcoder", "both"}:
        samples.extend(
            load_cadcoder_samples(
                args.cadcoder_file,
                args.limit,
                max_prompt_chars=args.max_prompt_chars,
                allow_large=args.allow_large,
            )
        )
    return samples


def default_data_root() -> Path:
    return configured_data_root()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    data_root = default_data_root()
    parser = argparse.ArgumentParser(description="Evaluate CAD Agent algorithms on CADPrompt and CAD-Coder.")
    parser.add_argument("--dataset", choices=["cadprompt", "cadcoder", "both"], default="both")
    parser.add_argument("--mode", choices=["reference", "generate"], default="reference")
    parser.add_argument("--limit", type=int, default=20, help="Sample limit per selected dataset.")
    parser.add_argument("--cadprompt-dir", type=Path, default=default_cadprompt_dir())
    parser.add_argument("--cadcoder-file", type=Path, default=default_cadcoder_file())
    parser.add_argument("--runner", choices=["direct", "http"], default="direct", help="direct uses current CAD-Agent durable workflow in-process; http calls the REST API.")
    parser.add_argument("--backend-url", default="http://localhost:8000")
    parser.add_argument("--output-dir", type=Path, default=default_results_root() / "latest")
    parser.add_argument("--output-formats", nargs="+", default=["step", "stl"])
    parser.add_argument("--timeout-s", type=int, default=240)
    parser.add_argument("--max-prompt-chars", type=int, default=10000)
    parser.add_argument("--api-key")
    parser.add_argument("--bearer-token")
    parser.add_argument("--allow-large", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Only load samples and write selected_samples.json.")
    parser.add_argument("--no-resume", action="store_true", help="Do not skip rows already present in results.jsonl.")
    parser.add_argument("--max-pending", type=int, help="Process at most this many pending samples, useful for visible batched progress.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    samples = load_samples(args)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        selected = [asdict(sample) for sample in samples]
        (args.output_dir / "selected_samples.json").write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {"selected_samples": len(samples), "datasets": dict(Counter(sample.dataset for sample in samples))}
        (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    client = (
        DirectAlgorithmClient(timeout_s=args.timeout_s)
        if args.runner == "direct"
        else BackendClient(args.backend_url, timeout_s=args.timeout_s, api_key=args.api_key, bearer_token=args.bearer_token)
    )
    results = [] if args.no_resume else read_results(args.output_dir)
    done = set() if args.no_resume else completed_keys(args.output_dir)
    pending = [sample for sample in samples if sample_key(sample, args.mode) not in done]
    if args.max_pending is not None:
        pending = pending[: args.max_pending]
    if args.no_resume and (args.output_dir / "results.jsonl").exists():
        (args.output_dir / "results.jsonl").unlink()

    print(f"Selected {len(samples)} samples; pending {len(pending)}; already completed {len(done)}.", flush=True)
    for index, sample in enumerate(pending, start=1):
        label = f"[{index}/{len(pending)}] {sample.dataset}/{sample.sample_id} {args.mode}"
        print(f"START {label}", flush=True)
        row = evaluate_sample(client, sample, args.mode, args.output_formats)
        append_result(args.output_dir, row)
        results.append(row)
        status = "OK" if row.get("success") else f"FAIL {row.get('error_type') or 'UnknownError'}"
        print(f"DONE  {label} -> {status} in {row.get('wall_time_ms')} ms", flush=True)

    all_results = read_results(args.output_dir)
    summary = write_outputs(all_results, args.output_dir, samples)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
