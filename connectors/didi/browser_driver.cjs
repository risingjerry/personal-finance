/*
 * Bundled Node Playwright driver for `python3 -m connectors.didi fetch --browser`.
 *
 * The Python CLI passes the owner's browser cookies on stdin (never argv, never a
 * file), this script puts them into an in-memory browser context, lets Didi's
 * own JavaScript sign its requests, and prints captured JSON on stdout.
 *
 * It never computes wsgsig. It only clicks order cards (read-only) and stops on
 * a captcha/slider/login/risk page. Cookie/token values are never logged.
 */

'use strict';

const fs = require('fs');

function readInput() {
  try {
    return JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
  } catch (err) {
    return {};
  }
}

class Blocked extends Error {
  constructor(kind, message, hint) {
    super(message);
    this.kind = kind;
    this.hint = hint || '';
  }
}

const FORBIDDEN = [
  '支付', '去支付', '立即支付', '发票', '开发票', '开票', '投诉',
  '取消订单', '取消行程', '退款', '申请退款', '评价',
  '修改订单', '删除订单', '确认上车', '确认到达',
];

const BLOCK_SELECTORS = [
  '.geetest_panel',
  '.geetest_widget',
  '.nc-container',
  '.nc_1_wrapper',
  "[class*='slide-verify']",
  "[class*='slider-verify']",
  "[class*='captcha']",
  "[id*='captcha']",
  "iframe[src*='captcha']",
  "iframe[src*='verify']",
  '.verify-wrap',
  '.verify-box',
];

function hasForbidden(text) {
  return FORBIDDEN.some((word) => (text || '').includes(word));
}

function captureKind(pathname) {
  const p = (pathname || '').replace(/\/+$/, '');
  if (p.endsWith('/passenger/history')) return 'order_list';
  if (/\/pOrderDetail\/?$/i.test(p)) return 'order_detail';
  if (/\/(?:pGetRealTimeFeeDetail|pGetBillDetail)\/?$/i.test(p)) return 'fare_detail';
  return '';
}

function queryDict(rawUrl) {
  const out = {};
  try {
    const u = new URL(rawUrl);
    for (const [key, value] of u.searchParams.entries()) {
      if (!(key in out)) out[key] = value;
    }
  } catch (err) { /* ignore */ }
  return out;
}

function iterItems(payload) {
  if (!payload || typeof payload !== 'object') return [];
  const containers = [payload];
  if (payload.data && typeof payload.data === 'object') containers.push(payload.data);
  const items = [];
  for (const container of containers) {
    for (const key of ['order_waiting', 'order_done', 'orders', 'list']) {
      const value = container[key];
      if (Array.isArray(value)) {
        for (const item of value) if (item && typeof item === 'object') items.push(item);
      }
    }
  }
  return items;
}

function orderId(item) {
  for (const key of ['orderId', 'order_id', 'oid', 'order']) {
    let value = item ? item[key] : '';
    if (value && typeof value === 'object') {
      value = value.order_id || value.oid || value.orderId;
    }
    if (value !== undefined && value !== null && String(value).trim()) {
      return String(value).trim();
    }
  }
  return '';
}

class CaptureStore {
  constructor() {
    this.captures = [];
    this.listCaptures = [];
    this.details = {};
    this.domFares = {};
    this.currentOid = '';
    this.warnings = [];
    this.httpErrors = [];
    this.seen = new Set();
    this.pending = new Set();
  }

  attach(page) {
    page.on('response', (response) => {
      const promise = this.onResponse(response)
        .catch(() => {})
        .finally(() => this.pending.delete(promise));
      this.pending.add(promise);
    });
  }

  async drain() {
    while (this.pending.size) {
      await Promise.allSettled(Array.from(this.pending));
      await new Promise((r) => setTimeout(r, 30));
    }
  }

  async onResponse(response) {
    let url;
    let pathname;
    try {
      url = response.url();
      pathname = new URL(url).pathname;
    } catch (err) {
      return;
    }
    const kind = captureKind(pathname);
    if (!kind) return;
    const status = Number(response.status ? response.status() : 0);
    const query = queryDict(url);
    const oid = query.order_id || query.oid || query.orderId || this.currentOid || '';
    let pageNumber = null;
    let timemode = '';
    if (kind === 'order_list') {
      pageNumber = Number.parseInt(query.pagenum || '0', 10);
      if (!Number.isFinite(pageNumber)) pageNumber = 0;
      timemode = query.timemode || '';
    }
    const key = [kind, pageNumber, timemode, oid].join('|');
    if (this.seen.has(key)) return;
    this.seen.add(key);

    let payload = null;
    if (status >= 400) {
      this.httpErrors.push({ kind, status });
      payload = { _http_status: status };
    } else {
      try {
        payload = await response.json();
      } catch (err) {
        try {
          const text = (await response.text()) || '';
          payload = { _non_json: true, text: text.slice(0, 2000) };
        } catch (err2) {
          return;
        }
      }
    }
    const capture = {
      kind,
      url,
      status,
      response: payload,
      order_id: kind === 'order_list' ? '' : oid,
      page: pageNumber,
      timemode,
    };
    this.captures.push(capture);
    if (kind === 'order_list') {
      this.listCaptures.push(capture);
    } else {
      if (!this.details[oid]) this.details[oid] = { detail: null, fare: null };
      const target = kind === 'order_detail' ? 'detail' : 'fare';
      if (this.details[oid][target] === null) this.details[oid][target] = payload;
    }
  }

