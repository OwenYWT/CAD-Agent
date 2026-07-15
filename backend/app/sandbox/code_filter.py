import ast

ALLOWED_MODULES = {"cadquery", "cq", "math", "numpy", "np", "ezdxf"}

# Dangerous builtins that must never appear as function calls or name references
_DANGEROUS_NAMES = frozenset({
    "eval", "exec", "compile", "open", "input",
    "__import__", "breakpoint", "exit", "quit",
    "setattr", "delattr",
})

# Dangerous attribute accesses
_DANGEROUS_ATTRS = frozenset({
    "__import__", "__subclasses__", "__bases__", "__globals__",
    "__code__", "__builtins__", "__loader__", "__spec__",
    # numpy re-exports the whole ctypes module as `np.ctypeslib.ctypes`, which
    # reaches CDLL/windll and thus native host code — bypassing the import
    # whitelist entirely (no `import` statement fires). Block the bridge and the
    # native loaders directly. None of these appear in legitimate CadQuery code.
    "ctypeslib", "ctypes", "windll", "cdll", "oledll", "pydll",
    "CDLL", "WinDLL", "OleDLL", "PyDLL", "LoadLibrary",
    # ndarray.tofile / np.fromfile are arbitrary host file I/O reachable via the
    # whitelisted numpy object; CAD exports go through cq.exporters, never these.
    "tofile", "fromfile",
})


def validate_code(code: str) -> tuple[bool, str | None]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return (False, f"语法错误: {str(e)}")

    for node in ast.walk(tree):
        # Check import statements
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name.split(".")[0]
                if module_name not in ALLOWED_MODULES:
                    return (False, f"禁止导入模块: {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                module_name = node.module.split(".")[0]
                if module_name not in ALLOWED_MODULES:
                    return (False, f"禁止导入模块: {node.module}")

        # Check for dangerous function calls
        elif isinstance(node, ast.Call):
            func = node.func

            # Direct call: __import__("os"), eval("..."), exec("...")
            if isinstance(func, ast.Name):
                if func.id in _DANGEROUS_NAMES:
                    return (False, f"禁止调用: {func.id}()")

                # getattr-based bypass: getattr(x, "__import__")
                if func.id == "getattr" and len(node.args) >= 2:
                    target_arg = node.args[1]
                    if isinstance(target_arg, ast.Constant) and isinstance(target_arg.value, str):
                        if target_arg.value in _DANGEROUS_ATTRS | _DANGEROUS_NAMES:
                            return (False, f"禁止通过 getattr 访问: {target_arg.value}")

            # Attribute call: importlib.import_module("os"), builtins.__import__("os")
            if isinstance(func, ast.Attribute):
                if func.attr == "import_module":
                    return (False, "禁止调用: importlib.import_module()")
                if func.attr in _DANGEROUS_NAMES:
                    return (False, f"禁止访问: .{func.attr}()")

        # Check for dangerous attribute access (even without call)
        elif isinstance(node, ast.Attribute):
            if node.attr in _DANGEROUS_ATTRS:
                return (False, f"禁止访问属性: .{node.attr}")

        # Check for dangerous name references
        elif isinstance(node, ast.Name):
            if node.id == "__import__":
                return (False, "禁止直接访问 __import__")

    return (True, None)
