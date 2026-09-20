"""Ratchet actual import edges; legacy exceptions are explicit, never auto-updated.

Python AST includes function-local imports. TypeScript edges include re-exports
and dynamic imports. SQL, ContextVars and wire identities additionally require
the behavioral suites listed in modules.json; imports alone cannot prove them.
"""
from __future__ import annotations

import ast
import fnmatch
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]


def imports(path: Path, root: Path):
    source = path.relative_to(root).as_posix()
    if path.suffix == '.py':
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'), filename=source)):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ''
                if node.level:
                    package = path.relative_to(root / 'backend').with_suffix('').parts[:-1]
                    module = '.'.join((*package[:len(package) - node.level + 1], module)).rstrip('.')
                yield source, module, node.lineno
                for alias in node.names:
                    yield source, module + '.' + alias.name, node.lineno
            elif isinstance(node, ast.Import):
                for item in node.names:
                    yield source, item.name, node.lineno
    else:
        content = path.read_text(encoding='utf-8-sig')
        for match in re.finditer(r'''(?:from\s*|import\s*\(\s*|import\s*)["']([^"']+)["']''', content):
            target = match[1]
            if target.startswith('.'):
                target = (path.parent / target).resolve().relative_to(root.resolve()).as_posix()
            yield source, target, content[:match.start()].count('\n') + 1


def violations(root: Path, rules: list[dict]):
    found = {}
    for folder in ('backend/app', 'frontend/src'):
        for path in (root / folder).rglob('*'):
            if path.suffix not in {'.py', '.ts', '.tsx'}:
                continue
            for source, target, line in imports(path, root):
                for rule in rules:
                    if (any(fnmatch.fnmatch(source, p) for p in rule['sources'])
                            and any(fnmatch.fnmatch(target, p) for p in rule['forbidden'])
                            and source not in rule.get('exempt_sources', [])):
                        found[f"{rule['id']}|{source}|{target}"] = line
    return found


def audit(root: Path = ROOT):
    config = json.loads((root / 'docs/architecture/modules.json').read_text())
    baseline = json.loads((root / 'docs/architecture/legacy-imports.json').read_text())
    actual = violations(root, config['import_rules'])
    errors = [f'New boundary violation: {edge}:{line}' for edge, line in actual.items() if edge not in baseline]
    errors += [f'Remove resolved legacy exception: {edge}' for edge in baseline if edge not in actual]
    errors += [f'Exception lacks ownership/reason: {edge}' for edge, value in baseline.items()
               if not value.get('owner') or not value.get('reason') or not value.get('retire_in')]
    allowed = config['fence_bypass']
    for path in (root / 'backend/app').rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
            if isinstance(node, ast.Call) and any(k.arg == 'fence_model_job' and isinstance(k.value, ast.Constant)
                                                 and k.value.value is False for k in node.keywords):
                if path.relative_to(root).as_posix() not in allowed:
                    errors.append(f'Unauthorized model fence bypass: {path.relative_to(root)}:{node.lineno}')
    return errors


if __name__ == '__main__':
    errors = audit()
    print('\n'.join(errors) if errors else 'Architecture boundaries passed (legacy exceptions remain explicit).')
    sys.exit(bool(errors))
