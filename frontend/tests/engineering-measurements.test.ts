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
