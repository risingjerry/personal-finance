'use strict';
// Pure-function tests for the Alipay page/session classifiers and URL builders.
// No browser and no network involved.
const test = require('node:test');
const assert = require('node:assert/strict');
const alipay = require('../src/alipay');
const { toInjectableCookie } = require('../src/browser');

test('downloadUrl builds the verified query string', () => {
  const url = alipay.downloadUrl({ begin: '2025-01-01', end: '2026-01-01' });
  assert.equal(
    url,
    'https://consumeprod.alipay.com/record/download.htm?_input_charset=utf-8&suffix=csv&dateRange=customDate&dateType=createDate&fundFlow=all&beginDate=2025.01.01&endDate=2026.01.01&beginTime=00%3A00&endTime=24%3A00&tradeType=ALL&status=all&pageNum=1',
  );
});

test('standardUrl only adds parameters when a range is given', () => {
  assert.equal(alipay.standardUrl(), 'https://consumeprod.alipay.com/record/standard.htm');
  const u = new URL(alipay.standardUrl({ begin: '2025-01-01', end: '2026-01-01' }));
  assert.equal(u.searchParams.get('beginDate'), '2025.01.01');
  assert.equal(u.searchParams.get('endDate'), '2026.01.01');
  assert.equal(u.searchParams.get('pageNum'), '1');
});

test('classifySession distinguishes ok / need_login / security_challenge', () => {
  assert.equal(alipay.classifySession({ url: 'https://consumeprod.alipay.com/record/standard.htm', text: '我的账单 退出' }).session, 'ok');
  assert.equal(alipay.classifySession({ url: 'https://auth.alipay.com/login/index.htm', text: '' }).session, 'need_login');
  assert.equal(alipay.classifySession({ url: 'https://consumeprod.alipay.com/', text: '请扫码登录' }).session, 'need_login');
  assert.equal(alipay.classifySession({ url: 'https://consumeprod.alipay.com/', text: '为了您的账户安全，请完成安全校验' }).session, 'security_challenge');
  assert.equal(alipay.classifySession({ url: 'https://consumeprod.alipay.com/', text: '' }).session, 'unknown');
});

test('classifyDownloadPage recognises every documented state', () => {
  const cls = (t) => alipay.classifyDownloadPage({ url: 'https://consumeprod.alipay.com/record/download.htm', text: t }).state;
  assert.equal(cls('安全设置检测成功！无需短信校验。'), 'ready');
  assert.equal(cls('已经开始下载，请稍作等待。'), 'ready');
  assert.equal(cls('抱歉，系统繁忙，请稍候再试。'), 'rate_limited');
  assert.equal(cls('为了您的账户安全，请验证身份'), 'security_challenge');
  assert.equal(cls('请扫码登录支付宝'), 'need_login');
  assert.equal(cls('安全设置检测中，请稍候'), 'pending');
  assert.equal(cls('抱歉，导出失败'), 'error');
  assert.equal(cls(''), 'unknown');
});

test('the live error.htm?causeCode=ERROR.SYSTEM_BUSY page is rate_limited', () => {
  const state = alipay.classifyDownloadPage({
    url: 'https://consumeprod.alipay.com/error.htm?functionCode=FUN.ALL&causeCode=ERROR.SYSTEM_BUSY',
    text: '返回旧版 正在加载 ... [退出] error 抱歉，系统繁忙，请稍候再试。',
  }).state;
  assert.equal(state, 'rate_limited');
});

test('rate limiting wins over the generic error marker (系统繁忙 text)', () => {
  // The 系统繁忙 page carries no English "error" token; make sure classification
  // cannot be stolen by the error rule.
  const state = alipay.classifyDownloadPage({ url: 'https://consumeprod.alipay.com/record/download.htm', text: '系统繁忙 error' }).state;
  assert.equal(state, 'rate_limited');
});

test('parseFilename reads content-disposition variants', () => {
  assert.equal(alipay.parseFilename('attachment;filename=alipay_record_20260101_1200.zip'), 'alipay_record_20260101_1200.zip');
  assert.equal(alipay.parseFilename('attachment; filename="alipay_record_x.zip"'), 'alipay_record_x.zip');
  assert.equal(alipay.parseFilename("attachment; filename*=UTF-8''alipay%20record.zip"), 'alipay record.zip');
  assert.equal(alipay.parseFilename(null), null);
  assert.equal(alipay.parseFilename('inline'), null);
});

test('the in-page fetch expression targets the documented endpoint', () => {
  const expr = alipay.FETCH_ZIP_EXPR;
  assert.ok(expr.includes('https://consumeprod.alipay.com/record/download.resource'));
  assert.ok(expr.includes("credentials: 'include'"));
  assert.ok(expr.includes("name=securityId") || expr.includes('[name=securityId]'));
  assert.ok(expr.includes('securityId='));
  assert.ok(expr.includes('_xbox=true'));
  assert.ok(expr.includes('downloadForm'));
});

test('cookie mapping drops partitioned cookies and keeps session semantics', () => {
  const session = toInjectableCookie({ name: 'SESSION', value: 'v', domain: '.alipay.com', path: '/', expires: -1, httpOnly: true, secure: true, sameSite: 'Lax' });
  assert.deepEqual(session, { name: 'SESSION', value: 'v', domain: '.alipay.com', path: '/', secure: true, httpOnly: true, expires: -1, sameSite: 'Lax' });
  const expiring = toInjectableCookie({ name: 'c', value: 'v', domain: '.alipay.com', path: '/', expires: 1893456000, secure: false, httpOnly: false, sameSite: 'None' });
  assert.equal(expiring.expires, 1893456000);
  assert.equal(expiring.sameSite, 'None');
  assert.equal(toInjectableCookie({ name: '_CHIPS-abc', value: 'v', domain: '.alipay.com', path: '/' }), null);
  assert.equal(toInjectableCookie({ name: '', value: 'v', domain: '.alipay.com' }), null);
  const weird = toInjectableCookie({ name: 'w', value: 'v', domain: '.alipay.com', path: '/', sameSite: 'unspecified' });
  assert.equal(weird.sameSite, undefined);
  const noPath = toInjectableCookie({ name: 'p', value: 'v', domain: '.alipay.com' });
  assert.equal(noPath.path, '/');
});
