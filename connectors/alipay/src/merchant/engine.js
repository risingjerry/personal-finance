'use strict';
// Business-account operations on top of the gateway client: bill archive sync
// (month-first with daily fallback), online paginated queries, and lookups.
const { GatewayClient } = require('./client');
const { parseBillZip, planPeriods, periodBounds } = require('./bills');
const { mergeKind, saveRaw, setCoverage, readCoverage } = require('./store');
const { CliError, codes } = require('../errors');

const BILL = 'alipay.data.dataservice.bill.downloadurl.query';
const METHOD = {
  balance: 'alipay.data.bill.balance.query',
  sell: 'alipay.data.bill.sell.query',
  transfer: 'alipay.data.bill.transfer.query',
  trade: 'alipay.trade.query',
  refund: 'alipay.trade.fastpay.refund.query',
};

function classifyBill(result) {
  if (result.code === '10000') return 'downloaded';
  const sub = String(result.sub_code || '');
  if (/bill_not_exist/i.test(sub)) return 'no_bill';
  if (sub === 'BILL_DATE_BEFORE_REGISTRATION') return 'before_registration';
  if (sub === 'TYPE_NOT_SUPPORTED') return 'unsupported';
  if (/PERMISSION|AUTH/i.test(sub)) return 'denied';
  return 'error';
}

async function syncPeriod({ home, client, kind, period, refresh, coverage }) {
  const key = `${kind}|${period}`;
  const prior = coverage[key];
  if (!refresh && prior && ['downloaded', 'no_bill', 'before_registration'].includes(prior.status)) {
    return { kind, period, status: 'cached', rows: prior.rows || 0 };
  }
  const result = await client.call(BILL, { bill_type: kind, bill_date: period });
  const status = classifyBill(result);
  if (status !== 'downloaded') {
    setCoverage(home, key, { status, code: result.code || '', sub_code: result.sub_code || '', rows: 0 });
    return { kind, period, status, code: result.code, sub_code: result.sub_code };
  }
  const buffer = await client.download(result.bill_download_url);
  const parsed = parseBillZip(buffer, kind);
  if (parsed.issues.length) {
    setCoverage(home, key, { status: 'error', issues: parsed.issues.slice(0, 3), rows: 0 });
    return { kind, period, status: 'error', issues: parsed.issues };
  }
  const raw = saveRaw(home, kind, period, buffer);
  const commit = mergeKind(home, parsed.rows[0] && parsed.rows[0].kind === 'trade' ? 'trade' : 'funds', parsed.rows);
  setCoverage(home, key, { status: 'downloaded', rows: parsed.rows.length, total: commit.total, raw });
  return { kind, period, status: 'downloaded', rows: parsed.rows.length, added: commit.added, updated: commit.updated };
}

async function pool(items, concurrency, worker) {
  const results = [];
  let index = 0;
  const runners = Array.from({ length: Math.max(1, concurrency) }, async () => {
    while (index < items.length) {
      const i = index++;
      results[i] = await worker(items[i]);
    }
  });
  await Promise.all(runners);
  return results;
}

/**
 * Archive bills for [from, to) in Beijing dates. `to` must not be later than
 * yesterday (offline bills are T+1). Monthly periods are tried first and fall
 * back to day-by-day when the provider says the period type is unsupported.
 */
async function syncBills({ home, client = new GatewayClient(), from, to, kinds = ['signcustomer', 'trade'], refresh = false, concurrency = 4, log = () => {}, today = new Date().toISOString().slice(0, 10) }) {
  if (!from || !to) throw new CliError(codes.BAD_INPUT, 'merchant sync needs --from and --to (Beijing dates, end exclusive)');
  const yesterday = new Date(Date.parse(`${today}T00:00:00Z`) - 86400000).toISOString().slice(0, 10);
  if (to > today) throw new CliError(codes.BAD_INPUT, `offline bills are T+1; --to must be ${today} or earlier`, { to, today });
  if (from >= to) throw new CliError(codes.BAD_INPUT, '--from must be earlier than --to');

  const lastDay = new Date(Date.parse(`${to}T00:00:00Z`) - 86400000).toISOString().slice(0, 10);
  const coverage = readCoverage(home);
  const tasks = [];
  for (const kind of kinds) {
    for (const period of planPeriods(from, lastDay, new Date(`${today}T00:00:00Z`))) tasks.push({ kind, period });
  }
  log(`merchant plan: ${tasks.length} task(s) (${kinds.join('/')}) ${from}..${lastDay}`);

  let done = 0;
  const outcome = [];
  await pool(tasks, concurrency, async (task) => {
    let res;
    try {
      res = await syncPeriod({ home, client, kind: task.kind, period: task.period, refresh, coverage });
    } catch (err) {
      res = { kind: task.kind, period: task.period, status: 'error', message: err.message };
    }
    // A monthly period the provider cannot produce is retried day by day.
    if (res.status === 'unsupported' && /^\d{4}-\d{2}$/.test(task.period)) {
      const b = periodBounds(task.period);
      const days = planPeriods(b.start, b.end, new Date(`${today}T00:00:00Z`));
      const parts = [];
      for (const day of days) {
        try {
          parts.push(await syncPeriod({ home, client, kind: task.kind, period: day, refresh, coverage }));
        } catch (err) {
          parts.push({ kind: task.kind, period: day, status: 'error', message: err.message });
        }
      }
      res = { kind: task.kind, period: task.period, status: 'daily_fallback', parts: parts.length, downloaded: parts.filter((p) => p.status === 'downloaded').length };
    }
    outcome.push(res);
    done++;
    if (done % 10 === 0 || done === tasks.length) log(`merchant sync: ${done}/${tasks.length}`);
    return res;
  });

  const summary = outcome.reduce((acc, r) => { acc[r.status] = (acc[r.status] || 0) + 1; return acc; }, {});
  const added = outcome.reduce((n, r) => n + (r.added || 0), 0);
  return { from, to, kinds, tasks: tasks.length, statuses: summary, added, results: outcome };
}

