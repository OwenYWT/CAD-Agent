import assert from 'node:assert/strict';
import test from 'node:test';
import { initialRequirementBasis, requirementNotice } from '../src/adapters/requirementIntake.ts';

test('explicit geometry records requested dimensions without inventing a measurement', () => {
  const basis = initialRequirementBasis('创建一个 100×60×3 mm 的板');
  assert.equal(basis.design_scope, 'geometry');
  assert.equal(basis.dimensions, '创建一个 100×60×3 mm 的板');
  assert.equal(basis.source_kind, 'user_specification');
  assert.equal(basis.concept_acknowledged, false);
  assert.doesNotMatch(requirementNotice(basis), /适配/);
});

test('intake retains property, relation and feature context with each dimension', () => {
  const objective = '创建顶部开口盒体，长 80 mm、宽 55 mm、高 32 mm；内部腔体长 74 mm、宽 49 mm、深 29 mm。';
  const basis = initialRequirementBasis(objective);
  assert.equal(basis.dimensions, objective);
  assert.equal(basis.source_kind, 'user_specification');
  assert.equal(basis.design_scope, 'geometry');
});

test('excluded assembly features do not turn a nominal design into physical fit', () => {
  for (const objective of [
    '创建长 80 mm、宽 55 mm、高 32 mm 的盒体。不包含上盖、装配结构、配合间隙和螺纹。',
    '创建一个 80×55×32 mm 的盒体，不创建上盖或任何装配结构。',
    'Create an 80×55×32 mm box without mating features.',
  ]) {
    assert.equal(initialRequirementBasis(objective).design_scope, 'geometry');
  }
  assert.equal(initialRequirementBasis('不创建上盖，但底部需要适配现有支架').design_scope, 'physical_fit');
  assert.equal(initialRequirementBasis('不创建上盖但底部需要适配现有支架').design_scope, 'physical_fit');
  assert.equal(initialRequirementBasis('A box without mating features but fit for an existing bracket').design_scope, 'physical_fit');
});

test('a declared unit and labelled dimensions remain intact; long input is not truncated into a specification', () => {
  const objective = '单位：mm\n外壳外尺寸：长 80，宽 55，高 32\n壁厚：3';
  assert.equal(initialRequirementBasis(objective).dimensions, objective);
  assert.equal(initialRequirementBasis(objective).source_kind, 'user_specification');
  const long = '创建一个 80×55×32 mm 的盒体。' + '说明'.repeat(800) + '孔径至少6mm';
  assert.equal(initialRequirementBasis(long).dimensions, '');
  assert.equal(initialRequirementBasis(long).source_kind, 'none');
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
