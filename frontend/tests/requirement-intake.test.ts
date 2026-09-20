import assert from 'node:assert/strict';
import test from 'node:test';
import { initialRequirementBasis, requirementNotice } from '../src/adapters/requirementIntake.ts';

test('explicit geometry records requested dimensions without inventing a measurement', () => {
  const basis = initialRequirementBasis('创建一个 100×60×3 mm 的板');
  assert.equal(basis.design_scope, 'geometry');
  assert.equal(basis.dimensions, '100×60×3 mm');
  assert.equal(basis.source_kind, 'user_specification');
  assert.equal(basis.concept_acknowledged, false);
  assert.doesNotMatch(requirementNotice(basis), /适配/);
});
test('fit, ambiguity and missing units do not become confirmed dimensions', () => {
  for (const prompt of ['适配 iPhone 的手机壳', '100×60×3 mm 的手机壳', '适配宽 25 mm 桌板的夹具']) {
    assert.equal(initialRequirementBasis(prompt).design_scope, 'physical_fit');
  }
  for (const prompt of ['创建一个板', '创建 100×60×3 的板', '创建一个板，长 100 或 120 mm']) {
    assert.equal(initialRequirementBasis(prompt).source_kind, 'none');
  }
});
test('references are not independent fit verification', () => {
  const basis = initialRequirementBasis('手机壳');
  assert.match(requirementNotice(basis), /适配未验证/);
  assert.match(requirementNotice({...basis, source_kind:'reference'}), /适配未验证/);
});
