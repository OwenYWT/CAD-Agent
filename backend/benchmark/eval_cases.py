"""CAD Agent evaluation case set (50 cases).

Design rules (see benchmark plan):
  - FOCUS on 3D-printable consumer parts, with a minority of mechanical parts and
    explicit path-coverage representatives (2D / revolve / sweep / loft / assembly).
  - Cases must NOT overlap the RAG library (backend/examples/*.json, 55 files).
    Run `python -m benchmark.check_overlap` after editing; any case whose top-1
    retriever similarity to an example > 0.5 must be reworded. This keeps the eval
    "open-book with RAG" (reflecting production) WITHOUT being the same item the
    retriever already memorized.
  - Every dimension/scenario here is deliberately different from the examples
    (different sizes, different feature combos, different objects).

Case schema:
  id                  : "P##" print | "M##" mechanical | "X##" path-coverage
  description         : the user prompt (Chinese), what actually gets generated
  difficulty          : "simple" | "moderate" | "complex"
  path                : expected modeling path — for `by_path` aggregation
                        "extrude_cut" | "revolve" | "sweep" | "loft" | "2d" | "assembly"
  expected_part_type  : optional — diagnostic compare vs plan.part_type (does NOT gate)
  expected_dims       : optional — {width,height,depth}; bbox sorted ±tol
  expected_features   : optional — {"holes": N}; parsed from plan.features (diagnostic)
  should_be_printable : 3D printability expectation (default True); omit/False for 2D
  tol                 : dimension tolerance, default 0.10

NOTE: expected_dims is given only where the prompt pins an overall size unambiguously.
Many organic / feature-driven parts leave it empty on purpose (bbox is not meaningful
or not pinned by the prompt) — those cases are judged on executed + printable.
"""

