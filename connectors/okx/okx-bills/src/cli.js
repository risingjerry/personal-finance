'use strict';
// okx-bills — read-only local CLI for OKX C2C (P2P) order records.
const fs = require('node:fs');
const path = require('node:path');
const { CliError, codes } = require('./errors');
const U = require('./util');
const { BrowserSession, defaultPort } = require('./browser');
const session = require('./session');
const orders = require('./orders');
const { toCsv, toFlowsCsv } = require('./export');
const flowsLib = require('./flows');
const { runSync } = require('./sync');

const HELP = `okx-bills — read-only OKX C2C (P2P) order records

Usage: okx-bills <command> [options] [--home DIR] [--port N] [--pretty]

Commands:
  auth login [--timeout 300]        open the OKX login window and wait for you to sign in
  auth status [--launch]            session + template status
  sync [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--order-type all|completed|pending]
       [--page-size 100] [--refresh-template] [--only all|c2c|flows]
                                    fetch C2C orders + deposit/withdrawal flows
  orders list [--from] [--to] [--side buy|sell] [--status completed|cancelled]
       [--currency USDT] [--q TEXT] [--limit N] [--offset N] [--include-cancelled]
  orders summary [--by month|side|currency|status|counterparty|method] [filters]
  flows list [--from] [--to] [--type deposit|withdraw] [--currency] [--q] [--limit N]
  flows summary [--by month|type|currency|network|status] [filters]
  export --kind c2c|flows --format csv|json|ndjson [filters] [--tz Asia/Shanghai] [--out FILE|-]
  doctor [--offline]

Every command prints {"ok":true,"cmd":...,"data":...,"meta":...} on stdout.
Errors print {"ok":false,"error":{code,message,details}} on stderr and exit 1.
`;

function logFactory(quiet) {
  return (...args) => { if (!quiet) console.error(...args); };
}

function emit(obj, { pretty = false } = {}) {
  process.stdout.write(JSON.stringify(obj, null, pretty ? 2 : 0) + '\n');
}