  listOrderIds() {
    const ids = [];
    const seen = new Set();
    for (const capture of this.listCaptures) {
      for (const item of iterItems(capture.response)) {
        const oid = orderId(item);
        if (oid && !seen.has(oid)) {
          seen.add(oid);
          ids.push(oid);
        }
      }
    }
    return ids;
  }

  lastList() {
    for (let i = this.listCaptures.length - 1; i >= 0; i -= 1) {
      const payload = this.listCaptures[i].response;
      if (payload && typeof payload === 'object') return payload;
    }
    return null;
  }

  hasDetail(oid) {
    const bucket = this.details[oid] || {};
    return Boolean(bucket.detail || bucket.fare);
  }
}

async function visibleText(page) {
  try {
    const text = await page.locator('body').innerText({ timeout: 1500 });
    return (text || '').slice(0, 8000);
  } catch (err) {
    return '';
  }
}

async function selectorHits(page) {
  try {
    for (const frame of page.frames()) {
      const frameUrl = (frame.url() || '').toLowerCase();
      if (['captcha', 'geetest', 'verify', 'risk'].some((m) => frameUrl.includes(m))) {
        return 1;
      }
    }
  } catch (err) { /* ignore */ }
  for (const selector of BLOCK_SELECTORS) {
    try {
      const locator = page.locator(selector);
      if ((await locator.count()) <= 0) continue;
      try {
        if (await locator.first().isVisible()) return 1;
      } catch (err) {
        return 1;
      }
    } catch (err) { /* ignore */ }
  }
  return 0;
}

function classifyBlock(url, text, hits) {
  const lowered = (url || '').toLowerCase();
  let host = '';
  let path = '';
  try {
    const parsed = new URL(url);
    host = (parsed.hostname || '').toLowerCase();
    path = (parsed.pathname || '').toLowerCase();
  } catch (err) { /* ignore */ }
  if (/^(passport|login|sso|epassport)\./.test(host)) {
    return ['auth', '滴滴跳转到了登录页'];
  }
  if (['x5sec', 'punish', '/captcha', '/verify', '/risk'].some((m) => lowered.includes(m)) ||
      ['captcha', 'verify', 'risk'].some((m) => path.includes(m))) {
    return ['risk', '滴滴请求被重定向到验证/风控地址'];
  }
  if (hits) return ['risk', '页面出现了验证码或滑块组件'];

  const riskMarkers = ['验证码', '滑动验证', '请完成验证', '安全验证', '访问频繁',
    '请求频繁', '操作频繁', '账号异常', '风险拦截', '请完成安全验证'];
  for (const marker of riskMarkers) {
    if ((text || '').toLowerCase().includes(marker.toLowerCase())) {
      return ['risk', `页面显示了验证/风控提示（${marker}）`];
    }
  }
  const authMarkers = ['请登录', '请先登录', '登录已失效', '登录状态已失效',
    '未登录', '重新登录', 'login_outdate'];
  for (const marker of authMarkers) {
    if ((text || '').toLowerCase().includes(marker.toLowerCase())) {
      return ['auth', `页面显示了登录提示（${marker}）`];
    }
  }
  return null;
}

async function currentBlock(page) {
  return classifyBlock(page.url(), await visibleText(page), await selectorHits(page));
}

function blockError(block) {
  if (block[0] === 'auth') {
    return new Blocked('auth', block[1],
      '请先在浏览器中人工重新登录滴滴，再重试；本工具不会绕过登录');
  }
  return new Blocked('risk', block[1],
    '页面出现了验证码/风控；本工具会立即停止，不会尝试破解或绕过');
}

function listFinished(store) {
  const response = store.lastList();
  if (!response) return false;
  if (Object.prototype.hasOwnProperty.call(response, 'havenext')) {
    const value = response.havenext;
    if (value === false || value === 0 || value === '0' || value === 'false' || value === 'False') {
      return true;
    }
  }
  if (!response.timemode) {
    if (iterItems(response).length === 0 && store.listCaptures.length) return true;
  }
  return false;
}

