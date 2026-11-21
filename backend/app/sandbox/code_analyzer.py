"""
CadQuery static code analyzer.
Catches common CadQuery anti-patterns before sending code to the sandbox,
saving execution time and providing actionable fix hints.
"""
import ast
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _CadQueryValueProvenance:
    roots: frozenset[str]
    subtracted_roots: frozenset[str] = frozenset()

    @property
    def applies_subtraction(self) -> bool:
        return bool(self.subtracted_roots)


@dataclass(frozen=True, slots=True)
class _CadQueryFunctionSummary:
    parameter_roots: tuple[tuple[str, str], ...]
    default_values: tuple[tuple[str, _CadQueryValueProvenance], ...]
    global_roots: tuple[tuple[str, str], ...]
    return_values: tuple[_CadQueryValueProvenance, ...]


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

        # 8. Check face selectors that select opposite planes before workplane()
        warnings.extend(self._check_non_coplanar_face_selector(code))

        # 9. Check unsupported CadQuery cleanup spelling
        warnings.extend(self._check_unsupported_cleanup(code))

        # 10. Check destructive double subtraction through body-derived workplanes
        warnings.extend(self._check_straight_line_double_subtraction(code))

        return warnings

    def execution_blockers(self, code: str) -> list[str]:
        """Return only structurally proven execution blockers.

        The broader analyzer intentionally includes advisory heuristics. Those
        diagnostics are useful for generation guidance but must not reject
        otherwise executable Durable source at the Podman boundary.
        """
        warnings = []
        warnings.extend(self._check_multiple_show_object(code))
        warnings.extend(self._check_non_coplanar_face_selector(code))
        warnings.extend(
            self._check_unsupported_cleanup(
                code,
                require_explicit_workplane=True,
            )
        )
        warnings.extend(self._check_straight_line_double_subtraction(code))
        return warnings

    def _check_straight_line_double_subtraction(self, code: str) -> list[str]:
        """Detect proven double subtraction in a top-level linear basic block.

        Durable execution treats these diagnostics as hard blockers, so this
        analysis deliberately does not infer provenance through functions,
        branches, loops, exception handling, or destructuring. Names written
        by unsupported control flow are invalidated before linear analysis
        continues.
        """
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []

        warning = (
            "A body-derived cutBlind/cutThruAll/hole result already applies "
            "the subtraction to its parent body; assign that result directly "
            "instead of subtracting it from the parent again."
        )
        subtractive_methods = {
            "cutBlind",
            "cutThruAll",
            "hole",
            "cboreHole",
            "cskHole",
        }
        provenance: dict[str, _CadQueryValueProvenance] = {}
        warnings: list[str] = []

        def expression_provenance(
            expression: ast.AST,
            *,
            fallback_root: str,
        ) -> _CadQueryValueProvenance:
            value = _CadQueryValueProvenance(roots=frozenset())
            if isinstance(expression, ast.Name):
                value = provenance.get(expression.id, value)
            elif isinstance(expression, ast.Attribute):
                value = expression_provenance(
                    expression.value,
                    fallback_root="",
                )
            elif (
                isinstance(expression, ast.Call)
                and isinstance(expression.func, ast.Attribute)
            ):
                receiver = expression_provenance(
                    expression.func.value,
                    fallback_root="",
                )
                value = receiver
                if expression.func.attr in subtractive_methods and receiver.roots:
                    value = _CadQueryValueProvenance(
                        roots=receiver.roots,
                        subtracted_roots=(
                            receiver.subtracted_roots | receiver.roots
                        ),
                    )
            elif isinstance(expression, ast.UnaryOp):
                value = expression_provenance(
                    expression.operand,
                    fallback_root="",
                )
            elif (
                isinstance(expression, ast.BinOp)
                and isinstance(expression.op, ast.Sub)
            ):
                value = expression_provenance(
                    expression.left,
                    fallback_root="",
                )

            if not value.roots and fallback_root:
                return _CadQueryValueProvenance(
                    roots=frozenset({fallback_root})
                )
            return value

        def is_double_subtraction(expression: ast.AST) -> bool:
            parent_name: str | None = None
            candidate: ast.AST | None = None
            if (
                isinstance(expression, ast.BinOp)
                and isinstance(expression.op, ast.Sub)
                and isinstance(expression.left, ast.Name)
            ):
                parent_name = expression.left.id
                candidate = expression.right
            elif (
                isinstance(expression, ast.Call)
                and isinstance(expression.func, ast.Attribute)
                and expression.func.attr == "cut"
                and isinstance(expression.func.value, ast.Name)
                and expression.args
            ):
                parent_name = expression.func.value.id
                candidate = expression.args[0]
            if (
                parent_name is None
                or candidate is None
                or parent_name not in provenance
            ):
                return False
            candidate_value = expression_provenance(
                candidate,
                fallback_root="",
            )
            return bool(
                provenance[parent_name].roots
                & candidate_value.subtracted_roots
            )

        def record_expression(expression: ast.AST | None) -> None:
            if expression is not None and is_double_subtraction(expression):
                warnings.append(warning)

        def target_names(target: ast.AST) -> set[str]:
            if isinstance(target, ast.Name):
                return {target.id}
            if isinstance(target, (ast.Tuple, ast.List)):
                return {
                    name
                    for element in target.elts
                    for name in target_names(element)
                }
            if isinstance(target, ast.Starred):
                return target_names(target.value)
            return set()

        def names_written_by_control_flow(statement: ast.stmt) -> set[str]:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return {
                    statement.name,
                    *(
                        name
                        for node in ast.walk(statement)
                        if isinstance(node, ast.Global)
                        for name in node.names
                    ),
                }

            class WrittenNameCollector(ast.NodeVisitor):
                def __init__(self) -> None:
                    self.names: set[str] = set()
                    self.has_wildcard_import = False

                def visit_Name(self, node: ast.Name) -> None:
                    if isinstance(node.ctx, (ast.Store, ast.Del)):
                        self.names.add(node.id)

                def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                    self.names.add(node.name)

                def visit_AsyncFunctionDef(
                    self,
                    node: ast.AsyncFunctionDef,
                ) -> None:
                    self.names.add(node.name)

                def visit_ClassDef(self, node: ast.ClassDef) -> None:
                    self.names.add(node.name)
                    self.generic_visit(node)

                def visit_alias(self, node: ast.alias) -> None:
                    if node.name == "*":
                        self.has_wildcard_import = True
                        return
                    self.names.add(node.asname or node.name.split(".", 1)[0])

                def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
                    if node.name:
                        self.names.add(node.name)
                    self.generic_visit(node)

                def visit_MatchAs(self, node: ast.MatchAs) -> None:
                    if node.name:
                        self.names.add(node.name)
                    self.generic_visit(node)

                def visit_MatchStar(self, node: ast.MatchStar) -> None:
                    if node.name:
                        self.names.add(node.name)

                def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
                    if node.rest:
                        self.names.add(node.rest)
                    self.generic_visit(node)

            collector = WrittenNameCollector()
            collector.visit(statement)
            if collector.has_wildcard_import:
                return set(provenance)
            return collector.names

        unsupported_control_flow = (
            ast.FunctionDef,
            ast.AsyncFunctionDef,
            ast.ClassDef,
            ast.If,
            ast.For,
            ast.AsyncFor,
            ast.While,
            ast.Try,
            ast.With,
            ast.AsyncWith,
            ast.Match,
        )

        for statement in tree.body:
            if isinstance(statement, unsupported_control_flow):
                for name in names_written_by_control_flow(statement):
                    provenance.pop(name, None)
                continue

            if isinstance(statement, ast.Assign):
                record_expression(statement.value)
                simple_targets = [
                    target
                    for target in statement.targets
                    if isinstance(target, ast.Name)
                ]
                complex_names = {
                    name
                    for target in statement.targets
                    if not isinstance(target, ast.Name)
                    for name in target_names(target)
                }
                for name in complex_names:
                    provenance.pop(name, None)
                if simple_targets:
                    value = expression_provenance(
                        statement.value,
                        fallback_root=(
                            f"assignment:{statement.lineno}:"
                            f"{statement.col_offset}"
                        ),
                    )
                    for target in simple_targets:
                        provenance[target.id] = value
                continue

            if isinstance(statement, ast.AnnAssign):
                record_expression(statement.value)
                names = target_names(statement.target)
                if (
                    isinstance(statement.target, ast.Name)
                    and statement.value is not None
                ):
                    provenance[statement.target.id] = expression_provenance(
                        statement.value,
                        fallback_root=(
                            f"assignment:{statement.lineno}:"
                            f"{statement.col_offset}"
                        ),
                    )
                else:
                    for name in names:
                        provenance.pop(name, None)
                continue

            if isinstance(statement, ast.AugAssign):
                record_expression(statement.value)
                if isinstance(statement.target, ast.Name):
                    parent = provenance.get(statement.target.id)
                    candidate = expression_provenance(
                        statement.value,
                        fallback_root="",
                    )
                    if (
                        isinstance(statement.op, ast.Sub)
                        and parent is not None
                        and bool(parent.roots & candidate.subtracted_roots)
                    ):
                        warnings.append(warning)
                    if parent is not None:
                        provenance[statement.target.id] = (
                            _CadQueryValueProvenance(
                                roots=parent.roots | candidate.roots,
                                subtracted_roots=(
                                    parent.subtracted_roots
                                    | candidate.subtracted_roots
                                ),
                            )
                        )
                else:
                    for name in target_names(statement.target):
                        provenance.pop(name, None)
                continue

            if isinstance(statement, ast.Expr):
                record_expression(statement.value)

        return list(dict.fromkeys(warnings))

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
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return warnings
        show_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "show_object"
        ]
        count = len(show_calls)
        if count > 1:
            warnings.append(
                f"检测到 {count} 次 show_object() 调用，但只允许 1 次。"
                "所有子特征（筋、柱、耳、槽等）必须通过 .union() 或 .cut() "
                "合并到主体 result 上，最终只调用一次 show_object(result)。"
            )

        # Use the syntax tree here. A multiline regex with repeated whitespace
        # has catastrophic backtracking on normal generated modeling loops.
        parent_by_node = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        show_object_in_loop = False
        loop_nodes = (
            ast.For,
            ast.AsyncFor,
            ast.While,
            ast.ListComp,
            ast.SetComp,
            ast.DictComp,
            ast.GeneratorExp,
        )
        for node in show_calls:
            parent = parent_by_node.get(node)
            while parent is not None and not isinstance(
                parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            ):
                if isinstance(parent, loop_nodes):
                    show_object_in_loop = True
                    break
                parent = parent_by_node.get(parent)
            if show_object_in_loop:
                break

        if show_object_in_loop:
            warnings.append(
                "show_object() appears inside a loop and would emit multiple "
                "independent objects. Union each feature into result inside the "
                "loop, then call show_object(result) once after the loop."
            )

        return warnings

    def _check_non_coplanar_face_selector(self, code: str) -> list[str]:
        """Reject ``faces('|X')``-style selectors used to create workplanes.

        CadQuery's parallel-axis face selectors return both opposite faces.  A
        subsequent ``workplane()`` then raises ``Selected faces must be
        co-planar`` because those faces lie on different planes.  Directional
        selectors (``>X``/``<X``) remain valid and are intentionally ignored.
        """
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []

        axes: set[str] = set()
        narrowing_methods = {"first", "last", "item"}
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "workplane"
            ):
                continue
            receiver = node.func.value
            narrowed = False
            while (
                isinstance(receiver, ast.Call)
                and isinstance(receiver.func, ast.Attribute)
            ):
                method = receiver.func.attr
                if method in narrowing_methods:
                    narrowed = True
                elif (
                    method == "faces"
                    and receiver.args
                    and isinstance(receiver.args[0], ast.Constant)
                    and isinstance(receiver.args[0].value, str)
                ):
                    selector = receiver.args[0].value.strip().upper()
                    if re.fullmatch(r"[<>][XYZ]", selector):
                        narrowed = True
                    elif re.fullmatch(r"\|[XYZ]", selector) and not narrowed:
                        axes.add(selector[1])
                receiver = receiver.func.value
        if not axes:
            return []

        joined_axes = ", ".join(sorted(axes))
        return [
            "Selected faces must be co-planar: faces('|{0}') selects opposite "
            "parallel faces before workplane(); use an explicit > or < face "
            "selector, or create separate workplanes for each side.".format(joined_axes)
        ]

    def _check_unsupported_cleanup(
        self,
        code: str,
        *,
        require_explicit_workplane: bool = False,
    ) -> list[str]:
        """Detect the common but unsupported ``Workplane.cleanup()`` call."""
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []
        cleanup_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "cleanup"
        ]

        def has_explicit_workplane(receiver: ast.AST) -> bool:
            while (
                isinstance(receiver, ast.Call)
                and isinstance(receiver.func, ast.Attribute)
            ):
                if (
                    receiver.func.attr == "Workplane"
                    and isinstance(receiver.func.value, ast.Name)
                    and receiver.func.value.id in {"cq", "cadquery"}
                ):
                    return True
                receiver = receiver.func.value
            return False

        if any(
            not require_explicit_workplane
            or has_explicit_workplane(call.func.value)
            for call in cleanup_calls
        ):
            return [
                "Workplane.cleanup() is not a CadQuery Workplane API; remove it "
                "or use the supported operation without cleanup()."
            ]
        return []

    def _check_body_derived_double_subtraction(self, code: str) -> list[str]:
        """Detect subtracting an already modified derivative from its parent.

        CadQuery operations such as ``cutBlind`` on ``body.faces(...).workplane()``
        return the complete modified body. Treating that return value as an
        independent cutter and subtracting it from ``body`` leaves only the
        removed material.
        """
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []

        warnings: list[str] = []
        function_summaries: dict[str, _CadQueryFunctionSummary] = {}
        warning = (
            "A body-derived cutBlind/cutThruAll/hole result already applies "
            "the subtraction to its parent body; assign that result directly "
            "instead of subtracting it from the parent again."
        )

        def value_for_name(
            name: str,
            provenance: dict[str, _CadQueryValueProvenance],
        ) -> _CadQueryValueProvenance:
            return provenance.get(
                name,
                _CadQueryValueProvenance(
                    roots=frozenset({f"external:{name}"})
                ),
            )

        def merge_values(
            values: list[_CadQueryValueProvenance],
        ) -> _CadQueryValueProvenance:
            return _CadQueryValueProvenance(
                roots=frozenset(
                    root
                    for value in values
                    for root in value.roots
                ),
                subtracted_roots=frozenset(
                    root
                    for value in values
                    for root in value.subtracted_roots
                ),
            )

        def deduplicate_environments(
            environments: list[dict[str, _CadQueryValueProvenance]],
        ) -> list[dict[str, _CadQueryValueProvenance]]:
            unique: list[dict[str, _CadQueryValueProvenance]] = []
            seen: set[tuple] = set()
            for environment in environments:
                signature = tuple(
                    sorted(
                        (
                            name,
                            tuple(sorted(value.roots)),
                            tuple(sorted(value.subtracted_roots)),
                        )
                        for name, value in environment.items()
                    )
                )
                if signature not in seen:
                    seen.add(signature)
                    unique.append(environment)
            return unique

        def instantiate_summary(
            call: ast.Call,
            provenance: dict[str, _CadQueryValueProvenance],
        ) -> _CadQueryValueProvenance | None:
            if not isinstance(call.func, ast.Name):
                return None
            summary = function_summaries.get(call.func.id)
            if summary is None:
                return None

            actual_by_root: dict[str, _CadQueryValueProvenance] = {}
            defaults = dict(summary.default_values)
            keyword_values = {
                keyword.arg: keyword.value
                for keyword in call.keywords
                if keyword.arg is not None
            }
            for index, (parameter_name, parameter_root) in enumerate(
                summary.parameter_roots
            ):
                argument: ast.AST | None = None
                if index < len(call.args):
                    argument = call.args[index]
                elif parameter_name in keyword_values:
                    argument = keyword_values[parameter_name]
                if argument is not None:
                    actual_by_root[parameter_root] = expression_provenance(
                        argument,
                        provenance,
                        fallback_root="",
                    )
                elif parameter_name in defaults:
                    actual_by_root[parameter_root] = defaults[parameter_name]

            for global_name, global_root in summary.global_roots:
                actual_by_root[global_root] = value_for_name(
                    global_name,
                    provenance,
                )

            instantiated_values: list[_CadQueryValueProvenance] = []
            for return_value in summary.return_values:
                roots: set[str] = set()
                subtracted_roots: set[str] = set()
                for root in return_value.roots:
                    actual = actual_by_root.get(root)
                    if actual is None:
                        roots.add(root)
                    else:
                        roots.update(actual.roots)
                        subtracted_roots.update(actual.subtracted_roots)
                for root in return_value.subtracted_roots:
                    actual = actual_by_root.get(root)
                    if actual is None:
                        subtracted_roots.add(root)
                    else:
                        subtracted_roots.update(actual.roots)
                        subtracted_roots.update(actual.subtracted_roots)
                instantiated_values.append(
                    _CadQueryValueProvenance(
                        roots=frozenset(roots),
                        subtracted_roots=frozenset(subtracted_roots),
                    )
                )
            return merge_values(instantiated_values)

        def expression_provenance(
            expression: ast.AST,
            provenance: dict[str, _CadQueryValueProvenance],
            *,
            fallback_root: str,
        ) -> _CadQueryValueProvenance:
            dependencies = {
                node.id for node in ast.walk(expression)
                if isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in provenance
            }
            values = [provenance[dependency] for dependency in dependencies]
            values.extend(
                summary_value
                for node in ast.walk(expression)
                if isinstance(node, ast.Call)
                for summary_value in [instantiate_summary(node, provenance)]
                if summary_value is not None
            )
            inherited = merge_values(values)
            roots = inherited.roots
            subtracted_roots = inherited.subtracted_roots
            applies_subtraction = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {"cutBlind", "cutThruAll", "hole", "cboreHole", "cskHole"}
                for node in ast.walk(expression)
            )
            fallback_roots = frozenset({fallback_root}) if fallback_root else frozenset()
            roots = roots or fallback_roots
            if applies_subtraction:
                subtracted_roots = subtracted_roots | roots
            return _CadQueryValueProvenance(
                roots=roots,
                subtracted_roots=subtracted_roots,
            )

        def is_double_subtraction(
            expression: ast.AST,
            provenance: dict[str, _CadQueryValueProvenance],
        ) -> bool:
            candidate: ast.AST | None = None
            parent_name: str | None = None
            if (
                isinstance(expression, ast.BinOp)
                and isinstance(expression.op, ast.Sub)
                and isinstance(expression.left, ast.Name)
            ):
                parent_name = expression.left.id
                candidate = expression.right
            elif (
                isinstance(expression, ast.Call)
                and isinstance(expression.func, ast.Attribute)
                and expression.func.attr == "cut"
                and isinstance(expression.func.value, ast.Name)
                and expression.args
            ):
                parent_name = expression.func.value.id
                candidate = expression.args[0]
            if parent_name is None or candidate is None:
                return False
            parent_value = value_for_name(parent_name, provenance)
            candidate_value = expression_provenance(
                candidate,
                provenance,
                fallback_root="",
            )
            return (
                bool(parent_value.roots & candidate_value.subtracted_roots)
            )

        def expression_has_double_subtraction(
            expression: ast.AST | None,
            provenance: dict[str, _CadQueryValueProvenance],
        ) -> bool:
            if expression is None:
                return False
            return any(
                is_double_subtraction(node, provenance)
                for node in ast.walk(expression)
            )

        def parameter_provenance(
            function: ast.FunctionDef | ast.AsyncFunctionDef,
            arguments: ast.arguments,
        ) -> tuple[
            dict[str, _CadQueryValueProvenance],
            tuple[tuple[str, str], ...],
        ]:
            parameters = [
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            ]
            if arguments.vararg is not None:
                parameters.append(arguments.vararg)
            if arguments.kwarg is not None:
                parameters.append(arguments.kwarg)
            parameter_roots = tuple(
                (
                    parameter.arg,
                    f"parameter:{function.name}:{function.lineno}:{parameter.arg}",
                )
                for parameter in parameters
            )
            return (
                {
                    name: _CadQueryValueProvenance(roots=frozenset({root}))
                    for name, root in parameter_roots
                },
                parameter_roots,
            )

        def record_expression_warning(
            expression: ast.AST | None,
            provenance: dict[str, _CadQueryValueProvenance],
        ) -> None:
            if expression_has_double_subtraction(expression, provenance):
                warnings.append(warning)

        def bind_target(
            target: ast.AST,
            provenance: dict[str, _CadQueryValueProvenance],
            value: _CadQueryValueProvenance,
        ) -> None:
            if isinstance(target, ast.Name):
                provenance[target.id] = value
            elif isinstance(target, (ast.Tuple, ast.List)):
                for element in target.elts:
                    bind_target(element, provenance, value)

        def names_needed_by(statements: list[ast.stmt]) -> set[str]:
            names = {
                node.id
                for statement in statements
                for node in ast.walk(statement)
                if isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
            }
            for statement in statements:
                for node in ast.walk(statement):
                    if not (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                    ):
                        continue
                    summary = function_summaries.get(node.func.id)
                    if summary is not None:
                        names.update(name for name, _root in summary.global_roots)
            return names

        def analyze_statements(
            statements: list[ast.stmt],
            environments: list[dict[str, _CadQueryValueProvenance]],
            *,
            return_values: list[_CadQueryValueProvenance] | None = None,
            required_after: set[str] | None = None,
        ) -> list[dict[str, _CadQueryValueProvenance]]:
            active_environments = environments
            required_after = required_after or set()
            for index, statement in enumerate(statements):
                needed_after = required_after | names_needed_by(
                    statements[index + 1:]
                )
                next_environments: list[dict[str, _CadQueryValueProvenance]] = []
                for provenance in active_environments:
                    next_environments.extend(
                        analyze_statement(
                            statement,
                            provenance,
                            return_values=return_values,
                            required_after=needed_after,
                        )
                    )
                # Function definitions can add call-time global dependencies
                # to summaries, so recalculate liveness before pruning.
                needed_after = required_after | names_needed_by(
                    statements[index + 1:]
                )
                active_environments = deduplicate_environments(
                    [
                        {
                            name: value
                            for name, value in environment.items()
                            if name in needed_after
                        }
                        for environment in next_environments
                    ]
                )
                if not active_environments:
                    break
            return active_environments

        def analyze_statement(
            statement: ast.stmt,
            provenance: dict[str, _CadQueryValueProvenance],
            *,
            return_values: list[_CadQueryValueProvenance] | None,
            required_after: set[str],
        ) -> list[dict[str, _CadQueryValueProvenance]]:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parameter_values, parameter_roots = parameter_provenance(
                    statement,
                    statement.args,
                )
                parameter_names = {name for name, _root in parameter_roots}
                local_names = {
                    node.id
                    for node in ast.walk(statement)
                    if isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Store)
                }
                loaded_names = names_needed_by(statement.body)
                global_names = sorted(
                    (loaded_names & provenance.keys())
                    - parameter_names
                    - local_names
                )
                global_roots = tuple(
                    (
                        name,
                        f"global:{statement.name}:{statement.lineno}:{name}",
                    )
                    for name in global_names
                )
                function_environment = {
                    name: _CadQueryValueProvenance(roots=frozenset({root}))
                    for name, root in global_roots
                }
                function_environment.update(parameter_values)

                positional_parameters = [
                    *statement.args.posonlyargs,
                    *statement.args.args,
                ]
                default_values: dict[str, _CadQueryValueProvenance] = {}
                if statement.args.defaults:
                    default_parameters = positional_parameters[
                        -len(statement.args.defaults):
                    ]
                    for parameter, default in zip(
                        default_parameters,
                        statement.args.defaults,
                    ):
                        default_values[parameter.arg] = expression_provenance(
                            default,
                            provenance,
                            fallback_root=(
                                f"default:{statement.name}:{statement.lineno}:"
                                f"{parameter.arg}"
                            ),
                        )
                for parameter, default in zip(
                    statement.args.kwonlyargs,
                    statement.args.kw_defaults,
                ):
                    if default is not None:
                        default_values[parameter.arg] = expression_provenance(
                            default,
                            provenance,
                            fallback_root=(
                                f"default:{statement.name}:{statement.lineno}:"
                                f"{parameter.arg}"
                            ),
                        )

                function_returns: list[_CadQueryValueProvenance] = []
                analyze_statements(
                    statement.body,
                    [function_environment],
                    return_values=function_returns,
                )
                if function_returns:
                    function_summaries[statement.name] = _CadQueryFunctionSummary(
                        parameter_roots=parameter_roots,
                        default_values=tuple(default_values.items()),
                        global_roots=global_roots,
                        return_values=tuple(function_returns),
                    )
                return [provenance]

            if isinstance(statement, ast.ClassDef):
                analyze_statements(statement.body, [provenance.copy()])
                return [provenance]

            if isinstance(statement, ast.Assign):
                record_expression_warning(statement.value, provenance)
                updated = provenance.copy()
                if (
                    len(statement.targets) == 1
                    and isinstance(statement.targets[0], (ast.Tuple, ast.List))
                    and isinstance(statement.value, (ast.Tuple, ast.List))
                    and len(statement.targets[0].elts) == len(statement.value.elts)
                    and not any(
                        isinstance(element, ast.Starred)
                        for element in statement.targets[0].elts
                    )
                ):
                    for element_index, (target, value_expression) in enumerate(
                        zip(statement.targets[0].elts, statement.value.elts)
                    ):
                        bind_target(
                            target,
                            updated,
                            expression_provenance(
                                value_expression,
                                provenance,
                                fallback_root=(
                                    f"assignment:{statement.lineno}:"
                                    f"{statement.col_offset}:{element_index}"
                                ),
                            ),
                        )
                else:
                    value = expression_provenance(
                        statement.value,
                        provenance,
                        fallback_root=(
                            f"assignment:{statement.lineno}:{statement.col_offset}"
                        ),
                    )
                    for target in statement.targets:
                        if isinstance(target, (ast.Tuple, ast.List)):
                            for element_index, element in enumerate(target.elts):
                                bind_target(
                                    element,
                                    updated,
                                    _CadQueryValueProvenance(
                                        roots=frozenset(
                                            {
                                                f"unpack:{statement.lineno}:"
                                                f"{statement.col_offset}:"
                                                f"{element_index}"
                                            }
                                        )
                                    ),
                                )
                        else:
                            bind_target(target, updated, value)
                return [updated]

            if isinstance(statement, ast.AnnAssign):
                record_expression_warning(statement.value, provenance)
                if statement.value is None:
                    return [provenance]
                updated = provenance.copy()
                bind_target(
                    statement.target,
                    updated,
                    expression_provenance(
                        statement.value,
                        provenance,
                        fallback_root=f"assignment:{statement.lineno}:{statement.col_offset}",
                    ),
                )
                return [updated]

            if isinstance(statement, ast.AugAssign):
                record_expression_warning(statement.value, provenance)
                updated = provenance.copy()
                if isinstance(statement.target, ast.Name):
                    target_value = value_for_name(statement.target.id, provenance)
                    candidate_value = expression_provenance(
                        statement.value,
                        provenance,
                        fallback_root="",
                    )
                    if (
                        isinstance(statement.op, ast.Sub)
                        and bool(
                            target_value.roots
                            & candidate_value.subtracted_roots
                        )
                    ):
                        warnings.append(warning)
                    updated[statement.target.id] = merge_values(
                        [target_value, candidate_value]
                    )
                return [updated]

            if isinstance(statement, ast.If):
                record_expression_warning(statement.test, provenance)
                body_environments = analyze_statements(
                    statement.body,
                    [provenance.copy()],
                    return_values=return_values,
                    required_after=required_after,
                )
                else_environments = analyze_statements(
                    statement.orelse,
                    [provenance.copy()],
                    return_values=return_values,
                    required_after=required_after,
                )
                return body_environments + else_environments

            if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                condition = (
                    statement.test
                    if isinstance(statement, ast.While)
                    else statement.iter
                )
                record_expression_warning(condition, provenance)
                body_environment = provenance.copy()
                if isinstance(statement, (ast.For, ast.AsyncFor)):
                    bind_target(
                        statement.target,
                        body_environment,
                        _CadQueryValueProvenance(
                            roots=frozenset(
                                {f"loop:{statement.lineno}:{statement.col_offset}"}
                            )
                        ),
                    )
                body_environments = analyze_statements(
                    statement.body,
                    [body_environment],
                    return_values=return_values,
                    required_after=required_after,
                )
                exit_environments = [provenance.copy(), *body_environments]
                if statement.orelse:
                    exit_environments = analyze_statements(
                        statement.orelse,
                        exit_environments,
                        return_values=return_values,
                        required_after=required_after,
                    )
                return exit_environments

            if isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    record_expression_warning(item.context_expr, provenance)
                return analyze_statements(
                    statement.body,
                    [provenance.copy()],
                    return_values=return_values,
                    required_after=required_after,
                )

            if isinstance(statement, ast.Try):
                body_environments = analyze_statements(
                    statement.body,
                    [provenance.copy()],
                    return_values=return_values,
                    required_after=required_after,
                )
                branch_environments = list(body_environments)
                for handler in statement.handlers:
                    branch_environments.extend(
                        analyze_statements(
                            handler.body,
                            [provenance.copy()],
                            return_values=return_values,
                            required_after=required_after,
                        )
                    )
                if statement.orelse:
                    body_environments = analyze_statements(
                        statement.orelse,
                        body_environments,
                        return_values=return_values,
                        required_after=required_after,
                    )
                    branch_environments.extend(body_environments)
                branch_environments = deduplicate_environments(branch_environments)
                if statement.finalbody:
                    branch_environments = analyze_statements(
                        statement.finalbody,
                        branch_environments,
                        return_values=return_values,
                        required_after=required_after,
                    )
                return branch_environments

            if isinstance(statement, ast.Return):
                record_expression_warning(statement.value, provenance)
                if return_values is not None and statement.value is not None:
                    return_values.append(
                        expression_provenance(
                            statement.value,
                            provenance,
                            fallback_root=f"return:{statement.lineno}:{statement.col_offset}",
                        )
                    )
                return []

            if isinstance(statement, ast.Expr):
                record_expression_warning(statement.value, provenance)
            elif isinstance(statement, ast.Assert):
                record_expression_warning(statement.test, provenance)
                record_expression_warning(statement.msg, provenance)
            elif isinstance(statement, ast.Raise):
                record_expression_warning(statement.exc, provenance)
                record_expression_warning(statement.cause, provenance)
            return [provenance]

        analyze_statements(tree.body, [{}])
        return list(dict.fromkeys(warnings))


# Singleton
analyzer = CadQueryAnalyzer()


def analyze_code(code: str) -> list[str]:
    """Convenience function. Returns list of warnings (empty = OK)."""
    return analyzer.analyze(code)


def analyze_execution_code(code: str) -> list[str]:
    """Return only proven blockers that must stop sandbox execution."""
    return analyzer.execution_blockers(code)
