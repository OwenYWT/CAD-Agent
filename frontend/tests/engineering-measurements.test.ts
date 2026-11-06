import test from 'node:test';
import assert from 'node:assert/strict';
import { engineeringMeasurements, engineeringSourceContext, engineeringVerificationNotices } from '../src/adapters/engineeringMeasurements.ts';

test('physical verification notices remain visible after a geometric pass without inventing certification', () => {
  const notice = {schema_version:'verification-notice.v1', category:'manufacturing_process', status:'unverified',
    source_refs:[{source_quote:'FDM PLA打印'}], message:'实际打印性能未验证。'};
  const report = {acceptance_contract:{verification_notices:[notice]}, acceptance:{outcome:'passed'}};
  assert.deepEqual(engineeringVerificationNotices(report), [{message:'实际打印性能未验证。', references:['FDM PLA打印']}]);
  assert.deepEqual(engineeringVerificationNotices({}), []);
  assert.deepEqual(engineeringVerificationNotices({acceptance_contract:{verification_notices:[{...notice,status:'passed'}]}}), []);
});

test('external inputs and unverified physical fit survive a passing geometry report', () => {
  const report = {acceptance_contract:{source_context:{schema_version:'source-context.v1', execution_scope:'concept_geometry',
    external_references:[{source_quote:'夹在 25 mm 桌板',property:'plate_thickness'}],
    unverified_aspects:['physical_fit','clamping_force']}},acceptance:{outcome:'passed'}};
  const context = engineeringSourceContext(report);
  assert.deepEqual(context?.references, ['夹在 25 mm 桌板']);
  assert.deepEqual(context?.unverified, ['实物适配','夹持力']);
  assert.equal(context?.scope, '概念几何');
  assert.equal(engineeringSourceContext({}), null);
});

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

test('group display uses the recorded complete partition and labels the whole inventory', () => {
  const report = {acceptance_contract:{schema_version:'engineering-acceptance.v2',checks:[
    {check_id:'count',kind:'hole_count',hole_group:{group_id:'a'},comparison:{op:'eq',value:2,unit:'count'}},
    {check_id:'diameter',kind:'hole_diameter',hole_group:{group_id:'a'},comparison:{op:'eq',value:6,unit:'mm'}},
  ]},acceptance:{evidence:[
    {check_id:'count',measured:[4],outcome:'passed',details:{population_evaluation:{outcome:'passed',feature_count:4,assignment:{a:[0,2],b:[1,3]}}}},
    {check_id:'diameter',measured:[6,4,6,4],outcome:'passed',details:{population_evaluation:{outcome:'passed',feature_count:4,assignment:{a:[0,2],b:[1,3]}}}},
  ]}};
  const rows = engineeringMeasurements(report);
  assert.equal(rows[0].measured,'2'); assert.equal(rows[1].measured,'6、6 mm');
  assert.equal(rows[0].measuredLabel,'本组实测（全范围 4 孔）');
  report.acceptance.evidence[1].outcome='failed';
  report.acceptance.evidence[1].details.population_evaluation.outcome='failed';
  const failed = engineeringMeasurements(report)[1];
  assert.equal(failed.measured,'6、4、6、4 mm');
  assert.equal(failed.measuredLabel,'全范围实测（孔组未满足）');
  assert.equal(failed.outcome,'未通过');
});

test('assembly relationships and counts keep their meaning without invented length units', () => {
  const report = {acceptance_contract:{schema_version:'engineering-acceptance.v2',checks:[
    {check_id:'isolation',kind:'assembly_predicate',description:'安装孔与内腔隔离',comparison:{op:'eq',value:1,unit:'count',tolerance_minus:0,tolerance_plus:0}},
    {check_id:'tabs',kind:'assembly_count',description:'安装耳数量',comparison:{op:'eq',value:4,unit:'count'}},
    {check_id:'groove',kind:'assembly_dimension',description:'槽宽',comparison:{op:'eq',value:2.4,unit:'mm'}},
  ]},acceptance:{evidence:[{check_id:'isolation',measured:[0],outcome:'failed'},
    {check_id:'tabs',measured:[3],outcome:'failed'},{check_id:'groove',measured:[2.4,2.4,2.4,2.4],outcome:'passed'}]}};
  const rows=engineeringMeasurements(report);
  assert.equal(rows[0].expected,'满足');assert.equal(rows[0].measured,'未满足');
  assert.equal(rows[0].outcome,'未通过');assert.equal(rows[0].tolerance,undefined);
  assert.equal(rows[1].expected,'4');assert.equal(rows[1].measured,'3');
  assert.equal(rows[2].measured,'2.4、2.4、2.4、2.4 mm');
  report.acceptance.evidence[0].measured=[];
  report.acceptance.evidence[0].outcome='indeterminate';
  assert.equal(engineeringMeasurements(report)[0].measured,'无量测值');
  assert.equal(engineeringMeasurements(report)[0].outcome,'未验证');
});

test('cube shape is a measured predicate, distinct from its edge length', () => {
  const report = {acceptance_contract:{schema_version:'engineering-acceptance.v2',checks:[
    {check_id:'shape',kind:'cube_shape',comparison:{op:'eq',value:1,unit:'count',tolerance_minus:0,tolerance_plus:0}},
    {check_id:'edge',kind:'cube_edge',comparison:{op:'eq',value:25,unit:'mm'}},
  ]},acceptance:{evidence:[{check_id:'shape',measured:[1],outcome:'passed'},
    {check_id:'edge',measured:[25],outcome:'passed'}]}};
  const rows = engineeringMeasurements(report);
  assert.equal(rows[0].expected, '满足'); assert.equal(rows[0].measured, '满足');
  assert.equal(rows[0].tolerance, undefined); assert.equal(rows[1].measured, '25 mm');
  report.acceptance.evidence[0].measured = [];
  report.acceptance.evidence[0].outcome = 'indeterminate';
  assert.equal(engineeringMeasurements(report)[0].outcome, '未验证');
});
