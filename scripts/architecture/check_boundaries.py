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
        tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=source)
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ''
                if node.level:
                    package = path.relative_to(root / 'backend').with_suffix('').parts[:-1]
                    module = '.'.join((*package[:len(package) - node.level + 1], module)).rstrip('.')
                yield source, module, node.lineno
                for alias in node.names:
                    aliases[alias.asname or alias.name] = module + '.' + alias.name
                    yield source, module + '.' + alias.name, node.lineno
            elif isinstance(node, ast.Import):
                for item in node.names:
                    aliases[item.asname or item.name.split('.')[0]] = item.name if item.asname else item.name.split('.')[0]
                    yield source, item.name, node.lineno
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                parts = []
                value = node
                while isinstance(value, ast.Attribute):
                    parts.append(value.attr)
                    value = value.value
                if isinstance(value, ast.Name) and value.id in aliases:
                    target = aliases[value.id] + '.' + '.'.join(reversed(parts))
                    if target.startswith('app.'):
                        yield source, target, node.lineno
            if (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and ((isinstance(node.func, ast.Attribute) and node.func.attr == 'import_module')
                         or (isinstance(node.func, ast.Name) and node.func.id == '__import__'))):
                yield source, node.args[0].value, node.lineno
    else:
        content = path.read_text(encoding='utf-8-sig')
        for match in re.finditer(r'''(?:from\s*|import\s*\(\s*|import\s*)["']([^"']+)["']''', content):
            target = match[1]
            if target.startswith('.'):
                target = (path.parent / target).resolve().relative_to(root.resolve()).as_posix()
            elif target.startswith('@/'):
                target = 'frontend/src/' + target[2:]
            target = re.sub(r'\.(?:tsx?|jsx?)$', '', target).removesuffix('/index')
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


def _module(path: Path, root: Path) -> str:
    if path.suffix == '.py':
        return '.'.join(path.relative_to(root/'backend').with_suffix('').parts).removesuffix('.__init__')
    return path.relative_to(root).with_suffix('').as_posix().removesuffix('/index')


def _components(graph):
    indices, low, stack, active, result = {}, {}, [], set(), []
    def visit(node):
        indices[node] = low[node] = len(indices)
        stack.append(node)
        active.add(node)
        for target in sorted(graph[node]):
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in active:
                low[node] = min(low[node], indices[target])
        if low[node] == indices[node]:
            group = set()
            while True:
                item = stack.pop()
                active.remove(item)
                group.add(item)
                if item == node:
                    break
            if len(group) > 1:
                result.append(group)
    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return result


def structural_violations(root: Path, config: dict):
    """Resolve real source files, public imports, ownership, directions and cycles.

    Function-local and TYPE_CHECKING imports are included intentionally. Cyclic
    edges are individually ratcheted so adding an edge inside an existing cycle
    cannot hide behind an unchanged strongly-connected component.
    """
    files = { _module(path, root): path for folder in ('backend/app','frontend/src')
              for path in (root/folder).rglob('*') if path.suffix in {'.py','.ts','.tsx'} }
    by_source = {p.relative_to(root).as_posix():m for m,p in files.items()}
    owners = config.get('file_owners', {})
    modules = config['modules']
    found, edges = {}, {}
    graph = {name:set() for name in files}
    module_graph = {name:set() for name in modules}
    for source, module in by_source.items():
        owner = owners.get(source)
        if owner not in modules:
            found[f'ownership|{source}'] = 1
        for _, target, line in imports(files[module], root):
            if target.startswith('@/'):
                target = 'frontend/src/' + target[2:]
            target = re.sub(r'\.(?:tsx?|jsx?)$', '', target)
            resolved, symbol = target, ''
            while resolved not in files and '.' in resolved and resolved.startswith('app.'):
                resolved, _, symbol_part = resolved.rpartition('.')
                symbol = symbol_part + ('.'+symbol if symbol else '')
            if resolved not in files:
                continue
            if resolved == module:
                continue
            target_source = files[resolved].relative_to(root).as_posix()
            target_owner = owners.get(target_source)
            graph[module].add(resolved)
            edges[(module,resolved)] = (source,target_source,line)
            if any(part.startswith('_') and part != '__init__' for part in symbol.split('.')):
                found[f'private-symbol|{source}|{resolved}.{symbol}'] = line
            if owner not in modules or target_owner not in modules or owner == target_owner:
                continue
            module_graph[owner].add(target_owner)
            if target_owner not in modules[owner].get('allowed_dependencies', []):
                found[f'direction|{source}|{target_source}'] = line
            if not any(fnmatch.fnmatchcase(resolved,pattern) for pattern in modules[target_owner]['public']):
                found[f'private-module|{source}|{target_source}'] = line
    for component in _components(graph):
        for (source,target),(source_path,target_path,line) in edges.items():
            if source in component and target in component:
                found[f'file-cycle|{source_path}|{target_path}'] = line
    for component in _components(module_graph):
        for source_owner in component:
            for target_owner in module_graph[source_owner] & component:
                found[f'module-cycle|{source_owner}|{target_owner}'] = 1
    return found


def audit(root: Path = ROOT):
    config = json.loads((root / 'docs/architecture/modules.json').read_text())
    baseline = json.loads((root / 'docs/architecture/legacy-imports.json').read_text())
    actual = violations(root, config['import_rules'])
    actual.update(structural_violations(root, config))
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
