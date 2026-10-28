'use strict';
// `okx sync`: pull C2C orders for a window into the local store.
const path = require('node:path');
const { layout, ensureLayout, parseDateStart, parseDateEnd, writeFileAtomic, readJson, nowIso, sha256, safeUrl } = require('./util');
const { CliError, codes } = require('./errors');
const session = require('./session');
const api = require('./api');
const orders = require('./orders');
const flows = require('./flows');

const DEFAULT_FROM = Date.UTC(2017, 0, 1);

async function runSync({ browser, home, flags, log }) {
  const L = ensureLayout(home);
  const fromMs = parseDateStart(flags.from) ?? DEFAULT_FROM;
  const toMs = parseDateEnd(flags.to) ?? Date.now();
  if (fromMs > toMs) throw new CliError(codes.BAD_INPUT, '--from must be before --to');

  const ready = await session.ensureReady({ browser, log, refreshTemplate: !!flags['refresh-template'] });
  log(`session ok (${safeUrl(ready.check.url)})`);

  if (flags.only === 'flows') {
    const flowsResult = await runFlowsSync({ page: ready.page, template: ready.template, home, log, flags });
    return { flows: flowsResult };
  }

  const windowSpec = {
    fromMs, toMs,
    orderType: flags['order-type'] || 'all',
    pageSize: Number(flags['page-size'] || 100),
    log,
  };

  let result;
  try {
    result = await api.fetchOrders(ready.page, ready.template, windowSpec);
  } catch (err) {
    // A stale signature is the normal failure mode; recapture headers and retry once.
    if (err.code === codes.API_FORBIDDEN || /code 403/.test(String(err.message))) {
      log('order list refused (signature expired?) — recapturing request headers');
      ready.template = await api.captureTemplate(ready.page, { log });
      writeFileAtomic(L.template, JSON.stringify(ready.template, null, 1), 0o600);
      result = await api.fetchOrders(ready.page, ready.template, windowSpec);
    } else {
      throw err;
    }
  }

  const normalized = result.items.map(orders.normalize);
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const rawFile = path.join(L.raw, `orders-${stamp}.json`);
  const rawBody = JSON.stringify({ capturedAt: result.capturedAt, window: { fromMs, toMs }, items: result.items });
  writeFileAtomic(rawFile, rawBody, 0o600);

  const { added, updated, total } = orders.upsertOrders(home, normalized);
  const meta = readJson(L.meta, {}) || {};
  writeFileAtomic(L.meta, JSON.stringify({
    ...meta,
    lastSyncAt: nowIso(),
    lastSyncWindow: { fromMs, toMs },
    lastSyncCounts: { fetched: normalized.length, added, updated, stored: total },
    lastRawFile: rawFile,
  }, null, 1), 0o600);

  let flowsResult = null;
  if (String(flags.only || 'all') !== 'c2c') {
    flowsResult = await runFlowsSync({ page: ready.page, template: ready.template, home, log, flags });
  }

  return {
    fetched: normalized.length,
    added,
    updated,
    stored: total,
    pages: result.pages,
    apiTotal: result.total,
    window: { from: new Date(fromMs).toISOString(), to: new Date(toMs).toISOString() },
    raw: { file: rawFile, bytes: Buffer.byteLength(rawBody), sha256: sha256(rawBody) },
    store: L.orders,
    flows: flowsResult,
  };
}

/**
 * Sync asset flows (deposits + withdrawals). Pagination is currentPage/pageLength.
 */
async function runFlowsSync({ page, template, home, log, flags }) {
  const L = ensureLayout(home);
  const capture = async (apiPath, rowsKey, extraParams) => {
    try {
      return await api.fetchPaged(page, template, apiPath, { rowsKey, extraParams, log });
    } catch (err) {
      if (err.code === codes.API_FORBIDDEN) {
        log(`asset history refused (signature expired?) — recapturing request headers`);
        const fresh = await api.captureTemplate(page, { log });
        writeFileAtomic(L.template, JSON.stringify(fresh, null, 1), 0o600);
        Object.assign(template, fresh);
        return api.fetchPaged(page, template, apiPath, { rowsKey, extraParams, log });
      }
      throw err;
    }
  };
  const dep = await capture(api.DEPOSIT_API, 'rechargeHistory', {});
  const wd = await capture(api.WITHDRAW_API, 'withdrawHistory', { invalidAsset: 'false' });
  const normalized = [
    ...dep.items.map(flows.normalizeDeposit),
    ...wd.items.map(flows.normalizeWithdraw),
  ];
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const rawFile = path.join(L.raw, `flows-${stamp}.json`);
  const rawBody = JSON.stringify({ capturedAt: new Date().toISOString(), deposits: dep.items, withdrawals: wd.items });
  writeFileAtomic(rawFile, rawBody, 0o600);
  const { added, updated, total } = flows.upsertFlows(home, normalized);
  return {
    fetched: normalized.length,
    deposits: dep.items.length,
    withdrawals: wd.items.length,
    added, updated, stored: total,
    raw: { file: rawFile, bytes: Buffer.byteLength(rawBody), sha256: sha256(rawBody) },
    store: L.flows,
  };
}

module.exports = { runSync, runFlowsSync, DEFAULT_FROM };
