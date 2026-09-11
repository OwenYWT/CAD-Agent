import type { SketchConstraint, SketchGeometry, SketchDimensionType } from "../types/sketch";

export const dimensionTypes = new Set<string>(["DistanceX","DistanceY","Distance","Radius","Diameter"]);
export const dimensionLabels: Record<SketchDimensionType,string> = {DistanceX:"X 距离",DistanceY:"Y 距离",Distance:"长度",Radius:"半径",Diameter:"直径"};
export function editableDimension(c: SketchConstraint) { return dimensionTypes.has(c.type) && c.driving !== false; }
export function validDimension(c: SketchConstraint, value: number) {
  return Number.isFinite(value) && Math.abs(value)<=1_000_000 &&
    (["DistanceX","DistanceY"].includes(c.type) || value>0);
}
export function previewSketch(geometry: SketchGeometry[], constraints: SketchConstraint[], values: Record<number,string>): SketchGeometry[] {
  const result=structuredClone(geometry);
  for (const constraint of constraints) {
    const raw=values[constraint.index];
    if (!editableDimension(constraint) || raw===undefined || !raw.trim() || !validDimension(constraint,Number(raw))) continue;
    const item=result.find(g=>g.index===constraint.first), value=Number(raw);
    if (!item) continue;
    if (item.type==="Part::GeomCircle" && ["Radius","Diameter"].includes(constraint.type)) {
      item.radius_mm=constraint.type==="Diameter" ? value/2 : value;
    } else if (["DistanceX","DistanceY"].includes(constraint.type) && constraint.second===-2000) {
      const point=constraint.first_position===3 && item.type==="Part::GeomCircle" ? item.center
        : constraint.first_position===1 ? item.start : constraint.first_position===2 ? item.end : undefined;
      if (point) point[constraint.type==="DistanceX" ? 0 : 1]=value;
    } else if (constraint.type==="Distance" && item.type==="Part::GeomLineSegment" && item.start && item.end) {
      const dx=item.end[0]-item.start[0],dy=item.end[1]-item.start[1],length=Math.hypot(dx,dy);
      if (length>1e-9) item.end=[item.start[0]+dx*value/length,item.start[1]+dy*value/length,0];
    }
  }
  return result;
}

export function sketchBounds(geometry: SketchGeometry[]): [number,number,number,number] {
  const points:number[][]=[];
  for (const item of geometry) {
    if (item.type==="Part::GeomCircle" && item.center && item.radius_mm) {
      const [x,y]=item.center,r=item.radius_mm;points.push([x-r,y-r],[x+r,y+r]);
    } else if (item.start && item.end) points.push(item.start,item.end);
  }
  if (!points.length) return [-10,-10,20,20];
  const x=Math.min(...points.map(p=>p[0])),y=Math.min(...points.map(p=>p[1]));
  const width=Math.max(10,Math.max(...points.map(p=>p[0]))-x),height=Math.max(10,Math.max(...points.map(p=>p[1]))-y);
  const pad=Math.max(width,height)*0.5;
  return [x-pad,-y-height-pad,width+2*pad,height+2*pad];
}