function checkListPayload(store) {
  for (const capture of store.listCaptures) {
    const payload = capture.response;
    if (!payload || typeof payload !== 'object') continue;
    if (payload._non_json) {
      const text = String(payload.text || '');
      const block = classifyBlock(capture.url, text, 0);
      if (block) throw blockError(block);
      if (text.includes('登录')) {
        throw new Blocked('auth', '滴滴列表接口返回了登录 HTML',
          '请先在浏览器中人工重新登录滴滴后重试');
      }
      if (['验证码', '验证', '风控', '频繁', 'captcha'].some((m) => text.includes(m))) {
        throw new Blocked('risk', '滴滴列表接口返回了验证/风控 HTML',
          '本工具会立即停止，不会尝试破解或绕过');
      }
      throw new Blocked('timeout', '滴滴列表接口返回了非 JSON 内容');
    }
    const errno = payload.errno;
    if (errno === 0 || errno === '0' || errno === undefined || errno === null) continue;
    const text = ['errmsg', 'error', 'message', 'msg', 'ret']
      .map((k) => String(payload[k] || '')).join(' ');
    const lowered = text.toLowerCase();
    if (['wsgsig', '签名', '验签', 'sign error', 'invalid sign', 'missing sign']
      .some((m) => lowered.includes(m.toLowerCase()))) {
      throw new Blocked('risk', '滴滴列表接口拒绝了浏览器请求（wsgsig 签名未被接受）',
        '页面自身签名仍未通过；本工具不会重算、伪造或重放 wsgsig。' +
        '请先在浏览器中人工打开订单列表确认可用');
    }
    if (['101', '401', '41005'].includes(String(errno)) ||
        ['请登录', '未登录', '登录失效', 'login'].some((m) => text.includes(m))) {
      throw new Blocked('auth', '滴滴列表接口返回未登录',
        '请先在浏览器中人工重新登录滴滴后重试；本工具不会绕过登录');
    }
    if (String(errno) === '403' ||
        ['风控', '验证', '频繁', '限制'].some((m) => text.includes(m))) {
      throw new Blocked('risk', '滴滴列表接口返回验证/风控提示',
        '本工具会立即停止，不会尝试破解或绕过');
    }
    throw new Blocked('risk', `滴滴列表接口返回失败 (errno=${errno})`);
  }
}

async function waitFirstList(page, store, timeoutMs) {
  const deadline = Date.now() + Math.max(15000, timeoutMs);
  while (Date.now() < deadline) {
    const block = await currentBlock(page);
    if (block) throw blockError(block);
    if (store.listCaptures.length) {
      if (store.httpErrors.length) raiseHttp(store);
      checkListPayload(store);
      return;
    }
    if (store.httpErrors.length) raiseHttp(store);
    const text = await visibleText(page);
    if (text.includes('加载失败') || text.includes('暂时无法获取您的订单')) {
      await page.waitForTimeout(2000);
      await store.drain();
      if (!store.listCaptures.length) {
        throw new Blocked('auth', '滴滴订单页没有进入订单列表（登录态可能已失效）',
          '页面显示“加载失败/暂时无法获取您的订单”。请先在浏览器中重新登录滴滴，' +
          '确认能人工打开订单列表后再运行；本工具不会尝试绕过登录或风控。');
      }
    }
    await page.waitForTimeout(500);
    await store.drain();
  }
  throw new Blocked('timeout', '等待滴滴订单列表超时',
    '页面可能加载很慢或网络受限；可增大 --timeout 后重试');
}

function raiseHttp(store) {
  for (const item of store.httpErrors) {
    if (item.kind === 'order_list') {
      if (item.status === 401 || item.status === 403) {
        throw new Blocked('risk', `滴滴订单列表返回 HTTP ${item.status}`,
          '可能是登录失效或风控；请先在浏览器中人工确认');
      }
      if (item.status === 501) {
        throw new Blocked('risk', '滴滴订单列表返回 HTTP 501（请求可能缺少 wsgsig 签名）',
          '页面自身签名仍未通过；本工具不会重算、伪造或重放 wsgsig。' +
          '请先在浏览器中人工打开订单列表确认可用');
      }
      throw new Blocked('timeout', `滴滴订单列表返回 HTTP ${item.status}`);
    }
    if ((item.kind === 'order_detail' || item.kind === 'fare_detail') &&
        (item.status === 401 || item.status === 403)) {
      throw new Blocked('risk', `滴滴详情/费用接口返回 HTTP ${item.status}`,
        '可能是登录失效或风控；本工具会立即停止');
    }
  }
}

const NEXT_HINTS = ['轻按查看', '加载更多', '查看更多', '上拉加载', '点击加载',
  '查看更早', '更多订单', '查看历史订单'];

