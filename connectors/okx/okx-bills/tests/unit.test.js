'use strict';
// Unit tests for the pure parts: decimal math, normalization, CSV shape.
const test = require('node:test');
const assert = require('node:assert/strict');
const U = require('../src/util');
const orders = require('../src/orders');
const { toCsv } = require('../src/export');

test('decimal math stays exact (no float drift)', () => {
  assert.equal(U.sumDecimals(['0.1', '0.2']), '0.30');
  assert.equal(U.sumDecimals(['2000.00', '1000.00']), '3000.00');
  assert.equal(U.sumDecimals(['1.005', '2.005']), '3.01');
  assert.equal(U.fromUnits(U.toUnits('10000.00001')), '10000.00001');
});

test('date window parsing is inclusive', () => {
  assert.equal(U.parseDateStart('2026-10-01'), Date.UTC(2026, 9, 1));
  assert.equal(U.parseDateEnd('2026-10-01'), Date.UTC(2026, 9, 2) - 1);
  assert.equal(U.parseDateStart('2026-10'), Date.UTC(2026, 9, 1));
});

test('normalize maps raw API fields and strips quoted names', () => {
  const raw = {
    id: 100000000000001,
    publicTradingOrderId: 100000000000002,
    side: 'buy',
    baseCurrency: 'usdt',
    baseAmount: '100.00',
    quoteCurrency: 'cny',
    quoteAmount: '2000.00',
    price: '20.00',
    exchangeRate: '20.00',
    orderStatus: 'completed',
    paymentStatus: 'confirmed',
    orderLabelMessage: '交易完成',
    counterPartyName: '"张三"',
    receiptAccountType: '支付宝',
    createdDate: 1640995200000,
    completedDate: 1640995320000,
    orderPaidDate: 1640995260000,
  };
  const o = orders.normalize(raw);
  assert.equal(o.id, '100000000000001');
  assert.equal(o.base_currency, 'USDT');
  assert.equal(o.quote_currency, 'CNY');
  assert.equal(o.counterparty, '张三');
  assert.equal(o.created_at, new Date(1640995200000).toISOString());
  assert.equal(o.paid_at, new Date(1640995260000).toISOString());
});

test('cancelled orders are excluded unless asked for', () => {
  const list = [
    { id: '1', status: 'completed', side: 'buy', created_ms: 2, quote_amount: '10', base_amount: '1' },
    { id: '2', status: 'cancelled', side: 'buy', created_ms: 1, quote_amount: '99', base_amount: '9' },
  ];
  assert.equal(orders.filterOrders(list, {}).length, 1);
  assert.equal(orders.filterOrders(list, { includeCancelled: true }).length, 2);
  assert.equal(orders.totals(list).buy_quote_total, '10.00');
});

test('csv has BOM, CRLF and quoted commas', () => {
  const csv = toCsv([{
    id: '1', public_id: '2', side: 'buy', base_currency: 'USDT', base_amount: '1.00',
    quote_currency: 'CNY', quote_amount: '7.10', price: '7.10', exchange_rate: '7.10',
    status: 'completed', payment_status: 'confirmed', counterparty: '张三,李四',
    payment_method: '支付宝', label: '交易完成', created_at: '2026-01-01T00:00:00.000Z',
  }], { tz: 'UTC' });
  assert.ok(csv.startsWith('\uFEFF'));
  assert.ok(csv.includes('\r\n'));
  assert.ok(csv.includes('"张三,李四"'));
  assert.ok(csv.includes('买入'));
  assert.ok(csv.includes('已确认'));
});

test('flow normalization keeps deposit/withdraw distinct with stable keys', () => {
  const flows = require('../src/flows');
  const d = flows.normalizeDeposit({
    depositId: 100000001, currency: 'USDT', amount: '2000.0', networkName: 'Tron (TRC20)',
    address: 'T000000000000000000000000000000001', txid: '0000000000000000000000000000000000000000000000000000000000000001', statusStr: '已转入', status: 2,
    depositInitiateTime: 1641081600000, isInnerTransfer: false,
  });
  const w = flows.normalizeWithdraw({
    id: 100000002, currency: 'USDT', amount: '100.001', fee: '0.0001', feeCurrency: 'USDT',
    networkName: 'Polygon (USDT0)', address: '0x0000000000000000000000000000000000000001', txid: '0x0000000000000000000000000000000000000000000000000000000000000001',
    statusStr: '已转出', status: 2, createDate: 1641168000000,
  });
  assert.equal(d.key, 'deposit:100000001');
  assert.equal(w.key, 'withdraw:100000002');
  assert.equal(d.type, 'deposit');
  assert.equal(w.type, 'withdraw');
  assert.equal(w.fee, '0.0001');
  assert.equal(d.fee, null);
  assert.equal(flows.totalsFlows([d, w]).deposit_amount, '2000.00');
});

test('flows csv carries type label, fee and Beijing time', () => {
  const { toFlowsCsv } = require('../src/export');
  const flows = require('../src/flows');
  const w = flows.normalizeWithdraw({
    id: 1, currency: 'USDT', amount: '10', fee: '0.1', feeCurrency: 'USDT',
    networkName: 'Tron', address: 'T000000000000000000000000000000001', txid: '0000000000000001', statusStr: '已转出', status: 2,
    createDate: Date.UTC(2026, 0, 1, 0, 0, 0),
  });
  const csv = toFlowsCsv([w]);
  assert.ok(csv.includes('提币'));
  assert.ok(csv.includes('2026-01-01 08:00:00'));
  assert.ok(csv.startsWith('\uFEFF'));
});
