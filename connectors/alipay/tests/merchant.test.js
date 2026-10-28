'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const merchant = require('../src/commands/merchant');

function tempHome() {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'alipay-merchant-cmd-'));
  const dir = path.join(home, 'merchant');
  fs.mkdirSync(dir, { recursive: true });
  return { home, dir };
}

function seed(dir) {
  const funds = [
    { id: 'a', kind: 'funds', occurred_at: '2022-01-01 08:00:00', income: '100.00', expense: '0.00', counterparty: '**三', type: '在线支付', raw: {} },
    { id: 'b', kind: 'funds', occurred_at: '2022-01-01 08:01:00', income: '0.00', expense: '-1.00', counterparty: '支付宝', type: '收费', raw: {} },
  ];
  const trades = [{ id: 'c', kind: 'trade', occurred_at: '2022-01-01 10:00:00', income: null, expense: null, counterparty: '王五', type: '交易', raw: {} }];
  fs.writeFileSync(path.join(dir, 'funds.ndjson'), funds.map((r) => JSON.stringify(r)).join('\n') + '\n');
  fs.writeFileSync(path.join(dir, 'trades.ndjson'), trades.map((r) => JSON.stringify(r)).join('\n') + '\n');
}

test('merchant list reads the local archive and filters by date', async () => {
  const { home, dir } = tempHome();
  seed(dir);
  const res = await merchant.list({ home, flags: { from: '2022-01-01', to: '2022-01-02', limit: 10 } });
  assert.equal(res.data.total, 3);
  assert.equal(res.data.records.length, 3);
  const onlyFunds = await merchant.list({ home, flags: { kind: 'funds', from: '2022-01-01', to: '2022-01-02' } });
  assert.equal(onlyFunds.data.total, 2);
});

test('merchant summary aggregates income and expense by month', async () => {
  const { home, dir } = tempHome();
  seed(dir);
  const res = await merchant.summary({ home, flags: { by: 'month' } });
  const group = res.data.groups.find((g) => g.key === '2022-01');
  assert.equal(group.count, 3);
  assert.equal(group.income, '100.00');
  assert.equal(group.expense, '-1.00');
});

test('merchant sync refuses a missing window before any request', async () => {
  const { home } = tempHome();
  await assert.rejects(() => merchant.sync({ home, flags: {}, log: () => {} }), (err) => err.code === 'bad_input');
});

test('merchant status reports empty stores and the engine name', async () => {
  const { home } = tempHome();
  const res = await merchant.status({ home });
  assert.equal(res.data.engine, 'native (openapi.alipay.com, RSA2)');
  assert.equal(res.data.funds.records, 0);
  assert.equal(res.data.trades.records, 0);
});
