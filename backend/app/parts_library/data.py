METRIC_SCREWS: dict[str, dict] = {
    "M2":   {"pitch": 0.4,  "head_dia": 3.8,  "head_h": 1.3, "clearance": 2.2,  "tap": 1.6},
    "M2.5": {"pitch": 0.45, "head_dia": 4.5,  "head_h": 1.7, "clearance": 2.7,  "tap": 2.05},
    "M3":   {"pitch": 0.5,  "head_dia": 5.5,  "head_h": 2.0, "clearance": 3.2,  "tap": 2.5},
    "M4":   {"pitch": 0.7,  "head_dia": 7.0,  "head_h": 2.8, "clearance": 4.3,  "tap": 3.3},
    "M5":   {"pitch": 0.8,  "head_dia": 8.5,  "head_h": 3.5, "clearance": 5.3,  "tap": 4.2},
    "M6":   {"pitch": 1.0,  "head_dia": 10.0, "head_h": 4.0, "clearance": 6.4,  "tap": 5.0},
    "M8":   {"pitch": 1.25, "head_dia": 13.0, "head_h": 5.3, "clearance": 8.4,  "tap": 6.8},
    "M10":  {"pitch": 1.5,  "head_dia": 16.0, "head_h": 6.4, "clearance": 10.5, "tap": 8.5},
    "M12":  {"pitch": 1.75, "head_dia": 18.0, "head_h": 7.5, "clearance": 12.5, "tap": 10.2},
}

BEARINGS: dict[str, dict] = {
    "608":  {"inner": 8,  "outer": 22, "width": 7},
    "6000": {"inner": 10, "outer": 26, "width": 8},
    "6001": {"inner": 12, "outer": 28, "width": 8},
    "6200": {"inner": 10, "outer": 30, "width": 9},
    "6201": {"inner": 12, "outer": 32, "width": 10},
    "6202": {"inner": 15, "outer": 35, "width": 11},
}


def lookup(designation: str) -> dict | None:
    key = designation.upper()
    if key in METRIC_SCREWS:
        return METRIC_SCREWS[key]
    if designation in BEARINGS:
        return BEARINGS[designation]
    return None


def format_for_prompt(designation: str) -> str:
    key = designation.upper()
    if key in METRIC_SCREWS:
        s = METRIC_SCREWS[key]
        return (
            f"{key} 螺丝: 过孔直径 {s['clearance']}mm, 底孔直径 {s['tap']}mm, "
            f"头部直径 {s['head_dia']}mm, 头部高度 {s['head_h']}mm"
        )
    if designation in BEARINGS:
        b = BEARINGS[designation]
        return (
            f"{designation} 轴承: 内径 {b['inner']}mm, 外径 {b['outer']}mm, "
            f"宽度 {b['width']}mm"
        )
    return ""