async function clickNextHint(page) {
  // The order list paginates by month: a footer like "轻按查看26年09月订单"
  // has to be clicked (read-only). Scroll to the bottom so a lazy footer mounts.
  try { await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight)); } catch (err) { /* ignore */ }
  await page.waitForTimeout(600);
  for (const hint of NEXT_HINTS) {
    try {
      const locator = page.getByText(hint, { exact: false });
      const total = await locator.count();
      for (let i = total - 1; i >= 0; i -= 1) {
        const node = locator.nth(i);
        try {
          if (!(await node.isVisible())) continue;
          const text = ((await node.innerText({ timeout: 600 })) || '').trim();
          if (!text || text.length > 60 || hasForbidden(text)) continue;
          await node.scrollIntoViewIfNeeded({ timeout: 3000 });
          await node.click({ timeout: 4000 });
          return text;
        } catch (err) { /* try next match */ }
      }
    } catch (err) { /* try next hint */ }
  }
  return '';
}

async function paginateOne(page, store, timeoutMs) {
  // Advance exactly one month: click the footer hint, wait for its list XHR.
  const before = store.listCaptures.length;
  try {
    await page.evaluate(() => {
      const els = [document.scrollingElement, ...document.querySelectorAll('*')];
      for (const el of els) {
        if (!el) continue;
        if (el.scrollHeight > el.clientHeight + 40) el.scrollTop = el.scrollHeight;
      }
      window.scrollTo(0, document.body.scrollHeight);
    });
    await page.mouse.move(620, 520);
    await page.mouse.wheel(0, 24000);
  } catch (err) { /* ignore */ }
  await page.waitForTimeout(800);
  const clicked = await clickNextHint(page);
  if (!clicked) store.warnings.push('没有找到下一页/上一月的入口（页面可能已到最旧）');
  if (!clicked) return false;
  const deadline = Date.now() + Math.min(25000, Math.max(8000, timeoutMs));
  while (Date.now() < deadline && store.listCaptures.length <= before) {
    await page.waitForTimeout(400);
    await store.drain();
  }
  if (store.listCaptures.length <= before) {
    store.warnings.push(`点击“${clicked}”后没有新订单返回，分页停止`);
    return false;
  }
  await page.waitForTimeout(700);
  return true;
}

async function orderCards(page) {
  try {
    return await page.evaluate(() => [...document.querySelectorAll('.order-item')].map((el, i) => {
      const vm = el.__vue__;
      const item = (vm && vm.item) || {};
      const oid = item.orderId || item.oid || item.order_id || '';
      return { index: i, order_id: String(oid || ''), text: (el.innerText || '').slice(0, 300) };
    }));
  } catch (err) {
    return [];
  }
}

async function safeClick(page, card) {
  try { await card.scrollIntoViewIfNeeded({ timeout: 5000 }); } catch (err) { /* ignore */ }
  for (const selector of ['.order-desc', '.order-sub-desc', '.from', '.to', '.order-time', '.time']) {
    try {
      const target = card.locator(selector).first();
      if ((await target.count()) <= 0) continue;
      if (!(await target.isVisible())) continue;
      const label = await target.innerText({ timeout: 800 });
      if (hasForbidden(label)) continue;
      await target.click({ timeout: 5000 });
      return true;
    } catch (err) { /* try next */ }
  }
  try {
    const box = await card.boundingBox();
    if (box && box.width && box.height) {
      await page.mouse.click(box.x + Math.min(18, box.width / 3), box.y + Math.min(18, box.height / 3));
      return true;
    }
  } catch (err) { /* ignore */ }
  try {
    if (hasForbidden(await card.innerText({ timeout: 800 }))) return false;
    await card.click({ timeout: 5000 });
    return true;
  } catch (err) { return false; }
}

async function dismissModals(page) {
  for (const text of ['我知道了', '知道了', '关闭']) {
    if (hasForbidden(text)) continue;
    try {
      const locator = page.getByText(text, { exact: true });
      if ((await locator.count()) <= 0) continue;
      const target = locator.last();
      if (await target.isVisible()) await target.click({ timeout: 2000 });
    } catch (err) { /* ignore */ }
  }
}

async function waitForDetail(page, store, oid, timeoutMs, withFare) {
  const deadline = Date.now() + Math.max(10000, timeoutMs);
  while (Date.now() < deadline) {
    if (store.httpErrors.length) raiseHttp(store);
    const block = await currentBlock(page);
    if (block) throw blockError(block);
    if (store.hasDetail(oid)) {
      if (withFare && !(store.details[oid] || {}).fare) {
        await page.waitForTimeout(800);
        await store.drain();
      }
      return;
    }
    await page.waitForTimeout(300);
    await store.drain();
  }
  store.warnings.push(`订单 ${oid} 未捕获到详情/费用响应`);
}

