import test from 'node:test';
import assert from 'node:assert/strict';
import { engineeringMeasurements } from '../src/adapters/engineeringMeasurements.ts';

test('display joins actual evidence to its own criteria by ID, retaining failures and zero', () => {
  const rows=engineeringMeasurements({acceptance_contract:{checks:[
    {check_id:'depth',kind:'hole_depth',description:'孔深',nominal:10},
    {check_id:'holes',kind:'hole_count',description:'孔数',nominal:0}]},
  acceptance:{evidence:[{check_id:'holes',outcome:'passed',measured:[0],method:'count'},
    {check_id:'depth',outcome:'failed',measured:[8],method:'shaft_length'}]}});
  assert.equal(rows[0].expected,'0');assert.equal(rows[0].measured,'0');
  assert.equal(rows[1].description,'孔深');assert.equal(rows[1].expected,'10 mm');
  assert.equal(rows[1].measured,'8 mm');assert.equal(rows[1].outcome,'未通过');
});

test('old reports and unknown evidence never invent criteria or a passing state', () => {
  assert.deepEqual(engineeringMeasurements({}),[]);
  const [row]=engineeringMeasurements({acceptance:{evidence:[{check_id:'old',measured:[]}]}});
  assert.equal(row.expected,'未记录');assert.equal(row.measured,'无量测值');assert.equal(row.outcome,'未验证');
});


test('hole-position evidence identifies deviation, not measured coordinates', () => {
  const [row]=engineeringMeasurements({acceptance_contract:{checks:[{check_id:'p',kind:'hole_position',
    scope:{centers_mm:[[4,5,0]]},tolerance_mm:0.01}]},acceptance:{evidence:[{check_id:'p',measured:[0.005],outcome:'passed'}]}});
  assert.equal(row.measuredLabel,'孔轴位置偏差');assert.equal(row.expected,'[[4,5,0]] mm');
  assert.equal(row.measured,'0.005 mm');assert.equal(row.tolerance,0.01);
});

test('v2 comparisons display bounds and asymmetric authorized tolerance', () => {
  const rows = engineeringMeasurements({acceptance_contract:{schema_version:'engineering-acceptance.v2',checks:[
    {check_id:'min',kind:'wall_thickness',comparison:{op:'ge',lower:5,unit:'mm'}},
    {check_id:'max',kind:'planar_gap',comparison:{op:'le',upper:5,unit:'mm'}},
    {check_id:'range',kind:'planar_gap',comparison:{op:'range',lower:4,upper:6,unit:'mm'}},
    {check_id:'eq',kind:'planar_gap',comparison:{op:'eq',value:25,unit:'mm',tolerance_minus:0.1,tolerance_plus:0.2}},
  ]},acceptance:{evidence:['min','max','range','eq'].map(check_id=>({check_id,measured:[5],outcome:'failed'}))}});
  assert.deepEqual(rows.map(r=>r.expected), ['≥ 5 mm','≤ 5 mm','[4, 6] mm','25 mm']);
  assert.equal(rows[3].tolerance,'−0.1 / +0.2');
});

test('through-hole evidence displays the geometric predicate without length units or inferred success', () => {
  const report = {acceptance_contract:{schema_version:'engineering-acceptance.v2', checks:[
    {check_id:'through',kind:'hole_through',comparison:{op:'eq',value:1,unit:'count',tolerance_minus:0,tolerance_plus:0}},
  ]},acceptance:{evidence:[{check_id:'through',measured:[1,0],outcome:'failed'}]}};
  const [row] = engineeringMeasurements(report);
  assert.equal(row.expected,'贯通');assert.equal(row.measured,'贯通、未贯通');
  assert.equal(row.outcome,'未通过');assert.equal(row.tolerance,undefined);
  report.acceptance.evidence[0].measured=[];
  report.acceptance.evidence[0].outcome='indeterminate';
  assert.equal(engineeringMeasurements(report)[0].outcome,'未验证');
  assert.equal(engineeringMeasurements(report)[0].measured,'无量测值');
});
