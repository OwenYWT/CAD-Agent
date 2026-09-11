import test from 'node:test';
import assert from 'node:assert/strict';
import { validateEngineeringField } from '../src/adapters/engineeringField.ts';
import type { EngineeringField } from '../src/types/engineeringTask.ts';

const field:EngineeringField={schema_version:'cad-fea-field.v1',positions_mm:[[0,0,0],[1,0,0],[0,1,0]],
  displacements_mm:[[0,0,0],[0,0,0.1],[0,0,0.2]],von_mises_mpa:[0,1,2],triangles:[[0,1,2]]};
test('FEA surface requires matching nodes, finite fields and valid connectivity',()=>{
  assert.equal(validateEngineeringField(field),field);
  for (const invalid of [{...field,triangles:[[0,1,3]]},{...field,triangles:[[0,1,1.5]]},
    {...field,displacements_mm:[[0,0,0]]},{...field,von_mises_mpa:[0,1,Infinity]},
    {...field,positions_mm:[[0,0,0],[1,NaN,0],[0,1,0]]}]) {
    assert.throws(()=>validateEngineeringField(invalid));
  }
});