async function restoreOrderList(page, store, url, timeoutMs) {
  const currentUrl = page.url() || '';
  if (currentUrl.includes('#/order-list')) return;
  if (currentUrl.split('?')[0].replace(/\/+$/, '').endsWith('/order-list')) return;
  try {
    await page.goBack({ waitUntil: 'domcontentloaded', timeout: timeoutMs });
  } catch (err) {
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    } catch (err2) {
      return;
    }
  }
  await page.waitForTimeout(500);
  await store.drain();
  if (!store.listCaptures.length) await waitFirstList(page, store, timeoutMs);
  try {
    if ((await page.locator('.order-item').count()) === 0) {
      try { await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight)); } catch (err) { /* ignore */ }
      await page.waitForTimeout(600);
    }
  } catch (err) {
    if (err instanceof Blocked) throw err;
  }
}

async function readDomFare(page) {
  try {
    const text = await page.evaluate(() => (document.body.innerText || '').slice(0, 500));
    const match = text.match(/(\d+(?:\.\d{1,2}))\s*元/);
    return match ? match[1] : '';
  } catch (err) { return ''; }
}

async function openFareDetail(page, store, oid, timeoutMs) {
  // The detail page shows the paid amount as "13.9元" plus a "查看明细" link.
  // Clicking it (read-only) makes the page request pGetBillDetail, which is
  // the authoritative breakdown. Never touch 开具发票/支付/投诉.
  await page.waitForTimeout(500);
  const hints = ['查看明细', '费用明细', '查看费用明细'];
  for (const hint of hints) {
    let locator = null;
    try { locator = page.getByText(hint, { exact: false }); } catch (err) { continue; }
    let total = 0;
    try { total = await locator.count(); } catch (err) { continue; }
    for (let i = 0; i < total; i += 1) {
      try {
        const node = locator.nth(i);
        if (!(await node.isVisible())) continue;
        const label = ((await node.innerText({ timeout: 500 })) || '').trim();
        if (!label || label.length > 20) continue;
        if (hasForbidden(label)) continue;
        await node.click({ timeout: 4000 });
      } catch (err) { continue; }
      const deadline = Date.now() + Math.min(8000, timeoutMs);
      while (Date.now() < deadline) {
        if (store.details[oid] && store.details[oid].fare) return true;
        await page.waitForTimeout(300);
        await store.drain();
      }
      if (store.details[oid] && store.details[oid].fare) return true;
    }
  }
  return false;
}

async function clickDetails(page, context, store, input, timeoutMs) {
  if (!input.with_detail) return;
  const cards = await orderCards(page);
  if (!cards.length) {
    store.warnings.push('订单列表响应已捕获，但页面没有可点击的订单卡片；跳过了详情点击');
    return;
  }
  const ids = cards.map((card) => card.order_id).filter(Boolean);
  if (!ids.length) return;
  let limit = ids.length;
  if (input.max_details && input.max_details > 0) {
    limit = Math.min(limit, input.max_details);
    if (limit < ids.length) {
      store.warnings.push(`按 --max-details=${input.max_details} 只点击了前 ${limit} 单`);
    }
  }

  const skip = new Set(Array.isArray(input.skip_detail_ids) ? input.skip_detail_ids : []);
  for (let index = 0; index < limit; index += 1) {
    const oid = ids[index];
    if (skip.has(oid)) continue;
    const block = await currentBlock(page);
    if (block) throw blockError(block);
    await restoreOrderList(page, store, input.url, timeoutMs);

    const currentCards = await orderCards(page);
    const byId = {};
    for (const card of currentCards) if (card.order_id) byId[card.order_id] = card;
    const info = byId[oid];
    let cardIndex = info ? info.index : null;
    if (cardIndex === null || cardIndex === undefined || cardIndex >= currentCards.length) {
      cardIndex = index < currentCards.length ? index : null;
    }
    if (cardIndex === null) {
      store.warnings.push(`找不到订单 ${oid} 的卡片，跳过详情`);
      continue;
    }

    const newPages = [];
    const onPage = (newPage) => {
      newPages.push(newPage);
      store.attach(newPage);
    };
    context.on('page', onPage);
    store.currentOid = oid;
    try {
      const card = page.locator('.order-item').nth(cardIndex);
      const clicked = await safeClick(page, card);
      if (!clicked) {
        store.warnings.push(`订单 ${oid} 的卡片点击失败，跳过详情`);
        continue;
      }
      let detailPage = page;
      let routeSeen = false;
      const deadline = Date.now() + 5000;
      while (Date.now() < deadline) {
        const detailBlock = await currentBlock(page);
        if (detailBlock) throw blockError(detailBlock);
        if (newPages.length) {
          detailPage = newPages[newPages.length - 1];
          try {
            await detailPage.waitForLoadState('domcontentloaded', { timeout: 3000 });
          } catch (err) { /* ignore */ }
          routeSeen = true;
          break;
        }
        if ((page.url() || '').includes('#/gulfstream-lite')) { routeSeen = true; break; }
        await page.waitForTimeout(250);
      }
      if (!routeSeen) {
        store.warnings.push(`订单 ${oid} 未观察到详情页路由（#/gulfstream-lite）`);
      }
      await waitForDetail(detailPage, store, oid, timeoutMs, input.with_fare);
      if (input.with_fare && !(store.details[oid] && store.details[oid].fare)) {
        // The detail page already shows the paid amount ("13.9元"); reading it
        // avoids one extra click/XHR per trip. Fall back to the bill sheet.
        const domFare = await readDomFare(detailPage);
        if (domFare) {
          store.domFares[oid] = domFare;
        } else {
          const fareOk = await openFareDetail(detailPage, store, oid, timeoutMs);
          if (!fareOk) store.warnings.push(`订单 ${oid} 未捕获到费用明细`);
        }
      }
      if (newPages.length) {
        try { await detailPage.close(); } catch (err) { /* ignore */ }
      } else {
        await restoreOrderList(page, store, input.url, timeoutMs);
      }
    } finally {
      try { context.removeListener('page', onPage); } catch (err) { /* ignore */ }
    }
    await page.waitForTimeout(250);
    await store.drain();
  }
}

