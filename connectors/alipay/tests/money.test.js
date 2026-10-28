'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { toCents, formatCents, normalizeAmount, isValidAmount, sumAmounts, compareAmounts } = require('../src/money');

test('toCents parses decimal strings exactly', () => {
  assert.equal(toCents('42.12'), 4212n);
  assert.equal(toCents('0.00'), 0n);
  assert.equal(toCents('7'), 700n);
  assert.equal(toCents('7.5'), 750n);
  assert.equal(toCents('-3.09'), -309n);
  assert.equal(toCents('+1.2'), 120n);
  assert.equal(toCents('1000000.01'), 100000001n);
});

test('toCents rejects malformed amounts instead of rounding', () => {
  for (const bad of ['', 'abc', '1.234', '1,000.00', '1e5', '.5', '--1', null, undefined, 'NaN']) {
    assert.equal(isValidAmount(bad), false, `expected ${JSON.stringify(bad)} to be invalid`);
    assert.throws(() => toCents(bad));
  }
});

test('formatCents round-trips and keeps two decimals', () => {
  assert.equal(formatCents(4212n), '42.12');
  assert.equal(formatCents(0n), '0.00');
  assert.equal(formatCents(-309n), '-3.09');
  assert.equal(formatCents(5n), '0.05');
  assert.equal(normalizeAmount('42.1'), '42.10');
  assert.equal(normalizeAmount('42'), '42.00');
  assert.equal(normalizeAmount('-0.5'), '-0.50');
});

test('sumAmounts avoids float drift', () => {
  // 0.1 + 0.2 === 0.30000000000000004 in float arithmetic.
  assert.equal(sumAmounts(['0.10', '0.20']), '0.30');
  const many = Array.from({ length: 1000 }, () => '0.01');
  assert.equal(sumAmounts(many), '10.00');
  const mixed = ['99999999.99', '0.01'];
  assert.equal(sumAmounts(mixed), '100000000.00');
});

test('compareAmounts is exact at the boundary', () => {
  assert.equal(compareAmounts('1.10', '1.1'), 0);
  assert.equal(compareAmounts('1.10', '1.11'), -1);
  assert.equal(compareAmounts('100.00', '99.99'), 1);
  assert.equal(compareAmounts('-1.00', '0.00'), -1);
});
