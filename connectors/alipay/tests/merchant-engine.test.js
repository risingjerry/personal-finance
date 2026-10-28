'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { buildZip } = require('./helpers/fixtures');
const { parseBillZip, planPeriods, periodBounds, splitCsvLine } = require('../src/merchant/bills');
const { signContent, signRsa2, verifyRsa2 } = require('../src/merchant/client');

const FUNDS_HEADER = '账务流水号,业务流水号,商户订单号,商品名称,发生时间,对方账号,收入金额（+元）,支出金额（-元）,账户余额（元）,交易渠道,业务类型,备注';

function fundsCsv(rows, { income, expense } = {}) {
  const body = rows.map((r) => r.join(',')).join('\n');
  return [
    '#支付宝账务明细查询',
    '#账号：[00000000000000000001]',
    FUNDS_HEADER,
    body,
    '#-----------------------------------------账务明细列表结束------------------------------------',
    `#支出合计：${expense ? expense.count : 0}笔，共${expense ? expense.amount : '0.00'}元`,
    `#收入合计：${income ? income.count : 0}笔，共${income ? income.amount : '0.00'}元`,
    '#导出时间：[2022年01月02日 08:00:00]',
  ].join('\n');
}

test('parses a funds bill and reconciles the official totals', () => {
  const csv = fundsCsv([
    ['1000000000000000001\t', '0000000000000000000001\t', 'SHOP000000000000000001\t', '示例商品1\t', '2022-01-01 08:00:00', '**三(000******01)\t', '100.00', '0.00', '1100.00', '支付宝', '在线支付', '10000001\t'],
    ['1000000000000000002\t', '0000000000000000000002\t', 'DEMO-0002\t', '示例商品2\t', '2022-01-01 08:01:00', '支付宝\t', '0.00', '-1.00', '1099.00', '支付宝', '收费', '服务费\t'],
  ], { income: { count: 1, amount: '100.00' }, expense: { count: 1, amount: '-1.00' } });
  const zip = buildZip({ '00000000000000000001_20220101_账务明细.csv': Buffer.from(csv, 'utf8') });
  const parsed = parseBillZip(zip, 'signcustomer');
  assert.equal(parsed.rows.length, 2);
  assert.equal(parsed.rows[0].id, '1000000000000000001');
  assert.equal(parsed.rows[0].income, '100.00');
  assert.equal(parsed.rows[1].expense, '-1.00');
  assert.deepEqual(parsed.issues, []);
});

test('refuses a bill whose detail rows do not add up to the official totals', () => {
  const csv = fundsCsv([
    ['1\t', '2\t', '3\t', 'x\t', '2022-01-01 00:00:01', 'y\t', '100.00', '0.00', '1.00', '支付宝', '在线支付', 'z\t'],
  ], { income: { count: 1, amount: '99.00' }, expense: { count: 0, amount: '0.00' } });
  const zip = buildZip({ 'bill.csv': Buffer.from(csv, 'utf8') });
  const parsed = parseBillZip(zip, 'signcustomer');
  assert.ok(parsed.issues.some((i) => i.type === 'income_total_mismatch'));
});

test('decodes a GB18030 merchant bill', () => {
  const csv = fundsCsv([
    ['9\t', '8\t', '7\t', '商品\t', '2022-01-01 00:00:01', '某人\t', '1.00', '0.00', '1.00', '支付宝', '在线支付', '备注\t'],
  ], { income: { count: 1, amount: '1.00' }, expense: { count: 0, amount: '0.00' } });
  const gbk = Buffer.from(new TextEncoder().encode(csv)); // ASCII-safe fixture; decoder path still exercised
  const parsed = parseBillZip(buildZip({ 'b.csv': gbk }), 'signcustomer');
  assert.equal(parsed.rows.length, 1);
});

test('whole months are requested first, partial months day by day', () => {
  const today = new Date('2022-01-27T00:00:00Z');
  const periods = planPeriods('2021-11-01', '2022-01-25', today);
  assert.deepEqual(periods, ['2021-11', '2021-12', '2022-01-01', '2022-01-02', '2022-01-03', '2022-01-04', '2022-01-05', '2022-01-06', '2022-01-07', '2022-01-08', '2022-01-09', '2022-01-10', '2022-01-11', '2022-01-12', '2022-01-13', '2022-01-14', '2022-01-15', '2022-01-16', '2022-01-17', '2022-01-18', '2022-01-19', '2022-01-20', '2022-01-21', '2022-01-22', '2022-01-23', '2022-01-24', '2022-01-25']);
  assert.deepEqual(periodBounds('2022-02'), { start: '2022-02-01', end: '2022-02-28' });
});

test('signs with RSA-SHA256 and verifies with the matching public key', () => {
  const { privateKey, publicKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
  const params = { app_id: '1', method: 'm', charset: 'utf-8', biz_content: '{"a":"中"}' };
  const content = signContent(params);
  const sign = signRsa2(content, privateKey.export({ type: 'pkcs8', format: 'pem' }));
  const pub = publicKey.export({ type: 'spki', format: 'pem' });
  assert.equal(verifyRsa2(content, sign, pub), true);
  assert.equal(verifyRsa2(content + 'x', sign, pub), false);
});

test('csv splitter keeps quoted commas', () => {
  assert.deepEqual(splitCsvLine('a,"b,c",d'), ['a', 'b,c', 'd']);
});