async function openBillSheet(page) {
  const note = (msg) => { try { const s = globalThis.__osNotes || (globalThis.__osNotes = []); s.push(msg); } catch (e) {} };
  // One real UI interaction is required: the pay signer only accepts
  // pGetBillDetail while the fee sheet is open. Read-only.
  let cards = [];
  try {
    cards = await page.evaluate(() => [...document.querySelectorAll('.order-item')]
      .map((el, index) => ({ index, text: el.innerText || '' })));
  } catch (err) { note('cards-eval-failed'); return false; }
  if (!cards.length) { note('cards-empty'); return false; }
  const candidates = cards.filter((card) => /已完成/.test(card.text)).slice(0, 8);
  if (!candidates.length) { note('no-completed-card'); return false; }
  for (const card of candidates) {
    try {
      await page.locator('.order-item').nth(card.index).click({ position: { x: 60, y: 30 }, timeout: 8000 });
      let link = null;
      const deadline = Date.now() + 15000;
      while (Date.now() < deadline && !link) {
        for (const hint of ['查看明细', '费用明细']) {
          const loc = page.getByText(hint, { exact: false });
          if ((await loc.count()) > 0) { link = loc.first(); break; }
        }
        if (!link) await page.waitForTimeout(500);
      }
      if (!link) { note('no-detail-link'); }
      if (link) {
        page.__sawBill = false;
        await link.click({ timeout: 5000 });
        const waitBill = Date.now() + 10000;
        while (Date.now() < waitBill && !page.__sawBill) {
          await page.waitForTimeout(400);
        }
        if (page.__sawBill) return true;
        const text = await page.evaluate(() => (document.body.innerText || '').slice(0, 600));
        if (/合计|车费明细|实付|支付金额/.test(text)) return true;
        note('no-bill-xhr');
      }
    } catch (err) { /* try the next card */ }
    try { await page.goBack({ waitUntil: 'domcontentloaded', timeout: 15000 }); } catch (err) { /* ignore */ }
    await page.waitForTimeout(1000);
  }
  return false;
}

