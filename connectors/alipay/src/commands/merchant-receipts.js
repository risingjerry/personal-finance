'use strict';
// Receipt evidence, not wallet fulfillment or an automatic identity/payment verdict.
const { CliError, codes } = require('../errors');
const { toCents, formatCents } = require('../money');
const { acquireLock } = require('../lock');
const { shanghaiToday, addDays } = require('../util');
const { syncBills, queryOnline } = require('../merchant/engine');
const { loadKind, readCoverage } = require('../merchant/store');

function date(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(Date.parse(value)) || new Date(value).toISOString().slice(0, 10) !== value) {
    throw new CliError(codes.BAD_INPUT, 'expected a valid Beijing date: YYYY-MM-DD');
  }
  return value;
}

async function run(ctx) {
  const day = shanghaiToday();
  const from = date(ctx.flags.from ?? addDays(day, -29));
  const to = date(ctx.flags.to ?? addDays(day, 1));
  if (from >= to || to > addDays(day, 1)) throw new CliError(codes.BAD_INPUT, 'require --from < --to <= tomorrow (end exclusive)');
  let amount = null;
  if (ctx.flags.amount !== undefined) {
    try { amount = toCents(ctx.flags.amount); } catch { throw new CliError(codes.BAD_INPUT, '--amount must be a positive decimal with at most 2 places'); }
    if (amount <= 0n) throw new CliError(codes.BAD_INPUT, '--amount must be positive');
  }
  const q = ctx.flags.q === undefined ? '' : String(ctx.flags.q).trim().toLowerCase();
  const archiveTo = to < day ? to : day;
  const lock = acquireLock(ctx.home, { cmd: 'merchant receipts' });
  try {
    // syncBills skips already covered periods. Do not use the date of the last
    // transaction as a coverage watermark: a fully checked day may have no rows.
    const before = loadKind(ctx.home, 'funds');
    // Do not let a merge silently drop corrupt evidence and then claim coverage.
    if (!before.badLines && from < archiveTo) await syncBills({ home: ctx.home, from, to: archiveTo, kinds: ['signcustomer'], concurrency: 1, today: day, log: ctx.log });
    const coverage = readCoverage(ctx.home);
    const missing = [];
    for (let d = from; d < archiveTo; d = addDays(d, 1)) {
      const covered = [coverage[`signcustomer|${d}`], coverage[`signcustomer|${d.slice(0, 7)}`]]
        .some((c) => c && c.status === 'downloaded');
      if (!covered) missing.push(d);
    }
    const store = loadKind(ctx.home, 'funds');
    const payerMatches = (payer) => !q || String(payer || '').toLowerCase().includes(q);
    const matches = store.records
      .filter((r) => r.occurred_at.slice(0, 10) >= from && r.occurred_at.slice(0, 10) < archiveTo)
      .filter((r) => toCents(r.income) > 0n && (amount === null || toCents(r.income) === amount) && payerMatches(r.counterparty))
      .sort((a, b) => b.occurred_at.localeCompare(a.occurred_at))
      .map((r) => ({ source: 'merchant_funds', id: r.id, paid_at: r.occurred_at, amount: r.income, payer: r.counterparty, type: r.type, remark: r.raw?.备注 || '', business_id: r.business_id }));
    let live = null;
    const warnings = [];
    if (missing.length || store.badLines) warnings.push('Archive coverage is incomplete; no match is not proof of nonpayment.');
    if (to > day) {
      live = { from: day, to_exclusive: to, complete: false, sells: null, transfers: null, errors: {} };
      // Preserve successful evidence even when another endpoint is unavailable.
      // Balance/deposit/withdraw queries cannot identify a customer's payment.
      for (const kind of ['sell', 'transfer']) {
        try {
          const { records } = await queryOnline({ kind, from: day, to });
          if (kind === 'sell') {
            live.latest_sell_at = records.reduce((latest, r) => r.gmt_pay > latest ? r.gmt_pay : latest, '');
            live.sells = records
              .filter((r) => r.trade_status === '成功' && toCents(r.total_amount) > 0n && (amount === null || toCents(r.total_amount) === amount) && payerMatches(r.other_account))
              .map((r) => ({ paid_at: r.gmt_pay, amount: r.total_amount, payer: r.other_account, order: r.merchant_order_no, title: r.goods_title, status: r.trade_status, refund_amount: r.refund_amount }));
          } else {
            live.transfers = records; // Raw evidence; direction/identity need verification.
          }
        } catch (err) {
          live.errors[kind] = err.message;
        }
      }
      warnings.push('Live sell/transfer APIs are not proven to cover all incoming friend transfers. Empty results do not prove nonpayment; today\'s funds bill is T+1.');
    }
    return { cmd: 'merchant.receipts', data: {
      from, to_exclusive: to, timezone: 'Asia/Shanghai', q, amount: amount === null ? null : formatCents(amount),
      identity_verified: false, total: matches.length, returned: Math.min(matches.length, 50), matches: matches.slice(0, 50),
      archive: { from, to_exclusive: archiveTo, complete: !missing.length && !store.badLines, missing_days: missing, bad_lines: store.badLines },
      live, warnings,
    } };
  } finally {
    lock.release();
  }
}

module.exports = { run };