function fail(err) {
  const payload = err instanceof CliError
    ? { ok: false, error: err.toJSON() }
    // Unexpected errors: the message only (a stack or URL can carry request headers); OKX_CLI_DEBUG=1 for the stack.
    : { ok: false, error: { code: codes.INTERNAL, message: process.env.OKX_CLI_DEBUG ? String(err && err.stack || err) : String(err && err.message || err).replace(/\?[^\s'"]*/g, '?…') } };
  process.stderr.write(JSON.stringify(payload) + '\n');
  process.exitCode = 1;
}

function filtersFrom(flags) {
  return {
    fromMs: U.parseDateStart(flags.from) ?? undefined,
    toMs: U.parseDateEnd(flags.to) ?? undefined,
    side: flags.side,
    status: flags.status,
    currency: flags.currency,
    q: flags.q,
    includeCancelled: !!flags['include-cancelled'],
  };
}

async function withBrowser(home, port, fn) {
  const browser = new BrowserSession({ home, port, log: () => {} });
  try {
    return await fn(browser);
  } finally {
    await browser.dispose();
  }
}

async function cmdAuth(flags, positional, ctx) {
  const sub = positional[1] || 'status';
  const { home, port, log } = ctx;
  if (sub === 'login') {
    const res = await withBrowser(home, port, (browser) => session.login({
      browser, log, timeoutMs: Math.max(30, Number(flags.timeout ?? 300)) * 1000,
    }));
    return { cmd: 'auth.login', data: { session: 'ok', ...res, browser: { port, profile: U.layout(home).profile } } };
  }
  if (sub === 'status') {
    return withBrowser(home, port, async (browser) => {
      const up = await browser.isUp();
      if (!up && !flags.launch) {
        return { cmd: 'auth.status', data: { session: 'browser_down', port, profile: U.layout(home).profile } };
      }
      await browser.ensure({ launch: true });
      await browser.verifyOwnership();
      const page = await browser.openPage({ url: 'https://www.okx.com/zh-hans/p2p/orders-completed' });
      const check = await session.checkSession(page);
      const tpl = U.readJson(U.layout(home).template, null);
      return {
        cmd: 'auth.status',
        data: {
          session: check.session,
          url: U.safeUrl(check.url),
          checkedAt: check.checkedAt,
          template: tpl ? { capturedAt: tpl.capturedAt, hasAuth: !!(tpl.headers && tpl.headers.Authorization) } : null,
          browser: { port, profile: U.layout(home).profile },
        },
      };
    });
  }
  throw new CliError(codes.USAGE, `unknown auth subcommand: ${sub}`);
}

async function cmdSync(flags, positional, ctx) {
  const data = await withBrowser(ctx.home, ctx.port, (browser) => runSync({
    browser, home: ctx.home, flags, log: ctx.log,
  }));
  return { cmd: 'sync', data };
}

async function cmdOrders(flags, positional, ctx) {
  const sub = positional[1] || 'list';
  const all = orders.loadOrders(ctx.home);
  const filtered = orders.filterOrders(all, filtersFrom(flags));
  if (!all.length) throw new CliError(codes.NO_DATA, 'no local orders yet: run `okx-bills sync` first');
  if (sub === 'summary') {
    const by = flags.by || 'month';
    return { cmd: 'orders.summary', data: { by, groups: orders.summarize(filtered, { by }), totals: orders.totals(filtered) } };
  }
  if (sub === 'list') {
    const limit = Math.max(1, Math.min(Number(flags.limit ?? 50), 5000));
    const offset = Math.max(0, Number(flags.offset ?? 0));
    const sorted = [...filtered].sort((a, b) => b.created_ms - a.created_ms);
    const page = sorted.slice(offset, offset + limit);
    return {
      cmd: 'orders.list',
      data: {
        total: filtered.length,
        returned: page.length,
        offset,
        limit,
        orders: page.map((o) => ({
          id: o.id, created_at: o.created_at, side: o.side, status: o.status,
          base: `${o.base_amount} ${o.base_currency}`, quote: `${o.quote_amount} ${o.quote_currency}`,
          price: o.price, counterparty: o.counterparty, method: o.payment_method,
        })),
      },
    };
  }
  throw new CliError(codes.USAGE, `unknown orders subcommand: ${sub}`);
}

async function cmdFlows(flags, positional, ctx) {
  const sub = positional[1] || 'list';
  const all = flowsLib.loadFlows(ctx.home);
  if (!all.length) throw new CliError(codes.NO_DATA, 'no local flows yet: run `okx-bills sync` first');
  const filtered = flowsLib.filterFlows(all, {
    fromMs: U.parseDateStart(flags.from) ?? undefined,
    toMs: U.parseDateEnd(flags.to) ?? undefined,
    type: flags.type,
    currency: flags.currency,
    status: flags.status,
    q: flags.q,
  });
  if (sub === 'summary') {
    const by = flags.by || 'month';
    return { cmd: 'flows.summary', data: { by, groups: flowsLib.summarizeFlows(filtered, { by }), totals: flowsLib.totalsFlows(filtered) } };
  }
  if (sub === 'list') {
    const limit = Math.max(1, Math.min(Number(flags.limit ?? 50), 5000));
    const offset = Math.max(0, Number(flags.offset ?? 0));
    const sorted = [...filtered].sort((a, b) => b.time_ms - a.time_ms);
    return {
      cmd: 'flows.list',
      data: {
        total: filtered.length, returned: Math.min(limit, sorted.length - offset), offset, limit,
        flows: sorted.slice(offset, offset + limit).map((f) => ({
          id: f.id, time: f.time, type: f.type, currency: f.currency, amount: f.amount,
          network: f.network, status: f.status_text, fee: f.fee,
        })),
      },
    };
  }
  throw new CliError(codes.USAGE, `unknown flows subcommand: ${sub}`);
}

async function cmdExport(flags, positional, ctx) {
  const kind = String(flags.kind || 'c2c').toLowerCase();
  const tz = flags.tz || 'Asia/Shanghai';
  if (kind === 'flows') return exportFlows(flags, ctx, tz);
  if (kind !== 'c2c') throw new CliError(codes.BAD_INPUT, `unsupported --kind ${kind}`);
  const all = orders.loadOrders(ctx.home);
  if (!all.length) throw new CliError(codes.NO_DATA, 'no local orders yet: run `okx-bills sync` first');
  const filtered = orders.filterOrders(all, filtersFrom(flags));
  const format = String(flags.format || 'csv').toLowerCase();
  const sorted = [...filtered].sort((a, b) => a.created_ms - b.created_ms);
  let body;
  if (format === 'csv') body = toCsv(sorted, { tz });
  else if (format === 'json') body = JSON.stringify(sorted, null, 2) + '\n';
  else if (format === 'ndjson') body = sorted.map((o) => JSON.stringify(o)).join('\n') + '\n';
  else throw new CliError(codes.BAD_INPUT, `unsupported --format ${format}`);

  const out = flags.out;
  if (out && out !== '-') {
    const file = path.resolve(String(out));
    U.ensureDir(path.dirname(file));
    U.writeFileAtomic(file, body, 0o600);
    return {
      cmd: 'export',
      data: {
        file, rows: sorted.length, format,
        bytes: Buffer.byteLength(body), sha256: U.sha256(body),
        totals: orders.totals(sorted),
      },
    };
  }
  process.stdout.write(body);
  return null;
}

async function cmdDoctor(flags, positional, ctx) {
  const L = U.ensureLayout(ctx.home);
  const checks = [];
  const add = (name, status, detail) => checks.push({ name, status, detail });
  add('node', process.version.startsWith('v22') || Number(process.versions.node.split('.')[0]) >= 22 ? 'ok' : 'warn', process.version);
  add('chrome', fs.existsSync(process.env.OKX_CLI_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome') ? 'ok' : 'fail', process.env.OKX_CLI_CHROME || 'default Chrome');
  add('profile', fs.existsSync(L.profile) ? 'ok' : 'warn', L.profile);
  const all = orders.loadOrders(ctx.home);
  add('store', all.length ? 'ok' : 'warn', `${all.length} orders @ ${L.orders}`);
  try {
    const f = flowsLib.loadFlows(ctx.home).length;
    add('flows', f ? 'ok' : 'warn', `${f} deposit/withdraw rows @ ${L.flows}`);
  } catch { /* ignore */ }
  const tpl = U.readJson(L.template, null);
  add('template', tpl && tpl.capturedAt ? 'ok' : 'warn', tpl ? `captured ${tpl.capturedAt}` : 'none (captured during sync/login)');
  if (!flags.offline) {
    const browser = new BrowserSession({ home: ctx.home, port: ctx.port, log: () => {} });
    try {
      const up = await browser.isUp();
      add('browser', up ? 'ok' : 'warn', up ? `CDP on ${ctx.port}` : 'not running');
      if (up) {
        await browser.ensure({ launch: false });
        await browser.verifyOwnership();
        add('ownership', 'ok', 'profile matches okx-cli');
      }
    } catch (err) {
      add('browser', 'fail', err.message);
    } finally {
      await browser.dispose();
    }
  }
  for (const c of checks) ctx.log(`[${c.status}] ${c.name}: ${c.detail}`);
  return { cmd: 'doctor', data: { checks, ok: !checks.some((c) => c.status === 'fail') } };
}

async function exportFlows(flags, ctx, tz) {
  const all = flowsLib.loadFlows(ctx.home);
  if (!all.length) throw new CliError(codes.NO_DATA, 'no local flows yet: run `okx-bills sync` first');
  const filtered = flowsLib.filterFlows(all, {
    fromMs: U.parseDateStart(flags.from) ?? undefined,
    toMs: U.parseDateEnd(flags.to) ?? undefined,
    type: flags.type,
    currency: flags.currency,
    q: flags.q,
  });
  const format = String(flags.format || 'csv').toLowerCase();
  const sorted = [...filtered].sort((a, b) => a.time_ms - b.time_ms);
  let body;
  if (format === 'csv') body = toFlowsCsv(sorted, { tz });
  else if (format === 'json') body = JSON.stringify(sorted, null, 2) + '\n';
  else if (format === 'ndjson') body = sorted.map((f) => JSON.stringify(f)).join('\n') + '\n';
  else throw new CliError(codes.BAD_INPUT, `unsupported --format ${format}`);
  const out = flags.out;
  if (out && out !== '-') {
    const file = path.resolve(String(out));
    U.ensureDir(path.dirname(file));
    U.writeFileAtomic(file, body, 0o600);
    return {
      cmd: 'export.flows',
      data: { file, rows: sorted.length, format, bytes: Buffer.byteLength(body), sha256: U.sha256(body), totals: flowsLib.totalsFlows(sorted) },
    };
  }
  process.stdout.write(body);
  return null;
}

const COMMANDS = {
  auth: cmdAuth,
  sync: cmdSync,
  orders: cmdOrders,
  flows: cmdFlows,
  export: cmdExport,
  doctor: cmdDoctor,
};

async function main(argv) {
  const t0 = Date.now();
  const { flags, positional } = U.parseArgs(argv);
  if (flags.help || positional[0] === 'help' || (!positional.length && !Object.keys(flags).length)) {
    process.stdout.write(HELP);
    return;
  }
  const quiet = !!flags.quiet;
  const ctx = {
    home: U.homeDir(flags.home),
    port: Number(flags.port || defaultPort()),
    quiet,
    log: logFactory(quiet),
  };
  const group = positional[0];
  const handler = COMMANDS[group];
  try {
    if (!handler) throw new CliError(codes.USAGE, `unknown command: ${group}`, { known: Object.keys(COMMANDS) });
    U.ensureLayout(ctx.home);
    const res = await handler(flags, positional, ctx);
    if (res) emit({ ok: true, ...res, meta: { elapsedMs: Date.now() - t0 } }, { pretty: !!flags.pretty });
  } catch (err) {
    fail(err);
  }
}

module.exports = { main, HELP };
