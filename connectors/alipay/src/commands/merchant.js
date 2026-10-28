'use strict';
// `alipay merchant ...` — business-account bills through the official
// open-platform APIs (no browser, no web quota). Storage stays separate from the
// personal bills on purpose.
const { CliError, codes } = require('../errors');
const { loadAll, loadKind, readCoverage, merchantLayout } = require('../merchant/store');
const { syncBills, queryOnline, detail } = require('../merchant/engine');
const { readConfig } = require('../merchant/client');
const { ensureDir } = require('../util');

function moneyAdd(a, b) {
  return (Number(a) + Number(b)).toFixed(2);
}

async function status(ctx) {
  const M = merchantLayout(ctx.home);
  const coverage = readCoverage(ctx.home);
  const funds = loadKind(ctx.home, 'funds');
  const trades = loadKind(ctx.home, 'trade');
  let config = null;
  try {
    const c = readConfig();
    config = { app_id: c.appId, config_file: c.file };
  } catch (err) {
    config = { error: err.message };
  }
  const counts = Object.entries(coverage).reduce((acc, [, v]) => { acc[v.status] = (acc[v.status] || 0) + 1; return acc; }, {});
  const span = (records) => (records.length ? { first: records[0].occurred_at, last: records[records.length - 1].occurred_at } : null);
  return {
    cmd: 'merchant.status',
    data: {
      engine: 'native (openapi.alipay.com, RSA2)',
      home: M.dir,
      config,
      funds: { records: funds.records.length, ...span(funds.records) },
      trades: { records: trades.records.length, ...span(trades.records) },
      coverage: { periods: Object.keys(coverage).length, byStatus: counts },
    },
  };
}

async function sync(ctx) {
  const { from, to, kind = 'both', refresh, concurrency } = ctx.flags;
  const kinds = kind === 'both' ? ['signcustomer', 'trade'] : [String(kind)];
  if (kinds.some((k) => !['signcustomer', 'trade'].includes(k))) throw new CliError(codes.BAD_INPUT, `unsupported --kind: ${kind}`);
  ensureDir(merchantLayout(ctx.home).dir, 0o700);
  const res = await syncBills({
    home: ctx.home,
    from: String(from),
    to: String(to),
    kinds,
    refresh: !!refresh,
    concurrency: Number(concurrency) || 4,
    log: ctx.log,
  });
  return { cmd: 'merchant.sync', data: res };
}

function toCents(s) {
  const n = Number(s);
  return Number.isFinite(n) ? Math.round(n * 100) : 0;
}

async function list(ctx) {
  const { from, to, kind, q, limit = 50, offset = 0 } = ctx.flags;
  const all = kind === 'trade' ? loadKind(ctx.home, 'trade').records : kind === 'funds' ? loadKind(ctx.home, 'funds').records : [...loadKind(ctx.home, 'funds').records, ...loadKind(ctx.home, 'trade').records];
  const text = q ? String(q).toLowerCase() : null;
  const rows = all
    .filter((r) => (!from || r.occurred_at.slice(0, 10) >= String(from)) && (!to || r.occurred_at.slice(0, 10) < String(to)))
    .filter((r) => !text || JSON.stringify(r.raw || r).toLowerCase().includes(text))
    .sort((a, b) => (a.occurred_at < b.occurred_at ? 1 : -1));
  const lim = Math.max(1, Number(limit) || 50);
  const off = Math.max(0, Number(offset) || 0);
  return {
    cmd: 'merchant.list',
    data: { total: rows.length, returned: rows.slice(off, off + lim).length, offset: off, limit: lim, records: rows.slice(off, off + lim) },
  };
}

async function summary(ctx) {
  const { from, to, by = 'month' } = ctx.flags;
  const all = [...loadKind(ctx.home, 'funds').records, ...loadKind(ctx.home, 'trade').records]
    .filter((r) => (!from || r.occurred_at.slice(0, 10) >= String(from)) && (!to || r.occurred_at.slice(0, 10) < String(to)));
  const keyOf = (r) => {
    if (by === 'day') return r.occurred_at.slice(0, 10);
    if (by === 'month') return r.occurred_at.slice(0, 7);
    if (by === 'counterparty') return r.counterparty || '(unknown)';
    if (by === 'type') return r.type || '(unknown)';
    return String(r.occurred_at.slice(0, 4));
  };
  const groups = new Map();
  for (const r of all) {
    const key = keyOf(r);
    const g = groups.get(key) || { key, count: 0, income: '0.00', expense: '0.00' };
    g.count++;
    if (r.kind === 'funds') {
      g.income = moneyAdd(g.income, r.income);
      g.expense = moneyAdd(g.expense, r.expense);
    }
    groups.set(key, g);
  }
  const out = [...groups.values()].sort((a, b) => (a.key < b.key ? 1 : -1));
  return { cmd: 'merchant.summary', data: { by, groups: out, totalGroups: out.length } };
}

async function balance(ctx) {
  const res = await queryOnline({ kind: 'balance' });
  return { cmd: 'merchant.balance', data: res };
}

async function query(ctx) {
  const { kind = 'sell', from, to } = ctx.flags;
  if (!['sell', 'transfer', 'deposit', 'withdraw'].includes(String(kind))) throw new CliError(codes.BAD_INPUT, `unsupported --kind: ${kind}`);
  const res = await queryOnline({ kind: String(kind), from: String(from), to: String(to) });
  return { cmd: 'merchant.query', data: res };
}

async function trade(ctx) {
  const res = await detail({ tradeNo: ctx.flags['trade-no'], refundRequestNo: ctx.flags['refund-no'] });
  return { cmd: 'merchant.trade', data: res };
}

module.exports = { status, sync, list, summary, balance, query, trade };
