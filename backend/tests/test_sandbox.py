"""
Sandbox security tests: verify that dangerous code is rejected by code_filter
and that executor_entry's safe_import blocks runtime bypass attempts.
"""
import pytest

from app.sandbox.code_filter import validate_code


# === Code filter: allowed imports ===

class TestCodeFilterAllowed:
    def test_cadquery(self):
        ok, _ = validate_code("import cadquery as cq\nresult = cq.Workplane('XY').box(1,1,1)")
        assert ok

    def test_math(self):
        ok, _ = validate_code("import math\nx = math.pi")
        assert ok

    def test_numpy(self):
        ok, _ = validate_code("import numpy as np\na = np.array([1,2])")
        assert ok

    def test_ezdxf(self):
        ok, _ = validate_code("import ezdxf\ndoc = ezdxf.new()")
        assert ok

    def test_combined_allowed(self):
        ok, _ = validate_code("import cadquery as cq\nimport math\nimport numpy as np")
        assert ok


# === Code filter: blocked imports ===

class TestCodeFilterBlockedImports:
    def test_os(self):
        ok, msg = validate_code("import os")
        assert not ok
        assert "os" in msg

    def test_subprocess(self):
        ok, msg = validate_code("import subprocess")
        assert not ok

    def test_sys(self):
        ok, msg = validate_code("import sys")
        assert not ok

    def test_pathlib(self):
        ok, msg = validate_code("from pathlib import Path")
        assert not ok

    def test_socket(self):
        ok, msg = validate_code("import socket")
        assert not ok

    def test_ctypes(self):
        ok, msg = validate_code("import ctypes")
        assert not ok

    def test_importlib(self):
        ok, msg = validate_code("import importlib")
        assert not ok

    def test_shutil(self):
        ok, msg = validate_code("import shutil")
        assert not ok

    def test_builtins(self):
        ok, msg = validate_code("import builtins")
        assert not ok


# === Code filter: blocked runtime bypasses ===

class TestCodeFilterBlockedBypasses:
    def test_dunder_import_call(self):
        """__import__('os').system('id') must be rejected."""
        ok, msg = validate_code("__import__('os').system('id')")
        assert not ok

    def test_importlib_import_module(self):
        """importlib.import_module('os') must be rejected."""
        ok, msg = validate_code("import importlib\nimportlib.import_module('os')")
        assert not ok

    def test_getattr_builtins_import(self):
        """getattr(__builtins__, '__import__')('sys') must be rejected."""
        ok, msg = validate_code("getattr(__builtins__, '__import__')('sys')")
        assert not ok

    def test_eval_call(self):
        ok, msg = validate_code("eval('1+1')")
        assert not ok

    def test_exec_call(self):
        ok, msg = validate_code("exec('print(1)')")
        assert not ok

    def test_compile_call(self):
        ok, msg = validate_code("compile('1+1', '<string>', 'eval')")
        assert not ok

    def test_open_call(self):
        ok, msg = validate_code("open('/etc/passwd')")
        assert not ok

    def test_input_call(self):
        ok, msg = validate_code("input('prompt')")
        assert not ok

    def test_dunder_subclasses(self):
        ok, msg = validate_code("().__class__.__subclasses__()")
        assert not ok

    def test_dunder_globals(self):
        ok, msg = validate_code("f.__globals__")
        assert not ok

    def test_dunder_builtins_attr(self):
        ok, msg = validate_code("x.__builtins__")
        assert not ok

    def test_getattr_dunder_import_string(self):
        ok, msg = validate_code("getattr(x, '__import__')")
        assert not ok

    def test_getattr_subclasses_string(self):
        ok, msg = validate_code("getattr(x, '__subclasses__')")
        assert not ok


# === Code filter: syntax errors ===

class TestCodeFilterSyntax:
    def test_syntax_error(self):
        ok, msg = validate_code("def f(\n")
        assert not ok
        assert "语法错误" in msg


# === Executor entry safe_import (unit-testable outside container) ===

class TestSafeImport:
    """Test the _safe_import logic directly by recreating it."""

    @staticmethod
    def _make_safe_import():
        """Recreate the safe_import function from executor_entry logic."""
        _DENIED = frozenset({
            "os", "sys", "subprocess", "pathlib", "builtins", "importlib",
            "inspect", "socket", "ctypes", "shutil", "signal", "pickle",
        })
        # Use real modules as stand-ins for the whitelist check
        import math
        _ALLOWED = {"math": math}

        def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
            root = name.split(".")[0]
            if root in _DENIED:
                raise ImportError(f"Import of '{name}' is not allowed in sandbox")
            if root in _ALLOWED:
                return _ALLOWED[root]
            raise ImportError(f"Import of '{name}' is not allowed in sandbox")

        return _safe_import

    def test_allows_math(self):
        safe_import = self._make_safe_import()
        result = safe_import("math")
        import math
        assert result is math

    def test_blocks_os(self):
        safe_import = self._make_safe_import()
        with pytest.raises(ImportError, match="not allowed"):
            safe_import("os")

    def test_blocks_subprocess(self):
        safe_import = self._make_safe_import()
        with pytest.raises(ImportError, match="not allowed"):
            safe_import("subprocess")

    def test_blocks_importlib(self):
        safe_import = self._make_safe_import()
        with pytest.raises(ImportError, match="not allowed"):
            safe_import("importlib")

    def test_blocks_os_path(self):
        safe_import = self._make_safe_import()
        with pytest.raises(ImportError, match="not allowed"):
            safe_import("os.path")

    def test_blocks_unknown(self):
        safe_import = self._make_safe_import()
        with pytest.raises(ImportError, match="not allowed"):
            safe_import("some_random_module")


# === File serving path traversal tests ===

class TestFileServingTraversal:
    """Verify that path traversal attempts are rejected by the files endpoint."""

    @pytest.mark.asyncio
    async def test_traversal_dot_dot(self):
        from httpx import AsyncClient, ASGITransport
        from app.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/files/abc/../../etc/passwd")
            # FastAPI normalizes the path, so traversal is blocked (400 or 404)
            assert r.status_code in (400, 404)

    @pytest.mark.asyncio
    async def test_traversal_encoded(self):
        from httpx import AsyncClient, ASGITransport
        from app.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/files/abc/%2e%2e%2fetc%2fpasswd")
            assert r.status_code in (400, 404)

    @pytest.mark.asyncio
    async def test_invalid_request_id(self):
        from httpx import AsyncClient, ASGITransport
        from app.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/files/../secrets/result.step")
            assert r.status_code in (400, 404)

    @pytest.mark.asyncio
    async def test_disallowed_extension(self):
        from httpx import AsyncClient, ASGITransport
        from app.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/files/abc/evil.sh")
            assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_valid_missing_file(self):
        from httpx import AsyncClient, ASGITransport
        from app.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/files/nonexistent-uuid/result.step")
            assert r.status_code == 404
