'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { GatewayClient } = require('../src/merchant/client');
const { BILL, METHOD } = require('../src/merchant/engine');
const { buildZip } = require('./helpers/fixtures');
const { writeKind, setCoverage } = require('../src/merchant/store');
const merchant = require('../src/commands/merchant');

function fixture(t, { now = '2026-11-01T23:30:00Z', failDay, noBillDay, failLive = false, failTransfer = false, liveSells = [] } = {}) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'alipay-receipts-'));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  t.mock.timers.enable({ apis: ['Date'], now: Date.parse(now) });
  const calls = [];
  t.mock.method(GatewayClient.prototype, 'call', async (method, biz) => {
    calls.push({ method, biz });
    if (method === BILL) {
      if (biz.bill_date === failDay) return { code: '40004', sub_code: 'NO_PERMISSION' };
      if (biz.bill_date === noBillDay) return { code: '40004', sub_code: 'bill_not_exist' };
      return { code: '10000', bill_download_url: `https://example.alipay.com/${biz.bill_date}.zip` };
    }
    if (failLive || (failTransfer && method === METHOD.transfer)) throw new Error('live query unavailable');
    if (method === METHOD.sell) return { code: '10000', total_size: String(liveSells.length), detail_list: liveSells };
    if (method === METHOD.balance) return { code: '10000', total_amount: '1000.00' };
    return { code: '10000', total_size: '0', detail_list: [] };
  });
  const csv = [
    '账务流水号,业务流水号,商户订单号,商品名称,发生时间,对方账号,收入金额（+元）,支出金额（-元）,账户余额（元）,交易渠道,业务类型,备注',
    'friend-in,business-demo,,,2026-10-30 19:13:59,**甲(123***@example.cn),500.00,0.00,1000.00,支付宝,其它,转账',
    '#收入合计：1笔，共500.00元', '#支出合计：0笔，共0.00元',
  ].join('\n');
  t.mock.method(GatewayClient.prototype, 'download', async (url) => {
    const empty = csv.split('\n').filter((line) => !line.startsWith('friend-in')).join('\n').replace('1笔，共500.00元', '0笔，共0.00元');
    return buildZip({ 'fake.csv': Buffer.from(url.includes('2026-10-30') ? csv : empty) });
  });
  return { home, calls };
}

async function receipts(ctx) {
  return require('../src/commands/merchant-receipts').run(ctx);
}

test('fills a stale archive before searching incoming friend transfers, using Beijing dates', async (t) => {
  const { home, calls } = fixture(t);
  writeKind(home, 'funds', [{ id: 'out', kind: 'funds', occurred_at: '2026-10-07 10:00:00', income: '0.00', expense: '-500.00', counterparty: '**甲(123***@example.cn)' }]);
  setCoverage(home, 'signcustomer|2026-10-29', { status: 'downloaded', rows: 0 });
  const res = await receipts({ home, flags: { q: '123***@example.cn', amount: '500' }, log: () => {} });
  assert.equal(res.data.from, '2026-10-04');
  assert.equal(res.data.to_exclusive, '2026-11-03');
  assert.deepEqual(res.data.matches.map((r) => r.id), ['friend-in']);
  assert.equal(res.data.matches[0].source, 'merchant_funds');
  assert.equal(res.data.archive.complete, true);
  assert.equal(res.data.live.complete, false);
  assert.ok(calls.some((c) => c.method === BILL && c.biz.bill_date === '2026-10-30'));
  assert.ok(!calls.some((c) => c.method === BILL && c.biz.bill_date === '2026-10-29'));
  assert.ok(!calls.some((c) => c.method === BILL && c.biz.bill_date >= '2026-11-02'));
  assert.ok(calls.filter((c) => c.method === METHOD.sell).every((c) => c.biz.start_time === '2026-11-02 00:00:00' && c.biz.end_time === '2026-11-03 00:00:00'));
});

test('archive gaps and empty live APIs never become a negative payment verdict', async (t) => {
  const { home } = fixture(t, { failDay: '2026-10-31' });
  const res = await receipts({ home, flags: { from: '2026-10-30', amount: '500' }, log: () => {} });
  assert.equal(res.data.archive.complete, false);
  assert.deepEqual(res.data.archive.missing_days, ['2026-10-31']);
  assert.equal(res.data.identity_verified, false);
  assert.equal(res.data.live.complete, false);
  assert.ok(res.data.warnings.length);
});

test('historical-only lookup does not require live APIs; retains archive evidence when live fails', async (t) => {
  const { home, calls } = fixture(t, { failLive: true });
  const old = await receipts({ home, flags: { from: '2026-10-30', to: '2026-11-01', amount: '500.00' }, log: () => {} });
  assert.equal(old.data.total, 1);
  assert.equal(old.data.live, null);
  assert.ok(!calls.some((c) => c.method === METHOD.sell));
  const current = await receipts({ home, flags: { from: '2026-10-30', amount: '500.00' }, log: () => {} });
  assert.equal(current.data.total, 1);
  assert.equal(current.data.live.complete, false);
  assert.equal(current.data.live.errors.sell, 'live query unavailable');
});

