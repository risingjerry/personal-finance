/*
 * Fast Didi trip+fare collector for `python3 -m connectors.didi fastfill`.
 *
 * The order list is paginated through the page's own signed axios ($http), so
 * wsgsig is produced by Didi's SDK (never computed or replayed here). Then one
 * real order is clicked once to obtain the detail URL template, and every
 * order's detail page is opened from that template in its own tab to read the
 * paid amount ("132.5元"). Read-only.
 *
 * stdin: {profile_dir, executable_path, url, max_months, out}
 * stdout: {ok, pages, orders:[{oid, order, productId}], fares:{oid: amount},
 *          notes:{oid: "third_party"|"cash"|"no_fare"}}
 */

'use strict';

const fs = require('fs');
const { chromium } = require('playwright');

function readInput() {
  try { return JSON.parse(fs.readFileSync(0, 'utf8') || '{}'); } catch (err) { return {}; }
}

function log(line) { process.stdout.write(line + '\n'); }

function decodeOrderId(oid) {
  const a = Buffer.from(oid + '='.repeat((4 - (oid.length % 4)) % 4), 'base64').toString('utf8');
  return Buffer.from(a + '='.repeat((4 - (a.length % 4)) % 4), 'base64').toString('utf8');
}

async function main(input) {
  const ctx = await chromium.launchPersistentContext(input.profile_dir, {
    headless: true,
    executablePath: input.executable_path || undefined,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    viewport: { width: 1280, height: 1000 },
  });
  const page = ctx.pages()[0] || await ctx.newPage();
  let historyUrl = '';
  let firstHistory = null;
  page.on('request', (r) => {
    if (!historyUrl && r.url().includes('/passenger/history')) historyUrl = r.url();
  });
  page.on('response', async (r) => {
    if (!firstHistory && r.url().includes('/passenger/history')) {
      try { firstHistory = await r.json(); } catch (err) { /* ignore */ }
    }
  });
  await page.goto(input.url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(8000);
  if (!historyUrl || !firstHistory) throw new Error('没有捕获到订单列表请求');

  // 1) All months through the page's signed axios.
  const list = await page.evaluate(async ({ tpl, first, maxMonths }) => {
    const vm = document.querySelector('#app').__vue__;
    if (!vm || !vm.$http) return { ok: false, reason: 'no-$http' };
    const url = new URL(tpl);
    const common = {};
    for (const [k, v] of url.searchParams.entries()) common[k] = v;
    delete common.wsgsig;
    delete common.sign;
    const pages = [first];
    let cursor = first && first.timemode;
    for (let i = 0; i < maxMonths && cursor; i += 1) {
      let data = null;
      try {
        const resp = await vm.$http.get(url.origin + url.pathname, {
          params: Object.assign({}, common, { timemode: cursor }), timeout: 15000,
        });
        data = resp.data;
      } catch (err) { break; }
      if (!data || (data.errno !== 0 && data.errno !== '0')) break;
      pages.push(data);
      if (!data.havenext || !data.timemode || data.timemode === cursor) break;
      cursor = data.timemode;
      await new Promise((r) => setTimeout(r, 200));
    }
    return { ok: true, pages };
  }, { tpl: historyUrl, first: firstHistory, maxMonths: Number(input.max_months) || 80 });
  if (!list.ok) throw new Error('列表接口不可用: ' + (list.reason || 'unknown'));

  const orders = [];
  const seen = new Set();
  for (const payload of list.pages) {
    for (const key of ['order_done', 'order_waiting']) {
      for (const item of (payload[key] || [])) {
        const oid = item.orderId || item.oid || item.order_id;
        if (!oid || seen.has(oid)) continue;
        seen.add(oid);
        orders.push({ oid, order: decodeOrderId(oid), productId: item.productId });
      }
    }
  }
  log('LIST pages=' + list.pages.length + ' orders=' + orders.length);

  // 2) Detail URL template from one real click (read-only).
  const idx = await page.evaluate(() => [...document.querySelectorAll('.order-item')]
    .findIndex((el) => /已完成/.test(el.innerText)));
  if (idx < 0) throw new Error('列表里没有可点击的已完成订单');
  await page.locator('.order-item').nth(idx).click({ position: { x: 60, y: 30 }, timeout: 8000 });
  await page.waitForTimeout(4000);
  const tpl = page.url();
  const base = tpl.split('#')[0];
  const route = tpl.split('#')[1].split('?')[0];
  const q0 = new URLSearchParams(tpl.split('#')[1].split('?')[1] || '');
  log('TEMPLATE ok');

  // 3) One tab per order, read the paid amount from the detail page.
  const fares = {};
  const notes = {};
  const started = Date.now();
  for (let i = 0; i < orders.length; i += 1) {
    const item = orders[i];
    const q = new URLSearchParams(q0.toString());
    q.set('oid', item.oid);
    q.set('order', item.order);
    for (const key of ['productid', 'product_id', 'Productid']) {
      if (q.has(key) && item.productId) q.set(key, String(item.productId));
    }
    const url = base + '#' + route + '?' + q.toString();
    let text = '';
    try {
      const tab = await ctx.newPage();
      const wait = tab.waitForResponse((r) => r.url().includes('pOrderDetail'), { timeout: 8000 }).catch(() => null);
      await tab.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await wait;
      await tab.waitForTimeout(500);
      text = await tab.evaluate(() => (document.body.innerText || '').slice(0, 500));
      await tab.close();
    } catch (err) {
      notes[item.oid] = 'error';
      continue;
    }
    if (/第三方服务订单/.test(text)) { notes[item.oid] = 'third_party'; continue; }
    const match = text.match(/(\d+(?:\.\d{1,2}))\s*元/);
    if (match) fares[item.oid] = match[1];
    else if (/现金支付/.test(text)) notes[item.oid] = 'cash';
    else notes[item.oid] = 'no_fare';
    if ((i + 1) % 50 === 0) {
      log('PROGRESS ' + (i + 1) + '/' + orders.length + ' fares=' + Object.keys(fares).length);
    }
  }
  const elapsed = Math.round((Date.now() - started) / 1000);
  log('FILL fares=' + Object.keys(fares).length + '/' + orders.length + ' seconds=' + elapsed);
  fs.writeFileSync(input.out, JSON.stringify({
    ok: true,
    pages: list.pages,
    orders,
    fares,
    notes,
  }, null, 1) + '\n', { mode: 0o600 });
  await ctx.close();
}

main(readInput()).catch((err) => {
  // Page URLs carry the login token: never log a query string.
  log('FATAL ' + (err && err.message ? String(err.message).replace(/\?[^\s'"]*/g, '?…') : 'unknown'));
  process.exitCode = 1;
});