async function apiFetch(page, store, input, timeoutMs) {
  // Fast path: the page's own signed axios instance ($http) is used to call the
  // same endpoints the UI uses. The site's SDK still computes wsgsig; we never
  // implement or replay it. No clicking, no page navigation.
  const first = store.listCaptures[0];
  if (!first || !first.response) return false;
  try {
    await page.waitForSelector('.order-item', { timeout: 15000 });
  } catch (err) {
    return false;
  }
  const sheet = await openBillSheet(page);
  if (!sheet) {
    try { store.warnings.push('API 模式未启用: ' + JSON.stringify(globalThis.__osNotes || [])); } catch (e) {}
    try { await page.waitForSelector('.order-item', { timeout: 10000 }); } catch (err) { /* ignore */ }
    return false;
  }
  const maxMonths = Math.max(1, Number(input.max_pages) || 50);
  const result = await page.evaluate(async ({ tpl, maxMonths, firstTimemode, firstResponse }) => {
    const vm = document.querySelector('#app').__vue__;
    if (!vm || !vm.$http) return { ok: false, reason: 'no-$http' };
    const url = new URL(tpl);
    const common = {};
    for (const [k, v] of url.searchParams.entries()) common[k] = v;
    delete common.wsgsig;
    delete common.sign;
    const histUrl = url.origin + url.pathname;
    const pages = [firstResponse];
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

    let cursor = firstTimemode || '';
    let guard = 0;
    while (cursor && guard < maxMonths) {
      guard += 1;
      let data = null;
      try {
        const resp = await vm.$http.get(histUrl, {
          params: Object.assign({}, common, { timemode: cursor }),
          timeout: 15000,
        });
        data = resp.data;
      } catch (err) { break; }
      if (!data || (data.errno !== 0 && data.errno !== '0')) break;
      pages.push(data);
      const next = data.timemode || '';
      if (!data.havenext || !next || next === cursor) break;
      cursor = next;
      await sleep(250);
    }

    const ids = [];
    const seen = new Set();
    for (const p of pages) {
      for (const key of ['order_done', 'order_waiting']) {
        for (const item of (p[key] || [])) {
          const oid = item.orderId || item.oid || item.order_id || '';
          if (oid && !seen.has(oid)) { seen.add(oid); ids.push(oid); }
        }
      }
    }

    const billUrl = 'https://pay.udache.com/gulfstream/pay/v1/client/pGetBillDetail';
    const fares = {};
    const errors = {};
    let index = 0;
    const worker = async () => {
      while (index < ids.length) {
        const oid = ids[index];
        index += 1;
        try {
          const resp = await vm.$http.get(billUrl, {
            params: Object.assign({}, common, { oid }),
            timeout: 12000,
          });
          const d = resp.data || {};
          const items = d.data && d.data.bill_detail && d.data.bill_detail.items;
          const item = items && items[0];
          const fee = item && item.real_pay && item.real_pay.bill_total_fee;
          if (fee !== undefined && fee !== null && fee !== '') fares[oid] = String(fee);
        } catch (err) {
          const code = err && err.response ? String(err.response.status) : 'network';
          errors[code] = (errors[code] || 0) + 1;
        }
        await sleep(350);
      }
    };
    await Promise.all([worker()]);
    return { ok: true, pages, ids, fares, errors };
  }, {
    tpl: first.url,
    maxMonths,
    firstTimemode: (first.response && first.response.timemode) || '',
    firstResponse: first.response,
  });

  if (!result || !result.ok) return false;
  // Replace list captures with the API pages (the first one is already there).
  store.listCaptures = [];
  store.captures = store.captures.filter((c) => c.kind !== 'order_list');
  for (const payload of result.pages) {
    const capture = {
      kind: 'order_list',
      url: 'https://common.diditaxi.com.cn/passenger/history',
      status: 200,
      response: payload,
      order_id: '',
      page: 0,
      timemode: payload.timemode || '',
    };
    store.captures.push(capture);
    store.listCaptures.push(capture);
  }
  for (const [oid, fee] of Object.entries(result.fares || {})) {
    store.domFares[oid] = fee;
  }
  if (result.errors && Object.keys(result.errors).length) {
    store.warnings.push('金额接口错误统计: ' + JSON.stringify(result.errors));
  }
  const missing = (result.ids || []).length - Object.keys(result.fares || {}).length;
  if (missing > 0) {
    store.warnings.push(`有 ${missing} 单没有取到金额（已关闭/无账单），已跳过`);
  }
  return true;
}

