/**
 * Unit tests for resolving the Agent's evidence refs into reader-facing labels.
 *
 * The panel already receives every finding and its evidence in the diagnosis
 * payload, so citation display is pure payload mapping and can be checked here
 * without a browser. See ``DiagnosisPanel`` for where the result is used.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildEvidenceIndex,
  describeEvidenceRef,
  evidenceAnchorId,
  findingTitle,
} from '../src/components/admin/evidenceRefs.js';

const orderFinding = {
  metric: 'order_count',
  label: '订单量',
  menu_item_id: null,
  evidence: [
    { ref: 'f0.e0', label: '本期订单量', value: 20 },
    { ref: 'f0.e1', label: '上期订单量', value: 70 },
    { ref: 'f0.e2', label: '变化幅度', value: -71.43 },
  ],
};

const dishFinding = {
  metric: 'dish_sales',
  label: '菜品销量',
  menu_item_id: 650,
  evidence: [
    { ref: 'f1.e0', label: '「招牌牛肉面」本期销量', value: 6 },
    { ref: 'f1.e1', label: '「招牌牛肉面」上期销量', value: 21 },
    { ref: 'f1.e2', label: '「招牌牛肉面」逐日走势', value: '整期缓降' },
  ],
};

test('a store-level ref reads as finding › evidence', () => {
  const index = buildEvidenceIndex([orderFinding, dishFinding]);
  assert.equal(describeEvidenceRef('f0.e1', index), '订单量 › 上期订单量');
});

test('a dish ref names the dish once, not twice', () => {
  const index = buildEvidenceIndex([orderFinding, dishFinding]);
  assert.equal(
    describeEvidenceRef('f1.e0', index),
    '菜品销量：招牌牛肉面 › 本期销量'
  );
});

test('the dish name is lifted from the evidence when the finding has none', () => {
  assert.equal(findingTitle(dishFinding), '菜品销量：招牌牛肉面');
  assert.equal(findingTitle(orderFinding), '订单量');
});

test('every evidence item is addressable by its own ref', () => {
  const index = buildEvidenceIndex([orderFinding, dishFinding]);
  assert.deepEqual([...index.keys()], [
    'f0.e0',
    'f0.e1',
    'f0.e2',
    'f1.e0',
    'f1.e1',
    'f1.e2',
  ]);
});

test('an unchanged naming scheme is not re-parsed from the string', () => {
  // The index is built from the items in order, not by splitting the ref on
  // ".e"; a renamed handle still resolves as long as both sides agree.
  const renamed = {
    ...orderFinding,
    evidence: [{ ref: 'order.volume.current', label: '本期订单量', value: 20 }],
  };
  const index = buildEvidenceIndex([renamed]);
  assert.equal(
    describeEvidenceRef('order.volume.current', index),
    '订单量 › 本期订单量'
  );
});

test('an unresolvable ref falls back to the raw handle instead of vanishing', () => {
  const index = buildEvidenceIndex([orderFinding]);
  assert.equal(describeEvidenceRef('f9.e9', index), 'f9.e9');
  assert.equal(describeEvidenceRef('f0.e0', new Map()), 'f0.e0');
});

test('evidence anchors are stable and distinct per ref', () => {
  assert.equal(evidenceAnchorId('f0.e2'), 'evidence-f0.e2');
  assert.notEqual(evidenceAnchorId('f0.e2'), evidenceAnchorId('f0.e3'));
});