EVAL_CASES: list[dict] = [
    # ============================================================
    # Print — simple (14)
    # ============================================================
    {
        "id": "P01",
        "description": "一个圆柱形桌脚垫，直径 35mm、高 12mm，底面平整贴地",
        "difficulty": "simple", "path": "extrude_cut",
        "expected_dims": {"width": 35, "height": 35, "depth": 12},
    },
    {
        "id": "P02",
        "description": "牙刷架底座，70x45x6mm 的圆角板，表面四个直径 18mm 的浅圆槽放牙刷",
        "difficulty": "simple", "path": "extrude_cut",
        "expected_dims": {"width": 70, "height": 45, "depth": 6},
    },
    {
        "id": "P03",
        "description": "防滑橡胶脚的替代件，正方形 28x28mm 高 9mm，顶部中心一个 M4 沉孔",
        "difficulty": "simple", "path": "extrude_cut",
        "expected_dims": {"width": 28, "height": 28, "depth": 9},
        "expected_features": {"holes": 1},
    },
    {
        "id": "P04",
        "description": "数据线收纳的圆形线扣，外径 22mm，中间一条 6mm 宽的开口卡槽，厚 8mm",
        "difficulty": "simple", "path": "extrude_cut",
    },
    {
        "id": "P05",
        "description": "冰箱贴底板，60x60x4mm 的薄板，背面一个直径 20mm 深 2mm 的圆形磁铁凹槽",
        "difficulty": "simple", "path": "extrude_cut",
        "expected_dims": {"width": 60, "height": 60, "depth": 4},
    },
    {
        "id": "P06",
        "description": "圆锥形漏斗塞，上口直径 30mm，下口直径 18mm，高 25mm，实心",
        "difficulty": "simple", "path": "extrude_cut",
    },
    {
        "id": "P07",
        "description": "桌面理线槽盖片，长 90mm 宽 20mm 厚 3mm，两端各一个 R10 的半圆缺口",
        "difficulty": "simple", "path": "extrude_cut",
        "expected_dims": {"width": 90, "height": 20, "depth": 3},
    },
    {
        "id": "P08",
        "description": "门把手垫圈，外径 48mm 内径 26mm 厚 5mm 的环形件",
        "difficulty": "simple", "path": "extrude_cut",
        "expected_dims": {"width": 48, "height": 48, "depth": 5},
        "expected_features": {"holes": 1},
    },
    {
        "id": "P09",
        "description": "三角形书立支脚，等边三角形边长 40mm，厚 8mm，竖直放置",
        "difficulty": "simple", "path": "extrude_cut",
    },
    {
        "id": "P10",
        "description": "标签夹，55x18x10mm 的长方块，沿长边开一条 2mm 宽 8mm 深的插槽",
        "difficulty": "simple", "path": "extrude_cut",
        "expected_dims": {"width": 55, "height": 18, "depth": 10},
    },
    {
        "id": "P11",
        "description": "圆形按钮帽，直径 16mm 高 10mm，顶部 R3 圆顶，底部一个直径 4mm 深 6mm 的轴孔",
        "difficulty": "simple", "path": "extrude_cut",
        "expected_features": {"holes": 1},
    },
    {
        "id": "P12",
        "description": "六角形杯垫，对边距 95mm，厚 5mm，边缘 R2 圆角",
        "difficulty": "simple", "path": "extrude_cut",
    },
    {
        "id": "P13",
        "description": "笔筒分隔插片，65x65x2mm 十字形隔板，中间开十字槽互插",
        "difficulty": "simple", "path": "extrude_cut",
    },
    {
        "id": "P14",
        "description": "圆角三角形吊牌，最长边 50mm 厚 3mm，顶角一个直径 5mm 的挂绳孔",
        "difficulty": "simple", "path": "extrude_cut",
        "expected_features": {"holes": 1},
    },

    # ============================================================
    # Print — moderate (16)
    # ============================================================
    {
        "id": "P15",
        "description": "手机平放充电支架，底座 75x70mm，背靠板倾斜 65 度高 55mm，底部一个直径 12mm 的过线孔",
        "difficulty": "moderate", "path": "extrude_cut",
        "expected_features": {"holes": 1},
    },
    {
        "id": "P16",
        "description": "桌沿挂耳机的 C 形夹钩，夹口开度 26mm，钩臂厚 6mm，挂钩内径 22mm",
        "difficulty": "moderate", "path": "extrude_cut",
    },
    {
        "id": "P17",
        "description": "磁吸笔筒底座，外径 60mm 高 70mm 壁厚 2.5mm，底部内嵌三个直径 10mm 深 3mm 的磁铁孔",
        "difficulty": "moderate", "path": "extrude_cut",
        "expected_features": {"holes": 3},
    },
    {
        "id": "P18",
        "description": "可堆叠收纳盒，外形 100x70x40mm 壁厚 2mm，顶口外翻 3mm 唇边便于叠放",
        "difficulty": "moderate", "path": "extrude_cut",
        "expected_dims": {"width": 100, "height": 70, "depth": 40},
    },
    {
        "id": "P19",
        "description": "键盘抬升脚，底面 30x25mm，楔形倾角 7 度，最高处 18mm，底部防滑横纹",
        "difficulty": "moderate", "path": "extrude_cut",
    },
    {
        "id": "P20",
        "description": "平板电脑横放支架，整体 120x80mm，前挡边高 15mm，背撑倾角 60 度，重心靠后防倒",
        "difficulty": "moderate", "path": "extrude_cut",
    },
    {
        "id": "P21",
        "description": "牙膏挤压器滚轮夹，主体 40x25x15mm，中间 3mm 宽的扁缝供牙膏管穿过，侧面一个旋钮轴孔直径 6mm",
        "difficulty": "moderate", "path": "extrude_cut",
        "expected_features": {"holes": 1},
    },
    {
        "id": "P22",
        "description": "墙面挂物排钩，背板 120x30x4mm，三个向上 J 形钩等距分布，背板四角 M4 安装孔",
        "difficulty": "moderate", "path": "extrude_cut",
        "expected_features": {"holes": 4},
    },
    {
        "id": "P23",
        "description": "圆形分格调味盒，外径 90mm 高 35mm，内部十字隔成四格，壁厚 2mm",
        "difficulty": "moderate", "path": "extrude_cut",
    },
    {
        "id": "P24",
        "description": "耳机绕线收纳盘，外径 70mm 中心柱直径 20mm，外圈挡边高 10mm，整体盘状",
        "difficulty": "moderate", "path": "extrude_cut",
    },
    {
        "id": "P25",
        "description": "桌面便签夹，底座 50x40x8mm，背部竖起一片 50x35x3mm 的夹板，顶部开 1.5mm 的夹缝",
        "difficulty": "moderate", "path": "extrude_cut",
    },
    {
        "id": "P26",
        "description": "可调节书本阅读架的卡齿支腿，长 110mm 宽 30mm 厚 6mm，一端 5 级棘齿调角",
        "difficulty": "moderate", "path": "extrude_cut",
    },
    {
        "id": "P27",
        "description": "圆顶按压式垃圾袋夹，半球形帽直径 40mm，下方一个开口 18mm 的弹性夹口",
        "difficulty": "moderate", "path": "extrude_cut",
    },
    {
        "id": "P28",
        "description": "USB 集线器固定座，65x30x20mm，顶面一个 62x10mm 的卡槽，两侧 M3 沉头螺丝孔",
        "difficulty": "moderate", "path": "extrude_cut",
        "expected_features": {"holes": 2},
    },
    {
        "id": "P29",
        "description": "牙线盒壁挂支架，背板 50x60mm，前面一个抱住直径 45mm 圆盒的半圆托，背板两个安装孔",
        "difficulty": "moderate", "path": "extrude_cut",
        "expected_features": {"holes": 2},
    },
    {
        "id": "P30",
        "description": "桌面手机+手表二合一托，底座 100x60mm，手机槽倾斜 60 度，旁边一个直径 40mm 的表带凹坑",
        "difficulty": "moderate", "path": "extrude_cut",
    },

    # ============================================================
    # Print — complex (8)
    # ============================================================
    {
        "id": "P31",
        "description": "桌面多功能笔插收纳，120x90x60mm，分三个高低不同的隔仓，外壁 2mm，底部四角圆角 R5",
        "difficulty": "complex", "path": "extrude_cut",
    },
    {
        "id": "P32",
        "description": "Raspberry Pi 风格外壳，95x65x30mm 壁厚 2mm，四角内置 M2.5 螺柱高 6mm，侧面一个 16x8mm 的接口开窗",
        "difficulty": "complex", "path": "extrude_cut",
        "expected_dims": {"width": 95, "height": 65, "depth": 30},
        "expected_features": {"holes": 4},
    },
    {
        "id": "P33",
        "description": "带加强筋的悬臂显示器挂钩，背板 60x80mm，悬臂伸出 70mm，背面三条三角加强筋，背板四个 M4 孔",
        "difficulty": "complex", "path": "extrude_cut",
        "expected_features": {"holes": 4},
    },
    {
        "id": "P34",
        "description": "可堆叠零件盒带分隔，外形 110x80x45mm，内部 2x3 格，前壁有一个 30x20mm 的取件缺口，顶部叠放导轨",
        "difficulty": "complex", "path": "extrude_cut",
    },
    {
        "id": "P35",
        "description": "蜂窝镂空笔筒，外径 80mm 高 95mm 壁厚 3mm，侧壁六边形蜂窝镂空阵列，底部实心",
        "difficulty": "complex", "path": "extrude_cut",
    },
    {
        "id": "P36",
        "description": "带铰链翻盖的小药盒，盒体 60x40x20mm，翻盖一体打印活动铰链，盒身分两格",
        "difficulty": "complex", "path": "extrude_cut",
    },
    {
        "id": "P37",
        "description": "电源适配器壁挂篮，托住 80x50x30mm 的适配器，底托加前挡，背板两个 M4 孔，侧面散热长槽",
        "difficulty": "complex", "path": "extrude_cut",
        "expected_features": {"holes": 2},
    },
    {
        "id": "P38",
        "description": "桌面线缆理线塔，圆柱形外径 50mm 高 120mm，沿高度螺旋开五个出线侧缝，中空走线",
        "difficulty": "complex", "path": "extrude_cut",
    },

    # ============================================================
    # Mechanical (6) — examples 强项，但用不同尺寸/特征避免撞车
    # ============================================================
    {
        "id": "M01",
        "description": "方形法兰盘，外形 70x70mm 厚 8mm，中心孔直径 30mm，四角各一个 M6 螺栓孔边距 10mm",
        "difficulty": "moderate", "path": "extrude_cut",
        "expected_dims": {"width": 70, "height": 70, "depth": 8},
        "expected_features": {"holes": 5},
    },
    {
        "id": "M02",
        "description": "两端不等径的双台阶轴，左段直径 12mm 长 25mm，右段直径 18mm 长 35mm，同轴",
        "difficulty": "moderate", "path": "extrude_cut",
    },
    {
        "id": "M03",
        "description": "T 形连接支架，竖板 60x40x6mm 与横板 60x30x6mm 垂直相交，交接处 R4 圆角，每板两个 M5 孔",
        "difficulty": "moderate", "path": "extrude_cut",
        "expected_features": {"holes": 4},
    },
    {
        "id": "M04",
        "description": "带凸缘的衬套，外径 24mm 主体长 30mm，一端凸缘外径 34mm 厚 4mm，通孔直径 12mm",
        "difficulty": "moderate", "path": "extrude_cut",
        "expected_features": {"holes": 1},
    },
    {
        "id": "M05",
        "description": "直齿轮简化件，分度圆约 48mm，16 齿，厚 10mm，中心轴孔直径 10mm 带 3mm 键槽",
        "difficulty": "complex", "path": "extrude_cut",
        "expected_features": {"holes": 1},
    },
    {
        "id": "M06",
        "description": "滑轨滑块，35x35x20mm，底面一条 12mm 宽 6mm 深的燕尾槽，顶部一个 M5 螺纹孔",
        "difficulty": "moderate", "path": "extrude_cut",
        "expected_features": {"holes": 1},
    },

    # ============================================================
    # Path coverage (6) — 显式覆盖非 extrude_cut 路径
    # ============================================================
    {
        "id": "X01",
        "description": "画一个法兰垫片的 2D 激光切割轮廓 DXF，外径 60mm，中心孔 25mm，六个直径 5mm 的螺栓孔在 PCD 45mm 上",
        "difficulty": "moderate", "path": "2d",
        "expected_part_type": "profile_2d",
        # 2D: no printability; judged on executed + dxf/svg output produced
    },
    {
        "id": "X02",
        "description": "输出一个齿形垫片的 2D 轮廓 DXF 用于激光切割，外径 40mm，内孔 20mm，外缘 12 个矩形齿",
        "difficulty": "moderate", "path": "2d",
        "expected_part_type": "profile_2d",
    },
    {
        "id": "X03",
        "description": "一个圆锥形灯罩，下口直径 120mm，上口直径 60mm，高 90mm，壁厚 2mm 的回转薄壁体",
        "difficulty": "moderate", "path": "revolve",
        "expected_part_type": "revolution",
    },
    {
        "id": "X04",
        "description": "一个 90 度弯管，管外径 28mm 内径 22mm，弯曲半径 45mm，两端各有 20mm 直管段",
        "difficulty": "complex", "path": "sweep",
        "expected_part_type": "swept",
    },
    {
        "id": "X05",
        "description": "一个方变圆的过渡接头，底部 60x60mm 方口，顶部直径 40mm 圆口，高 50mm，壁厚 2mm",
        "difficulty": "complex", "path": "loft",
        "expected_part_type": "organic",
    },
    {
        "id": "X06",
        "description": "一个简单的桌面支架装配体：方形底座 80x80x10mm 加一根直径 20mm 高 100mm 的立柱，立柱顶端一个 60x40x8mm 的平台",
        "difficulty": "complex", "path": "assembly",
        "expected_part_type": "assembly",
        "should_be_printable": False,  # assembly path may not produce a single watertight solid
    },
]


# Convenience: id -> case
EVAL_CASES_BY_ID = {c["id"]: c for c in EVAL_CASES}


def case_set_hash() -> str:
    """Stable hash of the case set (ids + descriptions), for report metadata.
    A changed hash means the baseline is no longer comparable apples-to-apples."""
    import hashlib
    blob = "\n".join(f"{c['id']}|{c['description']}" for c in EVAL_CASES)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