function parseRange(start, end, today = new Date().toISOString().slice(0, 10)) {
  if (!start || !end) throw new CliError(codes.BAD_INPUT, 'online queries need --from and --to (Beijing dates, [start,end), at most 31 days per window)');
  if (start >= end) throw new CliError(codes.BAD_INPUT, '--from must be earlier than --to');
  return { start, end };
}

/** One window of at most 31 days, all pages, with repeat/total guards. */
async function onlineWindow({ client, kind, start, end }) {
  const method = kind === 'sell' ? METHOD.sell : kind === 'transfer' || kind === 'deposit' || kind === 'withdraw' ? METHOD.transfer : null;
  if (!method) throw new CliError(codes.BAD_INPUT, `unsupported online kind: ${kind}`);
  const biz = { start_time: `${start} 00:00:00`, end_time: `${end} 00:00:00`, page_no: '1', page_size: '2000' };
  if (kind !== 'sell') biz.type = kind.toUpperCase();
  const rows = [];
  const seenPages = new Set();
  const seenItems = new Set();
  let total = null;
  for (;;) {
    const result = await client.call(method, biz);
    if (result.code !== '10000') {
      throw new CliError(codes.INTERNAL, `${kind} query failed`, { code: result.code, sub_code: result.sub_code, sub_msg: result.sub_msg });
    }
    const detail = Array.isArray(result.detail_list) ? result.detail_list : [];
    const digest = JSON.stringify(detail);
    if (detail.length && seenPages.has(digest)) throw new CliError(codes.INTERNAL, 'provider repeated a page; refusing an incomplete result');
    seenPages.add(digest);
    const reported = Number(result.total_size || 0);
    if (total !== null && total !== reported) throw new CliError(codes.INTERNAL, 'provider total changed during pagination');
    total = reported;
    for (const item of detail) {
      const identity = JSON.stringify(item);
      if (seenItems.has(identity)) throw new CliError(codes.INTERNAL, 'overlapping pages; refusing an incomplete result');
      seenItems.add(identity);
      rows.push(item);
    }
    if (rows.length >= total) break;
    if (!detail.length) throw new CliError(codes.INTERNAL, 'empty page before the reported total');
    biz.page_no = String(Number(biz.page_no) + 1);
  }
  return rows;
}

async function queryOnline({ client = new GatewayClient(), kind, from, to }) {
  if (kind === 'balance') {
    const result = await client.call(METHOD.balance, {});
    if (result.code !== '10000') throw new CliError(codes.INTERNAL, 'balance query failed', { sub_code: result.sub_code });
    return { kind, payload: result, fresh: true };
  }
  const { start, end } = parseRange(from, to);
  const days = (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86400000;
  if (days > 31) throw new CliError(codes.BAD_INPUT, 'online windows are limited to 31 days; pass a shorter range');
  const rows = await onlineWindow({ client, kind, start, end });
  return { kind, total: rows.length, records: rows, coverage: { start, end_exclusive: end } };
}

async function detail({ client = new GatewayClient(), tradeNo, refundRequestNo }) {
  if (!tradeNo) throw new CliError(codes.BAD_INPUT, '--trade-no is required');
  if (refundRequestNo) {
    const result = await client.call(METHOD.refund, { trade_no: tradeNo, out_request_no: refundRequestNo });
    return { found: result.code === '10000', payload: result };
  }
  const result = await client.call(METHOD.trade, { trade_no: tradeNo, query_options: ['fund_bill_list'] });
  return { found: result.code === '10000', payload: result };
}

module.exports = { syncBills, queryOnline, detail, classifyBill, METHOD, BILL };
