"""
CadQuery static code analyzer.
Catches common CadQuery anti-patterns before sending code to the sandbox,
saving execution time and providing actionable fix hints.
"""
import ast
import re


class CadQueryAnalyzer:
    """AST-based static analyzer for CadQuery code."""

    def analyze(self, code: str) -> list[str]:
        """Return a list of warning/error strings. Empty = code looks OK."""
        warnings = []

        # 1. Check show_object exists
        if "show_object" not in code and "result" not in code:
            warnings.append(
                "代码缺少 show_object(result) 或 result 变量。"
                "必须调用 show_object(result) 或定义 result 变量。"
            )

        # 2. Check unclosed polyline before extrude/revolve
        warnings.extend(self._check_unclosed_wire(code))

        # 3. Check revolve axis issues
        warnings.extend(self._check_revolve(code))

        # 4. Check fillet safety
        warnings.extend(self._check_fillet_safety(code))

        # 5. Check empty stack boolean
        warnings.extend(self._check_empty_stack_boolean(code))

        # 6. Check shell + fillet order
        warnings.extend(self._check_shell_fillet_order(code))

        # 7. Check multiple show_object calls (must be single entity output)
        warnings.extend(self._check_multiple_show_object(code))

        return warnings

    def _check_unclosed_wire(self, code: str) -> list[str]:
        """Detect polyline/lineTo without .close() before extrude/revolve."""
        warnings = []
        # Find chains that use lineTo/polyline but don't close before extrude/revolve
        # Simple heuristic: if lineTo appears and extrude/revolve appears, .close() should too
        has_lineTo = ".lineTo(" in code or ".polyline(" in code
        has_extrude = ".extrude(" in code or ".revolve(" in code
        has_close = ".close()" in code

        if has_lineTo and has_extrude and not has_close:
            warnings.append(
                "使用 .lineTo()/.polyline() 画轮廓后必须调用 .close() 闭合，"
                "然后才能 .extrude() 或 .revolve()。"
            )
        return warnings

    def _check_revolve(self, code: str) -> list[str]:
        """Check revolve usage patterns."""
        warnings = []
        if ".revolve(" not in code:
            return warnings

        # Check for negative X coordinates in moveTo/lineTo before revolve
        # This is a common mistake - profile crossing the revolve axis
        lines = code.split("\n")
        in_profile = False
        for line in lines:
            if ".moveTo(" in line or ".lineTo(" in line:
                in_profile = True
                # Extract coordinates
                match = re.search(r'\.\w+To\(\s*(-?\d+\.?\d*)', line)
                if match:
                    x_val = float(match.group(1))
                    if x_val < 0:
                        warnings.append(
                            f"revolve 截面中 X 坐标为负值 ({x_val})。"
                            "旋转体截面所有点的 X 坐标必须 >= 0，不能跨越旋转轴。"
                        )
                        break
        return warnings

    def _check_fillet_safety(self, code: str) -> list[str]:
        """Warn if fillet radius might be too large."""
        warnings = []
        # Extract fillet calls and parameter values
        fillet_match = re.findall(r'\.fillet\(\s*(\w+)\s*\)', code)
        if not fillet_match:
            return warnings

        # Extract parameter definitions
        params = {}
        for match in re.finditer(r'^(\w+)\s*=\s*(\d+\.?\d*)', code, re.MULTILINE):
            params[match.group(1)] = float(match.group(2))

        for fillet_arg in fillet_match:
            fillet_val = params.get(fillet_arg)
            if fillet_val is None:
                try:
                    fillet_val = float(fillet_arg)
                except ValueError:
                    continue

            # Check against dimension params
            dim_values = [v for k, v in params.items()
                         if any(kw in k for kw in ['width', 'height', 'depth', 'length', 'thickness'])]
            if dim_values:
                min_dim = min(dim_values)
                if fillet_val > min_dim * 0.4:
                    warnings.append(
                        f"fillet 半径 {fillet_val}mm 可能过大 (最小尺寸 {min_dim}mm 的 "
                        f"{fillet_val/min_dim*100:.0f}%)。建议不超过最短边的 40%。"
                    )
        return warnings

    def _check_empty_stack_boolean(self, code: str) -> list[str]:
        """Detect potential empty stack union/cut."""
        warnings = []
        # Pattern: .workplane().union() or creating workplane then immediately boolean
        if re.search(r'Workplane\([^)]*\)\s*\.\s*union\(', code):
            warnings.append(
                "在新建的空 Workplane 上直接调用 .union() 会失败。"
                "必须先在 Workplane 上创建实体 (.box()/.extrude() 等) 再做布尔运算。"
            )
        return warnings

    def _check_shell_fillet_order(self, code: str) -> list[str]:
        """Check that shell comes before fillet."""
        warnings = []
        shell_pos = code.find(".shell(")
        fillet_pos = code.find(".fillet(")
        if shell_pos > 0 and fillet_pos > 0 and fillet_pos < shell_pos:
            warnings.append(
                "检测到先 .fillet() 再 .shell()，这容易导致 shell 失败。"
                "正确顺序: 先 .shell() 再 .fillet()。"
            )
        return warnings

    def _check_multiple_show_object(self, code: str) -> list[str]:
        """Detect multiple show_object calls — all features must be merged into one entity."""
        warnings = []
        count = len(re.findall(r'show_object\s*\(', code))
        if count > 1:
            warnings.append(
                f"检测到 {count} 次 show_object() 调用，但只允许 1 次。"
                "所有子特征（筋、柱、耳、槽等）必须通过 .union() 或 .cut() "
                "合并到主体 result 上，最终只调用一次 show_object(result)。"
            )

        # Also check show_object inside loops
        if re.search(r'for\s+.*:.*\n(?:\s+.*\n)*?\s+show_object\s*\(', code, re.MULTILINE):
            warnings.append(
                "show_object() 出现在循环体内，这会输出多个独立实体。"
                "应在循环体内用 result = result.union(feature) 合并，"
                "循环结束后调用一次 show_object(result)。"
            )

        return warnings


# Singleton
analyzer = CadQueryAnalyzer()


def analyze_code(code: str) -> list[str]:
    """Convenience function. Returns list of warnings (empty = OK)."""
    return analyzer.analyze(code)
