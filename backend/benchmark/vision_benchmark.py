"""Controlled benchmark of the vision verify-and-correct capability.

Geometries are built DETERMINISTICALLY with CadQuery so the numbers isolate the
vision check itself (no LLM code-generation variance); multimodal calls are spent
only on the actual judgments/fixes. Two experiments:

  EXP 1 — DETECTION: 8 (description, geometry) pairs — 4 correct, 4 deliberately
  wrong (wrong shape / missing feature / detached part). Measures how reliably the
  vision check flags mismatches. The pre-vision pipeline shipped all of these as
  `success` because it only checked "executed + watertight", never shape-vs-request.

  EXP 2 — CORRECTION: start from flawed CadQuery, run the REAL loop
  (render -> vision -> fix_visual_issues -> re-execute -> render -> vision) and see
  whether the model's own critique repairs the geometry into a match.

Run (needs a working multimodal VISION_MODEL and cadquery importable):
    cd backend && python -m benchmark.vision_benchmark

Caveat: the evaluator is the same multimodal model that drives correction and N is
small, so this measures shape-match reliability, not absolute ground truth.
"""
import asyncio
import json
import tempfile
import time
from pathlib import Path

import cadquery as cq

from app.rendering.renderer import CADRenderer
from app.validation.vision_validator import VisionValidator
from app.sandbox.executor import CadQueryExecutor
from app.agent.code_gen import CodeGenerator

WORK = Path(tempfile.mkdtemp(prefix="vision_bench_"))
renderer = CADRenderer()
vision = VisionValidator()


def export(obj, name: str) -> Path:
    p = WORK / f"{name}.stl"
    cq.exporters.export(obj, str(p), exportType="STL")
    return p


# ---- deterministic geometries ----
def g_box():          return cq.Workplane("XY").box(40, 30, 20)
def g_cyl():          return cq.Workplane("XY").circle(15).extrude(40)
def g_box_hole():     return cq.Workplane("XY").box(40, 40, 40).faces(">Z").workplane().hole(12)
def g_shell():        return cq.Workplane("XY").circle(20).extrude(40).faces(">Z").shell(-3)
def g_box_plain40():  return cq.Workplane("XY").box(40, 40, 40)
def g_box_detached():
    base = cq.Workplane("XY").box(40, 30, 20)
    boss = cq.Workplane("XY").transformed(offset=(0, 0, 30)).box(8, 8, 8)  # floats above
    return cq.Workplane("XY").add(base).add(boss)


# (description, geometry-builder, expected_match)
EXP1 = [
    ("一个 40×30×20mm 的长方体",                              g_box,          True),
    ("一个直径 30mm、高 40mm 的圆柱",                          g_cyl,          True),
    ("一个 40mm 立方体，中心有一个直径 12mm 的竖直通孔",       g_box_hole,     True),
    ("一个圆柱形外壳：外径 40mm、高 40mm、壁厚 3mm、顶部开口",  g_shell,        True),
    ("一个直径 30mm、高 40mm 的圆柱",                          g_box,          False),  # wrong shape
    ("一个 40mm 立方体，中心有一个直径 12mm 的竖直通孔",       g_box_plain40,  False),  # missing hole
    ("一个 L 形支架（两条互相垂直的臂）",                     g_box,          False),  # wrong shape
    ("一个盒子，顶面有一个与盒体相连的凸台",                   g_box_detached, False),  # detached part
]

EXP2 = [
    {
        "desc": "一个 40mm 立方体，中心有一个直径 12mm 的竖直通孔",
        "code": "result = cq.Workplane('XY').box(40, 40, 40)\nshow_object(result)",  # hole missing
    },
    {
        "desc": "一个 40×30×20mm 的盒子，顶面中心有一个直径 8mm、高 10mm 的圆柱凸台，凸台与盒体相连",
        "code": (
            "base = cq.Workplane('XY').box(40, 30, 20)\n"
            "boss = cq.Workplane('XY').transformed(offset=(0,0,25)).circle(4).extrude(10)\n"  # floats
            "result = base.union(boss)\nshow_object(result)"
        ),
    },
]


