from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel


_NUMBER = r"-?(?:\d+(?:\.\d*)?|\.\d+)"
_ASSIGNMENT_RE = re.compile(
    rf"^(?P<indent>\s*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    rf"(?P<value>{_NUMBER})(?P<suffix>\s*(?:#.*)?)$",
    re.MULTILINE,
)
_RANGE_RE = re.compile(r"#\s*\[([^\]]+)\]")
_GROUP_RE = re.compile(r"^\s*#\s*\[([^\]]+)\]\s*$")
_COMMENT_RE = re.compile(r"^\s*#\s*(.+?)\s*$")


class CADParameter(BaseModel):
    name: str
    display_name: str
    value: float
    default_value: float
    type: Literal["number"] = "number"
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str | None = None
    group: str | None = None
    comment: str | None = None
    line: int


def _to_number(text: str) -> float:
    return float(text)


def _format_number(value: float) -> str:
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.12g}"


def _display_name(name: str) -> str:
    suffixes = ["_mm", "_cm", "_m", "_deg", "_rad", "_count"]
    base = name
    for suffix in suffixes:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return " ".join(part.capitalize() for part in base.split("_") if part) or name


def _unit_for(name: str) -> str | None:
    for suffix, unit in (
        ("_mm", "mm"),
        ("_cm", "cm"),
        ("_m", "m"),
        ("_deg", "deg"),
        ("_rad", "rad"),
    ):
        if name.endswith(suffix):
            return unit
    return None


def _parse_range(suffix: str) -> tuple[float | None, float | None, float | None]:
    match = _RANGE_RE.search(suffix)
    if not match:
        return None, None, None
    pieces = [piece.strip() for piece in match.group(1).split(":")]
    if len(pieces) == 2 and all(pieces):
        return _to_number(pieces[0]), _to_number(pieces[1]), None
    if len(pieces) == 3 and all(pieces):
        return _to_number(pieces[0]), _to_number(pieces[2]), _to_number(pieces[1])
    return None, None, None


def extract_parameters(code: str) -> list[CADParameter]:
    parameters: list[CADParameter] = []
    current_group: str | None = None
    pending_comment: str | None = None

    for line_number, line in enumerate(code.splitlines(), start=1):
        group_match = _GROUP_RE.match(line)
        if group_match:
            current_group = group_match.group(1).strip() or None
            pending_comment = None
            continue

        comment_match = _COMMENT_RE.match(line)
        if comment_match:
            text = comment_match.group(1).strip()
            if not text.startswith("["):
                pending_comment = text
            continue

        match = _ASSIGNMENT_RE.match(line)
        if not match:
            if line.strip() and not line.startswith((" ", "\t")):
                pending_comment = None
            continue

        if match.group("indent"):
            pending_comment = None
            continue

        name = match.group("name")
        value = _to_number(match.group("value"))
        min_value, max_value, step = _parse_range(match.group("suffix") or "")
        parameters.append(
            CADParameter(
                name=name,
                display_name=_display_name(name),
                value=value,
                default_value=value,
                min=min_value,
                max=max_value,
                step=step,
                unit=_unit_for(name),
                group=current_group,
                comment=pending_comment,
                line=line_number,
            )
        )
        pending_comment = None

    return parameters


def apply_parameter_values(code: str, values: dict[str, float]) -> str:
    if not values:
        return code

    parameters = {parameter.name: parameter for parameter in extract_parameters(code)}
    normalized_values = {name: float(value) for name, value in values.items() if name in parameters}
    for name, value in normalized_values.items():
        parameter = parameters[name]
        if parameter.min is not None and value < parameter.min:
            raise ValueError(f"{name} value {value} is below range minimum {parameter.min}")
        if parameter.max is not None and value > parameter.max:
            raise ValueError(f"{name} value {value} is above range maximum {parameter.max}")

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in normalized_values:
            return match.group(0)
        return (
            f"{match.group('indent')}{name} = "
            f"{_format_number(normalized_values[name])}{match.group('suffix')}"
        )

    return _ASSIGNMENT_RE.sub(replace, code)

