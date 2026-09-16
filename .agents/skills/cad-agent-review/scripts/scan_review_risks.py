#!/usr/bin/env python3
"""Read-only scan of changed CAD-Agent text for review leads."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[4]
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".yaml", ".yml",
    ".toml", ".ini", ".css", ".html", ".sql", ".sh", ".bat", ".ps1",
}
MOJIBAKE = (
    "\ufffd",
    "锟斤拷",
    "Ã©",
    "Ã¨",
    "Ã¤",
    "Â ",
    "â€™",
    "â€œ",
    "â€",
    "æ–‡",
)
PATTERNS = (
    ("high", "unimplemented", re.compile(r"NotImplementedError|TODO\b|FIXME\b|HACK\b|XXX\b")),
    ("high", "empty-production-branch", re.compile(r"except\s+Exception[^:]*:\s*(?:#.*)?$|^\s*pass\s*(?:#.*)?$")),
    ("medium", "placeholder-language", re.compile(r"\b(?:placeholder|dummy|fake|stub|sample-only)\b", re.I)),
    ("medium", "static-success", re.compile(r"(?:['\"](?:ok|success|status)['\"]\s*:\s*['\"]?(?:true|success|completed)['\"]?)|return\s*\{\s*['\"](?:ok|success)['\"]\s*:\s*True", re.I)),
    ("low", "mock-or-fixture-language", re.compile(r"\b(?:mock|fixture|facade|fake)\b", re.I)),
)


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def existing_path(value: str) -> bool:
    return (ROOT / value).exists()


def changed_paths(base: str | None, head: str, worktree: bool) -> set[str]:
    paths: set[str] = set()
    if base:
        paths.update(line.strip() for line in git("diff", "--name-only", f"{base}...{head}").splitlines() if line.strip())
    if worktree:
        paths.update(line.strip() for line in git("diff", "--name-only").splitlines() if line.strip())
        paths.update(line.strip() for line in git("diff", "--cached", "--name-only").splitlines() if line.strip())
        paths.update(line.strip() for line in git("ls-files", "--others", "--exclude-standard").splitlines() if line.strip())
    return paths


def diff_text(base: str | None, head: str, worktree: bool) -> str:
    parts: list[str] = []
    if base:
        parts.append(git("diff", "--no-ext-diff", "--unified=0", f"{base}...{head}"))
    if worktree:
        parts.append(git("diff", "--no-ext-diff", "--unified=0"))
        parts.append(git("diff", "--cached", "--no-ext-diff", "--unified=0"))
    return "\n".join(parts)


def added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    current: str | None = None
    line_no = 0
    result: dict[str, list[tuple[int, str]]] = {}
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            current = raw[6:]
            result.setdefault(current, [])
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            line_no = int(match.group(1)) if match else line_no
            continue
        if current and raw.startswith("+") and not raw.startswith("+++"):
            result[current].append((line_no, raw[1:]))
            line_no += 1
        elif current and not raw.startswith("-"):
            line_no += 1
    return result


def read_lines(path: str) -> list[str]:
    try:
        return (ROOT / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, UnicodeError):
        return []


def is_test_path(path: str) -> bool:
    lower = path.lower().replace("\\", "/")
    return "/tests/" in f"/{lower}/" or lower.startswith("tests/") or lower.endswith("_test.py") or ".test." in lower


def scan(paths: Iterable[str], additions: dict[str, list[tuple[int, str]]]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for path in sorted(set(paths)):
        if not existing_path(path) or Path(path).suffix.lower() not in TEXT_SUFFIXES:
            continue
        lines = read_lines(path)
        candidates = additions.get(path) or list(enumerate(lines, 1))
        test_file = is_test_path(path)
        for line_no, text in candidates:
            for marker in MOJIBAKE:
                if marker in text:
                    findings.append({"severity": "high", "category": "encoding", "path": path, "line": line_no, "message": f"possible encoding corruption: {marker}", "snippet": text.strip()[:240]})
                    break
            for severity, category, pattern in PATTERNS:
                if pattern.search(text):
                    effective = "info" if test_file and category in {"low", "mock-or-fixture-language", "placeholder-language"} else severity
                    findings.append({"severity": effective, "category": category, "path": path, "line": line_no, "message": "review context before accepting this marker", "snippet": text.strip()[:240]})
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="base commit/ref; omit for working-tree-only scanning")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--worktree", action="store_true", help="include staged, unstaged, and untracked files")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--fail-on-high", action="store_true")
    args = parser.parse_args()
    try:
        paths = changed_paths(args.base, args.head, args.worktree)
        findings = scan(paths, added_lines(diff_text(args.base, args.head, args.worktree)))
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    payload = {"root": str(ROOT), "paths": sorted(paths), "findings": findings, "summary": {"high": sum(x["severity"] == "high" for x in findings), "medium": sum(x["severity"] == "medium" for x in findings), "low": sum(x["severity"] == "low" for x in findings), "info": sum(x["severity"] == "info" for x in findings)}}
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Scanned {len(paths)} changed paths; findings: {len(findings)}")
        for item in findings:
            print(f"[{item['severity'].upper()}] {item['path']}:{item['line']} {item['category']}: {item['snippet']}")
        if not findings:
            print("No scanner findings. Manual review is still required.")
    return 1 if args.fail_on_high and payload["summary"]["high"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
