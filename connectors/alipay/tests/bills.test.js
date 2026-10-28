'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { parseStatement } = require('../src/csv');
const { statementCsv, yearRows } = require('./helpers/fixtures');
const { listBills, summarize, normalizeFilters, filterRecords, sortByCreated, recordsToCsv, recordsToNdjson, pickColumns } = require('../src/bills');
const { toCents } = require('../src/money');

function recordsFor(year, count) {
  return parseStatement(statementCsv({ begin: `${year}-01-01`, end: `${year + 1}-01-01`, rows: yearRows(year, count) })).records;
}

test('normalizeFilters maps date shorthands to bounds', () => {
  assert.equal(normalizeFilters({ from: '2025' }).start, '2025-01-01T00:00:00+08:00');
  assert.equal(normalizeFilters({ to: '2025' }).end, '2025-12-31T23:59:59+08:00');
  assert.equal(normalizeFilters({ from: '2025-02' }).start, '2025-02-01T00:00:00+08:00');
  assert.equal(normalizeFilters({ to: '2024-02' }).end, '2024-02-29T23:59:59+08:00');
  assert.equal(normalizeFilters({ to: '2024-02-03' }).end, '2024-02-03T23:59:59+08:00');
  assert.throws(() => normalizeFilters({ from: 'yesterday' }));
  assert.throws(() => normalizeFilters({ direction: 'sideways' }));
  assert.throws(() => normalizeFilters({ min: 'abc' }));
  assert.throws(() => normalizeFilters({ from: '2025-06-01', to: '2025-01-01' }));
});

test('filters by date, direction, text and exact amount bounds', () => {
  const records = recordsFor(2025, 3); // 10.50 out, 20.50 out, 30.50 in
  assert.equal(filterRecords(records, normalizeFilters({ direction: 'out' })).length, 2);
  assert.equal(filterRecords(records, normalizeFilters({ direction: 'in' })).length, 1);
  assert.equal(filterRecords(records, normalizeFilters({ q: 'shop a' })).length, 1);
  assert.equal(filterRecords(records, normalizeFilters({ q: 'item 2' })).length, 1);
  assert.equal(filterRecords(records, normalizeFilters({ min: '20.50' })).length, 2);
  assert.equal(filterRecords(records, normalizeFilters({ max: '20.50' })).length, 2);
  assert.equal(filterRecords(records, normalizeFilters({ min: '10.50', max: '20.50' })).length, 2);
  assert.equal(filterRecords(records, normalizeFilters({ from: '2026' })).length, 0);
  assert.equal(filterRecords(records, normalizeFilters({ from: '2025-01-02', to: '2025-01-31' })).length, 2);
});

test('amount bounds compare exactly, not as floats', () => {
  const base = recordsFor(2025, 1);
  const rec = { ...base[0], amount: '0.30', direction: 'out' };
  const list = [rec];
  assert.equal(filterRecords(list, normalizeFilters({ min: '0.3' })).length, 1);
  assert.equal(filterRecords(list, normalizeFilters({ min: '0.31' })).length, 0);
  assert.equal(filterRecords(list, normalizeFilters({ max: '0.29' })).length, 0);
  assert.equal(toCents(rec.amount), 30n);
});

test('listBills paginates, sorts and reports totals', () => {
  const records = recordsFor(2025, 5);
  const page1 = listBills(records, { limit: 2, offset: 0 });
  assert.equal(page1.total, 5);
  assert.equal(page1.returned, 2);
  assert.equal(page1.records[0].created_at > page1.records[1].created_at, true, 'default order is newest first');
  const page2 = listBills(records, { limit: 2, offset: 2 });
  assert.notEqual(page1.records[0].trade_no, page2.records[0].trade_no);
  const all = listBills(records, { limit: 0 });
  assert.equal(all.returned, 5);
  const asc = sortByCreated(records, 'asc');
  assert.equal(asc[0].created_at < asc[1].created_at, true);
});

test('summary reconciles with the statement footer (net of refunds)', () => {
  const csv = statementCsv({ begin: '2025-01-01', end: '2026-01-01', rows: yearRows(2025, 4) });
  const parsed = parseStatement(csv);
  const s = summarize(parsed.records, { by: 'year' });
  assert.equal(Number(s.totals.matched), 4);
  assert.equal(s.totals.expense_net, parsed.preamble.trailer['已支出'].amount);
  assert.equal(s.totals.income_net, parsed.preamble.trailer['已收入'].amount);
  assert.equal(s.totals.expense, s.totals.expense_net === '0.00' ? s.totals.expense : s.totals.expense);
  // gross expense is larger than net when a refund exists
  assert.equal(toCents(s.totals.expense) > toCents(s.totals.expense_net), true);
  assert.equal(s.groups[0].key, '2025');
  assert.equal(s.groups[0].count, 4);
});

test('summary groups by month, counterparty and type', () => {
  const records = recordsFor(2025, 4);
  const byMonth = summarize(records, { by: 'month' });
  assert.equal(byMonth.groups.length, 1);
  assert.equal(byMonth.groups[0].key, '2025-01');
  const byCp = summarize(records, { by: 'counterparty' });
  assert.deepEqual(byCp.groups.map((g) => g.key).sort(), ['Shop A', 'Shop B']);
  const byType = summarize(records, { by: 'type' });
  assert.deepEqual(byType.groups.map((g) => g.key), ['支付宝担保交易']);
  const top1 = summarize(records, { by: 'counterparty', top: 1 });
  assert.equal(top1.groups.length, 1);
  assert.equal(top1.groupCount, 2);
  assert.throws(() => summarize(records, { by: 'nope' }));
});

test('summary net = income - expense and groups sum to totals', () => {
  const records = recordsFor(2025, 5);
  const s = summarize(records, { by: 'counterparty' });
  let income = 0n;
  let expense = 0n;
  for (const g of s.groups) {
    income += toCents(g.income);
    expense += toCents(g.expense);
  }
  assert.equal(income, toCents(s.totals.income));
  assert.equal(expense, toCents(s.totals.expense));
  assert.equal(toCents(s.totals.net), income - expense);
});

test('export serialisers produce parseable csv and ndjson', () => {
  const records = recordsFor(2025, 2);
  const csv = recordsToCsv(records);
  assert.equal(csv.split('\r\n')[0], 'trade_no,merchant_order_no,created_at,paid_at,modified_at,source,type,counterparty,item,amount,direction,status,fee,refund,note,fund_state,year');
  assert.equal(csv.trim().split('\r\n').length, 3);
  const nd = recordsToNdjson(records).trim().split('\n').map((l) => JSON.parse(l));
  assert.equal(nd.length, 2);
  assert.equal(nd[0].amount, records[0].amount);
  const picked = pickColumns(records, ['trade_no', 'amount']);
  assert.deepEqual(Object.keys(picked[0]), ['trade_no', 'amount']);
});
