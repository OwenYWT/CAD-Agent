import assert from 'node:assert/strict';
import test from 'node:test';
import {previewSketch,validDimension,editableDimension} from '../src/adapters/sketchPreview.ts';
import type {SketchConstraint,SketchGeometry} from '../src/types/sketch.ts';

const circle:SketchGeometry={index:0,type:'Part::GeomCircle',center:[5,5,0],radius_mm:5};
const constraint=(index:number,type:string):SketchConstraint=>({index,type,name:'',value:5,first:0,first_position:type==='Radius' ? 0 : 3,second:-2000,second_position:0,driving:true});

test('dimension drafts modify local geometry without mutating the committed checkpoint',()=>{
  const result=previewSketch([circle],[constraint(0,'DistanceX'),constraint(1,'DistanceY'),constraint(2,'Radius')],{0:'0',1:'-3',2:'7'});
  assert.deepEqual(result[0].center,[0,-3,0]);assert.equal(result[0].radius_mm,7);
  assert.deepEqual(circle.center,[5,5,0]);assert.equal(circle.radius_mm,5);
});

test('diameter drafts have correct radius; malformed dimensions leave the preview unchanged',()=>{
  assert.equal(previewSketch([circle],[constraint(0,'Diameter')],{0:'14'})[0].radius_mm,7);
  for(const value of ['','-1','NaN','Infinity']) assert.equal(previewSketch([circle],[constraint(0,'Radius')],{0:value})[0].radius_mm,5);
  assert.equal(validDimension(constraint(0,'DistanceX'),0),true);
  assert.equal(editableDimension({...constraint(0,'Radius'),driving:false}),false);
});

test('linked two-geometry dimensions are not misrepresented as absolute positions',()=>{
  const linked={...constraint(0,'DistanceX'),second:1,second_position:3};
  assert.deepEqual(previewSketch([circle],[linked],{0:'12'})[0].center,[5,5,0]);
});

test('angle edits display degrees and never submit angular values as millimetres', async()=>{
  const {displayDimension,dimensionEdit}=await import('../src/adapters/sketchPreview.ts');
  const angle={...constraint(4,'Angle'),value:Math.PI/3};
  assert.ok(Math.abs(displayDimension(angle)-60)<1e-10);
  assert.equal(editableDimension(angle),true);
  assert.deepEqual(dimensionEdit(angle,75),{constraint_index:4,expected_type:'Angle',value_deg:75});
});
