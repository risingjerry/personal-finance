'use strict';
// `alipay bills list` / `alipay bills summary`
const { loadBills } = require('../store');
const { listBills, summarize, normalizeFilters } = require('../bills');

function storeMeta(ctx, records, badLines) {
  const L = require('../util').layout(ctx.home);
  return { home: ctx.home, dataFile: L.bills, storeRecords: records.length, corruptLines: badLines };
}

function filtersFromFlags(flags) {
  return { from: flags.from, to: flags.to, direction: flags.direction, q: flags.q, min: flags.min, max: flags.max };
}

async function list(ctx) {
  const { records, badLines } = loadBills(ctx.home);
  const filters = normalizeFilters(filtersFromFlags(ctx.flags));
  const limit = ctx.flags.limit === undefined ? 50 : Number(ctx.flags.limit);
  const offset = ctx.flags.offset === undefined ? 0 : Number(ctx.flags.offset);
  if (!Number.isFinite(limit) || limit < 0) throw require('../errors').usage('--limit must be >= 0');
  if (!Number.isFinite(offset) || offset < 0) throw require('../errors').usage('--offset must be >= 0');
  const order = ctx.flags.order || 'desc';
  if (order !== 'asc' && order !== 'desc') throw require('../errors').usage('--order must be asc or desc');
  const res = listBills(records, { filters, limit, offset, order });
  return {
    cmd: 'bills.list',
    data: res,
    meta: {
      ...storeMeta(ctx, records, badLines),
      filters: {
        from: ctx.flags.from ?? null,
        to: ctx.flags.to ?? null,
        direction: ctx.flags.direction ?? null,
        q: ctx.flags.q ?? null,
        min: ctx.flags.min ?? null,
        max: ctx.flags.max ?? null,
        order,
      },
    },
  };
}

async function summary(ctx) {
  const { records, badLines } = loadBills(ctx.home);
  const by = ctx.flags.by || 'year';
  const top = ctx.flags.top === undefined ? 0 : Number(ctx.flags.top);
  const res = summarize(records, { by, filters: filtersFromFlags(ctx.flags), top });
  return {
    cmd: 'bills.summary',
    data: res,
    meta: {
      ...storeMeta(ctx, records, badLines),
      filters: { from: ctx.flags.from ?? null, to: ctx.flags.to ?? null, direction: ctx.flags.direction ?? null, q: ctx.flags.q ?? null },
    },
  };
}

module.exports = { list, summary };
