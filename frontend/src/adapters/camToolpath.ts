import type { CamToolpath } from '../types/engineeringTask';

export function validateCamToolpath(field:CamToolpath):CamToolpath {
  const vector=(v:number[])=>Array.isArray(v) && v.length===3 && v.every(Number.isFinite);
  if (field.schema_version!=='cad-cam-toolpath.v1' || field.units!=='mm' || field.postprocessor!=='grbl_1_1'
    || !vector(field.work_origin_mm) || !Array.isArray(field.positions_mm) || !field.positions_mm.length || field.positions_mm.length>100000
    || !field.positions_mm.every(vector) || !Array.isArray(field.triangles) || !field.triangles.length || field.triangles.length>200000
    || !field.triangles.every(t=>Array.isArray(t) && t.length===3 && t.every(i=>Number.isInteger(i) && i>=0 && i<field.positions_mm.length))
    || !Array.isArray(field.trajectory) || field.trajectory.length<2 || field.trajectory.length>200000
    || !field.trajectory.every(t=>['rapid','feed'].includes(t.motion) && vector(t.position_mm)
      && (t.motion==='rapid' ? t.feed_mm_min===null : typeof t.feed_mm_min==='number' && Number.isFinite(t.feed_mm_min) && t.feed_mm_min>0))
    || !Number.isFinite(field.safe_z_mm) || !Number.isFinite(field.cutter_diameter_mm) || field.cutter_diameter_mm<=0) {
    throw new Error('加工路径不完整、坐标无效或超过显示预算');
  }
  return field;
}
