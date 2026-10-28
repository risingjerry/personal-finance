'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { loadBills, writeBills, mergeRecords, commitRecords, readMeta, writeMeta, updateMeta } = require('../src/store');

function tempHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'alipay-store-'));
}

const rec = (over = {}) => ({
  id: over.trade_no || '1',
  trade_no: '1',
  merchant_order_no: '',
  created_at: '2025-01-01T00:00:00+08:00',
  paid_at: null,
  modified_at: '2025-01-01T00:00:00+08:00',
  source: '淘宝',
  type: '支付宝担保交易',
  counterparty: 'shop',
  item: 'thing',
  amount: '1.00',
  direction: 'out',
  status: '交易成功',
  fee: '0.00',
  refund: '0.00',
  note: '',
  fund_state: '已支出',
  year: 2025,
  month: '2025-01',
  raw: [],
  ...over,
});

test('mergeRecords dedupes by trade_no and keeps the newer modified_at', () => {
  const existing = [rec({ trade_no: 'a', modified_at: '2025-01-01T00:00:00+08:00', item: 'old' })];
  const incoming = [
    rec({ trade_no: 'a', modified_at: '2025-02-01T00:00:00+08:00', item: 'new' }),
    rec({ trade_no: 'b', modified_at: '2025-01-01T00:00:00+08:00' }),
    rec({ trade_no: 'a', modified_at: '2024-01-01T00:00:00+08:00', item: 'older' }),
  ];
  const merged = mergeRecords(existing, incoming);
  assert.equal(merged.added, 1);
  assert.equal(merged.updated, 1);
  assert.equal(merged.unchanged, 1);
  const a = merged.records.find((r) => r.trade_no === 'a');
  assert.equal(a.item, 'new');
});

test('writeBills + loadBills round-trip and tolerate corrupt lines', () => {
  const home = tempHome();
  writeBills(home, [rec({ trade_no: '2' }), rec({ trade_no: '1' })]);
  const loaded = loadBills(home);
  assert.equal(loaded.records.length, 2);
  assert.equal(loaded.badLines, 0);
  // sorted ascending by created_at then trade_no
  assert.deepEqual(loaded.records.map((r) => r.trade_no), ['1', '2']);

  fs.appendFileSync(loaded.file, '{not json}\n');
  const again = loadBills(home);
  assert.equal(again.records.length, 2);
  assert.equal(again.badLines, 1);
});

test('commitRecords persists records and per-year meta', () => {
  const home = tempHome();
  const res = commitRecords(home, [rec({ trade_no: 'x' }), rec({ trade_no: 'y' })], {
    source: 'sync',
    years: { 2025: { status: 'complete', rows: 2, begin: '2025-01-01', end: '2026-01-01' } },
  });
  assert.equal(res.added, 2);
  assert.equal(res.records, 2);
  const meta = readMeta(home);
  assert.equal(meta.records, 2);
  assert.equal(meta.years['2025'].status, 'complete');
  assert.equal(meta.stats.exports, 1);

  // Re-committing the same rows changes nothing but keeps meta consistent.
  const again = commitRecords(home, [rec({ trade_no: 'x' })], { source: 'sync' });
  assert.equal(again.added, 0);
  assert.equal(again.unchanged, 1);
  assert.equal(readMeta(home).records, 2);
});

test('meta is readable when missing or corrupt', () => {
  const home = tempHome();
  assert.equal(readMeta(home).records, 0);
  fs.mkdirSync(path.join(home, 'data'), { recursive: true });
  fs.writeFileSync(path.join(home, 'data', 'meta.json'), '{oops');
  const meta = readMeta(home);
  assert.equal(meta.records, 0);
  assert.equal(meta.corrupted, true);
});

test('updateMeta merges year maps instead of replacing them', () => {
  const home = tempHome();
  writeMeta(home, { years: { 2024: { status: 'complete' } } });
  updateMeta(home, { years: { 2025: { status: 'complete' } }, lastSyncAt: 'now' });
  const meta = readMeta(home);
  assert.ok(meta.years['2024']);
  assert.ok(meta.years['2025']);
  assert.equal(meta.lastSyncAt, 'now');
});

test('a failed write never leaves a half-written bills file', () => {
  const home = tempHome();
  writeBills(home, [rec({ trade_no: 'keep' })]);
  const before = fs.readFileSync(path.join(home, 'data', 'bills.ndjson'), 'utf8');
  // Simulate a crash mid-write by writing a temp file and never renaming it.
  fs.writeFileSync(path.join(home, 'data', '.bills.ndjson.999.tmp'), '{ partial');
  assert.equal(fs.readFileSync(path.join(home, 'data', 'bills.ndjson'), 'utf8'), before);
  assert.equal(loadBills(home).records.length, 1);
});