test('validates dates and positive exact-decimal amounts before network access', async (t) => {
  const { home, calls } = fixture(t);
  for (const flags of [{ amount: '500.001' }, { amount: '-1' }, { amount: true }, { from: '2026-02-30' }, { to: '2026-11-04' }, { from: '2026-11-03' }]) {
    await assert.rejects(() => receipts({ home, flags, log: () => {} }), (err) => err.code === 'bad_input');
  }
  assert.equal(calls.length, 0);
});

test('today uses a one-day Beijing window and exposes partial coverage', async (t) => {
  const { home, calls } = fixture(t);
  const res = await merchant.today({ home, flags: {} });
  const sell = calls.find((c) => c.method === METHOD.sell);
  assert.equal(sell.biz.end_time, '2026-11-03 00:00:00');
  assert.equal(res.data.coverage.complete, false);
  assert.ok(res.data.coverage.note);
});

test('a temporarily missing bill is retried later, but downloaded bills are not downloaded twice', async (t) => {
  const { home, calls } = fixture(t, { noBillDay: '2026-10-30' });
  const first = await receipts({ home, flags: { from: '2026-10-30', to: '2026-10-31' }, log: () => {} });
  assert.equal(first.data.total, 0);
  assert.equal(first.data.archive.complete, false);
  // The bill becomes available on the next request, using the real ZIP parser.
  GatewayClient.prototype.call.mock.mockImplementation(async (method, biz) => {
    calls.push({ method, biz });
    return { code: '10000', bill_download_url: `https://example.alipay.com/${biz.bill_date}.zip` };
  });
  const second = await receipts({ home, flags: { from: '2026-10-30', to: '2026-10-31' }, log: () => {} });
  assert.equal(second.data.total, 1);
  assert.equal(second.data.archive.complete, true);
  const count = calls.length;
  await receipts({ home, flags: { from: '2026-10-30', to: '2026-10-31' }, log: () => {} });
  assert.equal(calls.length, count);
});

test('whole-month coverage avoids daily downloads for a partial-month lookup', async (t) => {
  const { home, calls } = fixture(t);
  setCoverage(home, 'signcustomer|2026-10', { status: 'downloaded', rows: 0 });
  const res = await receipts({ home, flags: { from: '2026-10-29', to: '2026-10-31' }, log: () => {} });
  assert.equal(res.data.archive.complete, true);
  assert.equal(calls.length, 0);
});

test('payer search does not mistake transaction IDs or remarks for payer identity', async (t) => {
  const { home } = fixture(t);
  const res = await receipts({ home, flags: { from: '2026-10-30', to: '2026-10-31', q: 'business-demo' }, log: () => {} });
  assert.equal(res.data.total, 0);
  assert.equal(res.data.identity_verified, false);
});

test('both archive-writing entry points respect the same lock and release it after failures', async (t) => {
  const { acquireLock, lockStatus } = require('../src/lock');
  const { home } = fixture(t);
  const lock = acquireLock(home, { cmd: 'other writer' });
  try {
    for (const fn of [receipts, merchant.sync]) {
      await assert.rejects(() => fn({ home, flags: { from: '2026-10-30', to: '2026-10-31' }, log: () => {} }), (err) => err.code === 'locked');
    }
  } finally { lock.release(); }
  await assert.rejects(() => merchant.sync({ home, flags: {}, log: () => {} }), (err) => err.code === 'bad_input');
  assert.equal(lockStatus(home).locked, false);
  await receipts({ home, flags: { from: '2026-10-30', to: '2026-10-31' }, log: () => {} });
  assert.equal(lockStatus(home).locked, false);
});

test('keeps matching live receipt evidence when a different endpoint fails', async (t) => {
  const base = { gmt_pay: '2026-11-02 08:00:00', total_amount: '500.00', trade_status: '成功', other_account: '**甲(123***@example.cn)', refund_amount: '0.00' };
  const { home } = fixture(t, { failTransfer: true, liveSells: [base,
    { ...base, total_amount: '50.00' }, { ...base, other_account: '**乙(999***@example.cn)' }, { ...base, trade_status: '关闭' },
  ] });
  const res = await receipts({ home, flags: { from: '2026-11-02', q: '123***@example.cn', amount: '500' }, log: () => {} });
  assert.equal(res.data.live.sells.length, 1);
  assert.equal(res.data.live.sells[0].amount, '500.00');
  assert.equal(res.data.live.transfers, null);
  assert.equal(res.data.live.errors.transfer, 'live query unavailable');
  assert.equal(res.data.live.complete, false);
});

test('corrupt archived evidence is not silently dropped by catch-up sync', async (t) => {
  const { home, calls } = fixture(t);
  const dir = path.join(home, 'merchant');
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, 'funds.ndjson');
  const bytes = '{"incomplete evidence"\n';
  fs.writeFileSync(file, bytes);
  setCoverage(home, 'signcustomer|2026-10-29', { status: 'downloaded', rows: 1 });
  const res = await receipts({ home, flags: { from: '2026-10-29', to: '2026-10-31' }, log: () => {} });
  assert.equal(fs.readFileSync(file, 'utf8'), bytes);
  assert.equal(res.data.archive.complete, false);
  assert.equal(res.data.archive.bad_lines, 1);
  assert.equal(calls.length, 0);
});
