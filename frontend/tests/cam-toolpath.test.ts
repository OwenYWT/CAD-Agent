import test from 'node:test';
import assert from 'node:assert/strict';
import { validateCamToolpath } from '../src/adapters/camToolpath.ts';
import type { CamToolpath } from '../src/types/engineeringTask.ts';

const path:CamToolpath={schema_version:'cad-cam-toolpath.v1',units:'mm',postprocessor:'grbl_1_1',work_origin_mm:[0,0,0],
  positions_mm:[[0,0,0],[1,0,0],[0,1,0]],triangles:[[0,1,2]],safe_z_mm:5,cutter_diameter_mm:6,
  trajectory:[{motion:'rapid',position_mm:[2,0,5],feed_mm_min:null},{motion:'feed',position_mm:[2,0,0],feed_mm_min:100}]};
test('CAM preview requires finite matching geometry and meaningful motion modes',()=>{
  assert.equal(validateCamToolpath(path),path);
  for (const invalid of [{...path,positions_mm:[]},{...path,triangles:[[0,1,3]]},
    {...path,trajectory:[path.trajectory[0],{...path.trajectory[1],feed_mm_min:0}]},
    {...path,cutter_diameter_mm:-1},{...path,safe_z_mm:Infinity}]) {
    assert.throws(()=>validateCamToolpath(invalid));
  }
});
