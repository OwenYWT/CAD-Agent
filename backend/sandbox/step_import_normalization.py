"""Bounded correction of STEP transfer's underestimated edge precision.

OCCT's SameParameter transfer uses sampled deviations. BOP validation uses a
maximum-distance solver and can find larger deviations on the *same* curves.
Only precision metadata is corrected here: no curve/surface reconstruction,
sewing, refinement, topology replacement, or acceptance-tolerance changes.
The product Python process measures a serialized BREP; FreeCAD owns mutation
and the final full BOP check. Neither process opens the other kernel's ABI.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


# Millimetres. This import policy permits at most one micron of representation
# uncertainty and at most 2x the existing edge precision (with a 10 nm floor).
# It does not authorize geometric movement or change any design requirement.
MAX_PRECISION_MM = 1e-3
TRANSFER_FLOOR_MM = 1e-5
SERIALIZATION_MARGIN_MM = 1e-8
CURVE_ERROR = 'BOPAlgo_InvalidCurveOnSurface'


class StepNormalizationError(RuntimeError):
    def __init__(self, message, *, code='native_import_precision_unsupported', details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def geometry_facts(shape):
    box = shape.optimalBoundingBox(False, False)
    facts = {'solids': len(shape.Solids), 'shells': len(shape.Shells),
        'faces': len(shape.Faces), 'wires': len(shape.Wires),
        'edges': len(shape.Edges), 'vertices': len(shape.Vertexes),
        'volume_mm3': float(shape.Volume), 'area_mm2': float(shape.Area),
        'bounds_mm': [float(getattr(box, axis+bound))
                      for axis in ('X', 'Y', 'Z') for bound in ('Min', 'Max')]}
    if not all(math.isfinite(value) for value in
               [facts['volume_mm3'], facts['area_mm2'], *facts['bounds_mm']]):
        raise StepNormalizationError('STEP 几何度量包含非有限值')
    return facts


def _measure(path):
    """Read-only exact OCCT diagnostics, run in the existing product runtime."""
    from OCP import __version__
    from OCP.BOPAlgo import BOPAlgo_ArgumentAnalyzer, BOPAlgo_CheckStatus
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS, TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedMapOfShape

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(path), BRep_Builder()) or shape.IsNull():
        raise ValueError('cannot read diagnostic BREP')
    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, edges)
    analyzer = BOPAlgo_ArgumentAnalyzer()
    analyzer.SetShape1(shape)
    analyzer.CurveOnSurfaceMode = True
    analyzer.Perform()
    rows = {}
    for result in analyzer.GetCheckResult():
        if result.GetCheckStatus() != BOPAlgo_CheckStatus.BOPAlgo_InvalidCurveOnSurface:
            raise ValueError('unexpected diagnostic status')
        faulty = [TopoDS.Edge_s(item) for item in result.GetFaultyShapes1()
                  if item.ShapeType() == TopAbs_EDGE]
        if len(faulty) != 1:
            raise ValueError('ambiguous faulty edge')
        edge = faulty[0]
        index = edges.FindIndex(edge)
        deviation = float(result.GetMaxDistance1())
        previous = rows.get(index, {}).get('deviation_mm', 0.0)
        rows[index] = {'edge_index': index,
            'before_mm': float(BRep_Tool.Tolerance_s(edge)),
            'deviation_mm': max(previous, deviation)}
    return {'brep_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'edge_count': edges.Extent(), 'measurement_kernel': 'OCP '+__version__,
        'edges': [rows[key] for key in sorted(rows)]}


def normalize_step_shape(shape):
    """Return (shape, evidence); valid inputs are returned completely unchanged.

    Any other BOP diagnostic, excessive discrepancy, unavailable measurement or
    unsuccessful strict recheck fails closed. Ordinary native modeling never
    calls this helper.
    """
    try:
        errors = shape.check(True)
    except ValueError as error:
        errors = str(error)
    if not errors:
        return shape, None
    # Do not treat unrelated validity problems as precision metadata defects.
    lines = str(errors).strip().splitlines()
    if (not shape.isValid() or not shape.Solids
            or any(not re.fullmatch(r'Error in (Edge|Face): '+CURVE_ERROR, line)
                   for line in lines if line.strip() and line != 'BOP check found the following errors:')
            or CURVE_ERROR not in str(errors)):
        raise StepNormalizationError('STEP 含不能安全自动修复的几何错误，请在源 CAD 中修复后重新导入',
                                     code='native_import_geometry_invalid')
    with tempfile.TemporaryDirectory(prefix='step-precision-') as directory:
        source = Path(directory)/'source.brep'
        shape.exportBrep(str(source))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        try:
            completed = subprocess.run(['/usr/local/bin/python', str(Path(__file__).resolve()), str(source)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=30, check=True)
            measured = json.loads(completed.stdout)
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            raise StepNormalizationError('STEP 曲线精度测量失败，未修改输入几何',
                code='native_import_diagnostic_failed') from error
    if (measured['brep_sha256'] != digest or measured['edge_count'] != len(shape.Edges)
            or not measured['edges']):
        raise StepNormalizationError('STEP 曲线精度证据与原生几何不匹配')
    changes = []
    for row in measured['edges']:
        index = row['edge_index']
        if type(index) is not int or not 1 <= index <= len(shape.Edges):
            raise StepNormalizationError('STEP 曲线精度证据包含无效边')
        before = float(shape.Edges[index-1].Tolerance)
        deviation = row['deviation_mm']
        bound = min(MAX_PRECISION_MM, max(TRANSFER_FLOOR_MM, before*2))
        after = deviation+SERIALIZATION_MARGIN_MM
        if (not math.isfinite(deviation) or deviation <= 0
                or not math.isclose(before, row['before_mm'], rel_tol=1e-9, abs_tol=1e-15)
                or after > bound):
            raise StepNormalizationError('STEP 曲线偏差超出安全导入精度范围，请在源 CAD 中修复后重新导入',
                details={'edge_index': index, 'deviation_mm': deviation, 'limit_mm': bound})
        changes.append({**row, 'after_mm': max(before, after), 'limit_mm': bound})
    before_facts = geometry_facts(shape)
    repaired = shape.copy()
    for row in changes:
        edge = repaired.Edges[row['edge_index']-1]
        edge.limitTolerance(row['after_mm'])
        # Keep OCCT's vertex >= incident edge precision invariant.
        for vertex in edge.Vertexes:
            vertex.limitTolerance(row['after_mm'])
    after_facts = geometry_facts(repaired)
    if before_facts != after_facts:
        raise StepNormalizationError('STEP 精度校正改变了几何，已拒绝导入')
    try:
        errors = repaired.check(True)
    except ValueError as error:
        raise StepNormalizationError('STEP 精度校正后仍未通过严格几何检查') from error
    if errors:
        raise StepNormalizationError('STEP 精度校正后仍未通过严格几何检查')
    return repaired, {'schema_version': 'step-import-normalization.v1',
        'method': 'measured_edge_precision_metadata', 'diagnostic': CURVE_ERROR,
        'brep_sha256': digest, 'measurement_kernel': measured['measurement_kernel'],
        'max_precision_mm': MAX_PRECISION_MM, 'changed_edges': changes,
        'geometry_before': before_facts, 'geometry_after': after_facts,
        'strict_check_passed': True}


def verify_saved_import(path):
    """Read the actual saved bytes in a different document, without healing."""
    import FreeCAD as App
    with tempfile.TemporaryDirectory(prefix='step-import-readback-') as directory:
        copy = Path(directory)/'saved.FCStd'
        shutil.copyfile(path, copy)
        restored = App.openDocument(str(copy))
        try:
            shape = restored.getObject('ImportedBase').Shape
            if not shape.isValid() or not shape.Solids or shape.check(True):
                raise StepNormalizationError('保存后的 STEP 导入模型未通过严格几何检查')
        except (AttributeError, ValueError) as error:
            raise StepNormalizationError('保存后的 STEP 导入模型未通过严格几何检查') from error
        finally:
            App.closeDocument(restored.Name)


if __name__ == '__main__':
    import sys
    print(json.dumps(_measure(Path(sys.argv[1])), allow_nan=False))
