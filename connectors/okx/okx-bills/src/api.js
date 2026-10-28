'use strict';
// OKX C2C (P2P) order API access.
//
// Why this shape: /v4/c2c/order/getOrderList only answers with the browser's
// full header set (Authorization JWT + X-Request-Timestamp + X-Client-Signature
// + Devid ...). A bare in-page fetch is rejected with 403. So we (1) let the
// real page issue one request and record its headers, then (2) replay that
// header set for our own page/pageIndex/date-range queries. The signature is
// not bound to the query string (verified), but it does expire, so a 403 simply
// means "reload the page and capture a fresh template".
const { CliError, codes } = require('./errors');
const { sleep } = require('./util');

const ORIGIN = 'https://www.okx.com';
const DEPOSIT_API = '/v2/asset/deposit/list-new';
const WITHDRAW_API = '/v2/asset/withdraw/list';
const ORDERS_PAGE = `${ORIGIN}/zh-hans/p2p/orders-completed`;
const ORDERS_API = '/v4/c2c/order/getOrderList';
// Headers the browser refuses to send (or that must stay the browser's own).
const DROP_HEADERS = new Set([
  'host', 'content-length', 'content-type', 'accept-encoding', 'connection',
  'user-agent', 'referer', 'origin', 'cookie', 'sec-ch-ua', 'sec-ch-ua-mobile',
  'sec-ch-ua-platform', 'sec-fetch-site', 'sec-fetch-mode', 'sec-fetch-dest',
  'if-none-match', 'if-modified-since',
]);

function cleanHeaders(headers) {
  const out = {};
  for (const [k, v] of Object.entries(headers || {})) {
    if (DROP_HEADERS.has(k.toLowerCase())) continue;
    out[k] = v;
  }
  return out;
}

async function captureTemplate(page, { timeoutMs = 40000, log = () => {} } = {}) {
  const conn = page.conn;
  const found = new Promise((resolve) => {
    const off = conn.on((msg) => {
      if (msg.method === 'Network.requestWillBeSent') {
        const url = msg.params?.request?.url || '';
        if (url.includes(ORDERS_API) && (!page.sessionId || msg.sessionId === page.sessionId)) {
          off();
          resolve(msg.params.request);
        }
      }
    });
    setTimeout(() => { off(); resolve(null); }, timeoutMs);
  });
  await page.send('Network.enable').catch(() => {});
  log('opening C2C order page to capture request headers');
  await page.navigate(ORDERS_PAGE);
  const req = await found;
  if (!req) throw new CliError(codes.NEED_LOGIN, 'order list request never fired (login required or page changed)', { url: ORDERS_PAGE });
  return { method: req.method, url: req.url, headers: cleanHeaders(req.headers), capturedAt: new Date().toISOString() };
}

async function callApi(page, template, pathAndQuery, { timeoutMs = 45000 } = {}) {
  const expr = `(async () => {
    try {
      const res = await fetch(${JSON.stringify(ORIGIN + pathAndQuery)}, {
        method: ${JSON.stringify(template.method || 'GET')},
        credentials: 'include',
        headers: ${JSON.stringify(template.headers)},
      });
      const text = await res.text();
      return JSON.stringify({ status: res.status, body: text });
    } catch (e) {
      return JSON.stringify({ status: 0, error: String(e && e.message || e) });
    }
  })()`;
  const raw = await page.evaluate(expr, { timeoutMs });
  if (typeof raw !== 'string') throw new CliError(codes.INTERNAL, 'unexpected fetch result');
  let parsed;
  try { parsed = JSON.parse(raw); } catch { throw new CliError(codes.INTERNAL, 'unreadable fetch result'); }
  if (parsed.error) throw new CliError(codes.BROWSER_DOWN, `in-page fetch failed: ${parsed.error}`, { path: pathAndQuery });
  let body = null;
  try { body = JSON.parse(parsed.body); } catch { /* non-json */ }
  return { status: parsed.status, body, text: parsed.body };
}

/**
 * Fetch every order in a window, following pagination.
 * @returns {Promise<{items:object[], pages:number, total:number, capturedAt:string}>}
 */
async function fetchOrders(page, template, {
  fromMs, toMs, orderType = 'all', pageSize = 100, maxPages = 60, log = () => {}, throttleMs = 600,
} = {}) {
  const items = [];
  const seen = new Set();
  let pageIndex = 1;
  let pageCount = 1;
  let total = 0;
  while (pageIndex <= Math.min(pageCount, maxPages)) {
    const q = new URLSearchParams({
      orderType,
      startTime: String(fromMs),
      endTime: String(toMs),
      pageSize: String(pageSize),
      pageIndex: String(pageIndex),
      t: String(Date.now()),
    });
    const { status, body } = await callApi(page, template, `${ORDERS_API}?${q.toString()}`);
    if (status !== 200 || !body || body.code !== 0) {
      throw new CliError(status === 403 ? codes.API_FORBIDDEN : codes.INTERNAL, `order list rejected (HTTP ${status}, code ${body && body.code})`, {
        status, code: body && body.code, msg: body && (body.msg || body.error_message),
      });
    }
    const data = body.data || {};
    total = data.totalItemCount ?? total;
    pageCount = data.pageCount ?? pageCount;
    for (const it of data.items || []) {
      if (!it || it.id == null) continue;
      const key = String(it.id);
      if (seen.has(key)) continue;
      seen.add(key);
      items.push(it);
    }
    log(`page ${pageIndex}/${pageCount}: +${(data.items || []).length} items (total ${total})`);
    pageIndex += 1;
    if (pageIndex <= pageCount) await sleep(throttleMs);
  }
  return { items, pages: pageCount, total, capturedAt: new Date().toISOString() };
}

/**
 * Paginate an asset-history endpoint (deposits / withdrawals), which uses
 * currentPage / pageLength / total and returns its rows under a named key.
 */
async function fetchPaged(page, template, apiPath, {
  rowsKey, pageLength = 100, maxPages = 100, log = () => {}, throttleMs = 500, extraParams = {},
} = {}) {
  const rows = [];
  const seen = new Set();
  let currentPage = 1;
  let total = 0;
  let guard = 0;
  while (guard < maxPages) {
    guard += 1;
    const q = new URLSearchParams({
      currencyId: '-1', currentPage: String(currentPage), pageLength: String(pageLength),
      ...extraParams, t: String(Date.now()),
    });
    const { status, body } = await callApi(page, template, `${apiPath}?${q.toString()}`);
    if (status !== 200 || !body || body.code !== 0) {
      throw new CliError(status === 403 ? codes.API_FORBIDDEN : codes.INTERNAL, `asset history rejected (HTTP ${status}, code ${body && body.code})`, {
        status, code: body && body.code, msg: body && (body.msg || body.error_message), apiPath,
      });
    }
    const data = body.data || {};
    total = Number(data.total ?? total);
    const batch = data[rowsKey] || [];
    for (const it of batch) {
      const key = String(it.id ?? it.depositId ?? it.txid);
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push(it);
    }
    log(`page ${currentPage}: +${batch.length} rows (total ${total})`);
    if (!batch.length || rows.length >= total) break;
    currentPage += 1;
    await sleep(throttleMs);
  }
  return { items: rows, total: Math.max(total, rows.length) };
}

module.exports = {
  ORDERS_PAGE, ORDERS_API, DEPOSIT_API, WITHDRAW_API, ORIGIN,
  captureTemplate, callApi, fetchOrders, fetchPaged, cleanHeaders,
};