async function main(input) {
  let chromium = null;
  try {
    ({ chromium } = require('playwright'));
  } catch (err) {
    throw new Blocked('browser', '找不到 Node Playwright 模块',
      '请先自行安装 playwright；本工具不会自动下载');
  }
  let browser = null;
  let context = null;
  let persistent = false;
  const store = new CaptureStore();
  const timeoutMs = Number(input.timeout_ms) || 30000;
  try {
    const launch = { headless: input.headless !== false };
    if (input.executable_path) launch.executablePath = input.executable_path;
    const contextOptions = {
      locale: 'zh-CN',
      timezoneId: 'Asia/Shanghai',
      viewport: { width: 1280, height: 1000 },
    };
    try {
      if (input.profile_dir) {
        // Owner-owned profile: cookies + localStorage + IndexedDB persist here,
        // so the CLI never needs to read browser again (and no Keychain prompt).
        fs.mkdirSync(input.profile_dir, { recursive: true, mode: 0o700 });
        context = await chromium.launchPersistentContext(
          input.profile_dir, Object.assign({}, contextOptions, launch));
        persistent = true;
      } else {
        browser = await chromium.launch(launch);
        context = await browser.newContext(contextOptions);
      }
    } catch (err) {
      throw new Blocked('browser', 'Playwright Chromium 启动失败',
        `Chromium 可能损坏或缺少依赖；本工具不会自动下载。错误类型: ${err.name || 'Error'}`);
    }
    if (!persistent) {
      let added = 0;
      for (const cookie of (input.cookies || [])) {
        try {
          await context.addCookies([cookie]);
          added += 1;
        } catch (err) { /* skip incompatible cookie */ }
      }
      if (!added) {
        throw new Blocked('auth', '没有把滴滴 Cookie 注入浏览器（Cookie 可能格式不兼容）',
          '请确认浏览器中仍登录滴滴；本工具不会打印 Cookie 值');
      }
    }
    const page = await context.newPage();
    page.__sawBill = false;
    page.on('response', (response) => {
      try { if (response.url().includes('pGetBillDetail')) page.__sawBill = true; } catch (err) { /* ignore */ }
    });
    store.attach(page);
    try {
      await page.goto(input.url, { waitUntil: 'domcontentloaded', timeout: Math.max(20000, timeoutMs) });
    } catch (err) {
      throw new Blocked('timeout', `打开滴滴订单页失败: ${err.name || 'Error'}`,
        '可检查网络后重试');
    }
    const initialBlock = await currentBlock(page);
    if (initialBlock) throw blockError(initialBlock);
    await dismissModals(page);
    await waitFirstList(page, store, timeoutMs);

    let blocked = null;
    let apiDone = false;
    if (input.api_mode === 'api') {
      try {
        apiDone = await apiFetch(page, store, input, timeoutMs);
      } catch (err) {
        apiDone = false;
      }
    }
    const monthLimit = Math.max(1, Number(input.max_pages) || 50);
    const skipMonths = Math.max(0, Number(input.skip_months) || 0);
    for (let skip = 0; skip < skipMonths; skip += 1) {
      let advanced = false;
      try { advanced = await paginateOne(page, store, timeoutMs); }
      catch (err) { if (err instanceof Blocked) blocked = { kind: err.kind, message: err.message }; break; }
      if (!advanced) { store.warnings.push(`跳过前 ${skip} 个月后找不到更早的月份`); break; }
    }
    // The list renders one month at a time, so details are clicked right after
    // that month is loaded; only then do we advance to the previous month.
    for (let month = 0; !apiDone && month < monthLimit; month += 1) {
      const block = await currentBlock(page);
      if (block) { blocked = { kind: block[0], message: block[1] }; break; }
      if (input.with_detail) {
        // The list is virtualised: after jumping months, scroll to the bottom so
        // the newly loaded (older) month's cards are actually rendered before
        // we try to click them.
        try {
          await page.evaluate(() => {
            const els = [document.scrollingElement, ...document.querySelectorAll('*')];
            for (const el of els) {
              if (!el) continue;
              if (el.scrollHeight > el.clientHeight + 40) el.scrollTop = el.scrollHeight;
            }
            window.scrollTo(0, document.body.scrollHeight);
          });
          await page.mouse.move(620, 520);
          await page.mouse.wheel(0, 24000);
        } catch (err) { /* ignore */ }
        await page.waitForTimeout(1600);
        try {
          await clickDetails(page, context, store, input, timeoutMs);
        } catch (err) {
          if (err instanceof Blocked) { blocked = { kind: err.kind, message: err.message }; break; }
          throw err;
        }
      }
      let advanced = false;
      try {
        advanced = await paginateOne(page, store, timeoutMs);
      } catch (err) {
        if (err instanceof Blocked) { blocked = { kind: err.kind, message: err.message }; break; }
        throw err;
      }
      if (!advanced) break;
    }
    await store.drain();
    let cookiesAfter = [];
    try { cookiesAfter = await context.cookies(); } catch (err) { /* ignore */ }
    return {
      ok: true,
      engine: 'node-playwright',
      final_url: page.url(),
      cookies_after: cookiesAfter,
      captures: store.captures,
      details: store.details,
      dom_fares: store.domFares,
      dom_orders: [],
      warnings: store.warnings,
      blocked,
    };
  } finally {
    if (context) {
      try { await context.close(); } catch (err) { /* ignore */ }
    }
    if (browser) {
      try { await browser.close(); } catch (err) { /* ignore */ }
    }
  }
}

const input = readInput();
main(input)
  .then((result) => {
    process.stdout.write(JSON.stringify(result));
  })
  .catch((err) => {
    const type = err instanceof Blocked ? err.kind : 'timeout';
    const payload = {
      ok: false,
      error: {
        type,
        message: (err && err.message) ? String(err.message) : '浏览器驱动失败',
        hint: (err && err.hint) ? String(err.hint) : '',
      },
    };
    process.stdout.write(JSON.stringify(payload));
    process.exitCode = 0;
  });
