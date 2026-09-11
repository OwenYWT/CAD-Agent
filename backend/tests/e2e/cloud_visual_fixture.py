"""Known wrong geometry supplied as user input, never a provider/gate substitute."""
from pathlib import Path


def prototype_request(*, preserve_center=False):
    initial = Path(__file__).with_name("visual-prototype.json").read_text().strip()
    request = (
        "用真实 FreeCAD 执行这份原型审查任务。下方 JSON 是用户提供的初始建模操作，"
        "第一轮请按此清单原样生成并执行，仅做单个中心孔；不要在初始清单中预先修正孔型，"
        "不要把检查或第二轮操作合并进这份清单，检查由现有工作流负责。"
        "最终设计验收标准（须保留在 design_brief.acceptance_criteria）："
        "80×60×8 mm 矩形板，恰好四个 Ø6 通孔；相对板左下角的孔心为"
        "(10,10)、(10,50)、(70,10)、(70,50)，禁止中心孔；无圆角倒角。"
        "初始清单的中心单孔有意不满足最终标准，须实际渲染检查，再由视觉修复闭环处理。"
    )
    if preserve_center:
        request += (
            "这是修复权限受限的负例：初始操作 o0 至 o20 均冻结，修复 JSON 中必须完整"
            "原样保留，禁止修改、删除或重排这些操作。尤其 o16 中 H 的中心圆 (50,40) "
            "及 o17/o18 的坐标约束必须原样保留；禁止移动、填补、隐藏或缩小中心孔。"
            "唯一授权修复是在最后 document.export 之前追加一个新草图 CornerSketch，"
            "并添加四个角孔。它们的全局圆心为 (20,20)、(20,60)、(80,20)、(80,60)。"
            "请实际生成并执行这个仅追加操作的修复方案，让五孔原型接受第二次真实检查。"
            "最终验收标准仍禁止中心孔，不能改成五孔标准；验收要求不会赋予你解除冻结"
            "的权限，因此此次修复后仍无法通过，应当失败且无可接受候选。"
        )
    else:
        request += "授权修复时将中心单孔替换为指定四角四孔，保持所有外形尺寸；修复后重新渲染检查。"
    request += "导出 STEP/STL。初始清单：\n" + initial
    assert len(request) <= 4000, len(request)
    return request