async def judge(desc, stl_path, code=""):
    rp = renderer.render_stl(stl_path, stl_path.parent / (stl_path.stem + "_r"))
    if not rp:
        return None, 0.0, ["no renders"]
    v = await vision.validate(desc, rp, code)
    return v.is_match, v.confidence, v.issues[:2]


async def run_exp1():
    rows = []
    for i, (desc, builder, expected) in enumerate(EXP1, 1):
        stl = export(builder(), f"e1_{i}")
        t = time.time()
        m, conf, issues = await judge(desc, stl)
        flagged = (m is not True)
        correct = (flagged != expected)
        rows.append({"case": i, "desc": desc[:34], "expected_match": expected,
                     "is_match": m, "confidence": conf, "correct": correct,
                     "issues": issues, "s": round(time.time() - t, 1)})
        print(f"  E1.{i} expect_match={expected} is_match={m} correct={correct} ({rows[-1]['s']}s)", flush=True)
    return rows


async def run_exp2():
    import shutil
    execu, cg = CadQueryExecutor(), CodeGenerator()
    rows = []
    for i, case in enumerate(EXP2, 1):
        desc, code = case["desc"], case["code"]
        res = await execu.execute(code)
        if not res.success:
            rows.append({"case": i, "desc": desc[:34], "note": f"initial exec failed: {res.error_type}"})
            continue
        stl0 = next((p for n, p in res.files.items() if str(p).endswith(".stl")), None)
        m0, _, iss0 = await judge(desc, Path(stl0), code)
        shutil.rmtree(res.work_dir, ignore_errors=True)

        final_match, rounds, cur_code, last_issues = m0, 0, code, iss0
        while final_match is not True and rounds < 2:
            rounds += 1
            cur_code = await cg.fix_visual_issues(cur_code, list(last_issues), [])
            res = await execu.execute(cur_code)
            if not res.success:
                last_issues = [f"exec failed: {res.error_type}"]
                break
            stl = next((p for n, p in res.files.items() if str(p).endswith(".stl")), None)
            final_match, _, last_issues = await judge(desc, Path(stl), cur_code)
            shutil.rmtree(res.work_dir, ignore_errors=True)

        rows.append({"case": i, "desc": desc[:34], "before_match": m0, "before_issues": iss0,
                     "after_match": final_match, "fix_rounds": rounds,
                     "corrected": (m0 is not True and final_match is True)})
        print(f"  E2.{i} before_match={m0} -> after_match={final_match} rounds={rounds} "
              f"corrected={rows[-1]['corrected']}", flush=True)
    return rows


async def main():
    print("===== EXP 1: DETECTION =====", flush=True)
    e1 = await run_exp1()
    print("\n===== EXP 2: CORRECTION =====", flush=True)
    e2 = await run_exp2()

    mism = [r for r in e1 if not r["expected_match"]]
    matched = [r for r in e1 if r["expected_match"]]
    caught = sum(1 for r in mism if r["is_match"] is not True)
    false_alarm = sum(1 for r in matched if r["is_match"] is not True)
    e2v = [r for r in e2 if "before_match" in r]
    summary = {
        "exp1_detection": {
            "n": len(e1), "accuracy_correct": sum(1 for r in e1 if r["correct"]),
            "mismatches_total": len(mism), "mismatches_caught": caught,
            "matches_total": len(matched), "false_alarms": false_alarm,
            "detection_rate_pct": round(100 * caught / len(mism), 1) if mism else None,
            "specificity_pct": round(100 * (len(matched) - false_alarm) / len(matched), 1) if matched else None,
        },
        "exp2_correction": {
            "cases": len(e2v),
            "initially_mismatched": sum(1 for r in e2v if r["before_match"] is not True),
            "corrected_by_loop": sum(1 for r in e2v if r.get("corrected")),
        },
    }
    print("\n========== SUMMARY ==========")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    out = WORK / "results.json"
    out.write_text(json.dumps({"summary": summary, "exp1": e1, "exp2": e2}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
