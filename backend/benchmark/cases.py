BENCHMARK_CASES: list[dict] = [
    # === Simple (10) ===
    {
        "id": "S01",
        "description": "一个 50x30x20mm 的实心长方体",
        "difficulty": "simple",
        "expected_dims": {"width": 50, "height": 30, "depth": 20},
    },
    {
        "id": "S02",
        "description": "直径 40mm、高 60mm 的圆柱",
        "difficulty": "simple",
        "expected_dims": {"width": 40, "height": 40, "depth": 60},
    },
    {
        "id": "S03",
        "description": "100x60x40mm 的空心盒子，壁厚 2mm",
        "difficulty": "simple",
        "expected_dims": {"width": 100, "height": 60, "depth": 40},
    },
    {
        "id": "S04",
        "description": "直径 50mm 厚 5mm 的圆形平板",
        "difficulty": "simple",
        "expected_dims": {"width": 50, "height": 50, "depth": 5},
    },
    {
        "id": "S05",
        "description": "80x50x3mm 的平板，四角 M3 安装孔边距 5mm",
        "difficulty": "simple",
        "expected_dims": {"width": 80, "height": 50, "depth": 3},
    },
    {
        "id": "S06",
        "description": "60x40x30mm 的实心长方体，所有边 R3 圆角",
        "difficulty": "simple",
        "expected_dims": {"width": 60, "height": 40, "depth": 30},
    },
    {
        "id": "S07",
        "description": "外径 30mm 内径 20mm 高 40mm 的圆管",
        "difficulty": "simple",
        "expected_dims": {"width": 30, "height": 30, "depth": 40},
    },
    {
        "id": "S08",
        "description": "正六边形截面的棱柱，对边距 20mm，高 15mm",
        "difficulty": "simple",
        "expected_dims": {},
    },
    {
        "id": "S09",
        "description": "直径 25mm 的球体",
        "difficulty": "simple",
        "expected_dims": {"width": 25, "height": 25, "depth": 25},
    },
    {
        "id": "S10",
        "description": "120x80x5mm 的平板，中心一个直径 30mm 的通孔",
        "difficulty": "simple",
        "expected_dims": {"width": 120, "height": 80, "depth": 5},
    },
    # === Moderate (7) ===
    {
        "id": "M01",
        "description": "L型支架，两臂各 50mm 长 30mm 宽，厚 5mm，每臂两个 M4 安装孔",
        "difficulty": "moderate",
        "expected_dims": {},
    },
    {
        "id": "M02",
        "description": "法兰盘，外径 80mm 厚 10mm，中心孔 20mm，6 个 M5 螺栓孔均匀分布在 PCD 60mm 上",
        "difficulty": "moderate",
        "expected_dims": {"width": 80, "height": 80, "depth": 10},
    },
    {
        "id": "M03",
        "description": "U型槽钢，总宽 60mm 总高 40mm 长 100mm，壁厚 4mm",
        "difficulty": "moderate",
        "expected_dims": {"width": 60, "height": 100, "depth": 40},
    },
    {
        "id": "M04",
        "description": "三段阶梯轴，直径分别 15/25/10mm，长度分别 20/30/15mm",
        "difficulty": "moderate",
        "expected_dims": {},
    },
    {
        "id": "M05",
        "description": "NEMA 17 电机安装板 50x50x5mm，中心孔 22mm，四角螺栓孔 M3 间距 31mm",
        "difficulty": "moderate",
        "expected_dims": {"width": 50, "height": 50, "depth": 5},
    },
    {
        "id": "M06",
        "description": "带键槽的轴，轴径 20mm 长 80mm，键槽宽 6mm 深 3.5mm 长 30mm",
        "difficulty": "moderate",
        "expected_dims": {},
    },
    {
        "id": "M07",
        "description": "散热板 100x80x3mm，6x8 阵列直径 5mm 通孔，间距 10mm",
        "difficulty": "moderate",
        "expected_dims": {"width": 100, "height": 80, "depth": 3},
    },
    # === Complex (3) ===
    {
        "id": "C01",
        "description": "电子外壳 120x80x35mm，壁厚 2mm，四角 M3 螺丝柱高 8mm，顶面 3 排通风槽",
        "difficulty": "complex",
        "expected_dims": {"width": 120, "height": 80, "depth": 35},
    },
    {
        "id": "C02",
        "description": "轴承座，底座 80x40x10mm 两个 M5 安装孔间距 50mm，圆柱支撑体装 608 轴承 (内径8 外径22 宽7)",
        "difficulty": "complex",
        "expected_dims": {},
    },
    {
        "id": "C03",
        "description": "带法兰的管道弯头，管外径 25mm 内径 20mm，90度弯曲半径 40mm，两端法兰外径 50mm 厚 5mm 4 个 M4 螺栓孔",
        "difficulty": "complex",
        "expected_dims": {},
    },
]
