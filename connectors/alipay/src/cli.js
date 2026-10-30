'use strict';
// Argument parsing, command routing and the JSON envelope contract.
//
// stdout: exactly one JSON object (except `export --out -` which streams data)
// stderr: progress lines ("[alipay] ...") and, on failure, one JSON error object
const { CliError, codes, usage } = require('./errors');
const { homeDir, makeLogger, nowIso, ensureLayout } = require('./util');
const { defaultPort } = require('./browser');
const pkg = require('../package.json');

const VERSION = pkg.version;

const HELP = `alipay-cli ${VERSION} — read-only CLI for personal Alipay transactions

USAGE
  alipay <command> [options]

COMMANDS
  auth login [--timeout 300]        Open the off-screen Chrome, wait for QR login, save a cookie snapshot
  auth status [--launch]            Report the session: ok | need_login | security_challenge | browser_down
  doctor [--offline]                Self-check: node, Chrome, profile, vault, data integrity, session
  sync [--from 2017] [--to 2026]    Export year windows into the local store (resumable, throttled)
       [--force] [--throttle 45] [--span-years 1] [--max-retries 2]
  import [file.zip|file.csv|dir...] Parse existing exports into the store (no network). Default dir: data/raw
       [--year YYYY]
  bills list [--from] [--to]        Query the local store
             [--direction in|out|none] [--q TEXT] [--min N] [--max N]
             [--limit 50] [--offset 0] [--order asc|desc]
  bills summary [--by year|month|counterparty|type|status|source]
                [--from] [--to] [--direction] [--top N]
  export --format json|csv|ndjson [--from] [--to] [--direction] [--q]
                [--min N] [--max N] [--columns a,b] [--order asc|desc] [--out FILE|-]
  merchant status                    Business archive: counts, coverage, app config
  merchant sync --from --to [--kind both|signcustomer|trade] [--refresh] [--concurrency 4]
                                     Archive business bills (official API, no web quota)
  merchant list [--from --to] [--kind funds|trade] [--q TEXT] [--limit --offset]
  merchant summary [--by month|day|counterparty|type] [--from --to]
  merchant balance                   Live account balance
  merchant today [--from DATE]       Same-day receipts (live) + balance check vs archive; start here for "did X pay me"
  merchant query --kind sell|transfer|deposit|withdraw --from --to
                                     Paginated online records (<=31-day windows)
  merchant trade --trade-no T [--refund-no R]
                                     Trade / refund detail
  status                             One-screen summary: personal store, business archive, session
  keepalive install [--interval 600] Keep the session alive: per-user launchd agent pings the record page
  keepalive status                  Is the agent installed/loaded; last run and session state
  keepalive uninstall               Remove the launchd agent (the Alipay session is untouched)
  mcp                               Run a stdio MCP server (tools: bills_list, bills_summary, bills_status)

GLOBAL OPTIONS
  --home DIR      Store root (default: $ALIPAY_CLI_HOME or ~/.alipay-cli)
  --port N        Chrome DevTools port (default: $ALIPAY_CLI_CDP_PORT or 9333)
  --pretty        Indent JSON output
  --quiet         Suppress progress lines on stderr
  --help, --version

EXAMPLES
  alipay auth login
  alipay import ~/Downloads/alipay_record_2025.zip
  alipay import ~/.alipay-cli/data/raw --year 2024
  alipay bills summary --by month --from 2025-01 --to 2025-12
  alipay bills list --direction out --min 100 --limit 20
  alipay export --format csv --from 2025 --out ~/Desktop/2025.csv
  alipay sync --from 2024 --to 2024 --throttle 60

CONTRACT
  Success -> stdout: {"ok":true,"cmd":"...","data":{...},"meta":{...}}
  Failure -> stderr: {"ok":false,"error":{"code":"...","message":"...","details":{...}}} (exit 1)
`;

function parseArgs(argv) {
  const flags = {};
  const positionals = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--') {
      positionals.push(...argv.slice(i + 1));
      break;
    }
    if (a === '-h' || a === '--help') {
      flags.help = true;
      continue;
    }
    if (a === '-v' || a === '--version' || a === '-V') {
      flags.version = true;
      continue;
    }
    if (a.startsWith('--')) {
      const eq = a.indexOf('=');
      let key;
      let val;
      if (eq >= 0) {
        key = a.slice(2, eq);
        val = a.slice(eq + 1);
      } else {
        key = a.slice(2);
        const next = argv[i + 1];
        // A lone "-" is a value (conventional stdout), not a flag.
        if (next !== undefined && next !== '--' && (next === '-' || !next.startsWith('-') || /^-\d/.test(next))) {
          val = next;
          i++;
        } else {
          val = true;
        }
      }
      if (/^no-/.test(key) && val === true) {
        flags[key.slice(3)] = false;
        continue;
      }
      flags[key] = val;
      continue;
    }
    if (a.startsWith('-') && a.length > 1 && !/^-\d/.test(a)) {
      flags[a.slice(1)] = true;
      continue;
    }
    positionals.push(a);
  }
  return { flags, positionals };
}

function resolveRoute(positionals) {
  const [first, second] = positionals;
  const routes = {
    'auth login': () => require('./commands/auth').login,
    'auth status': () => require('./commands/auth').status,
    doctor: () => require('./commands/doctor').run,
    sync: () => require('./commands/sync').run,
    import: () => require('./commands/importer').run,
    'bills list': () => require('./commands/bills').list,
    'bills summary': () => require('./commands/bills').summary,
    export: () => require('./commands/export').run,
    mcp: () => require('./commands/mcp').serve,
    'merchant status': () => require('./commands/merchant').status,
    'merchant list': () => require('./commands/merchant').list,
    'merchant sync': () => require('./commands/merchant').sync,
    'merchant summary': () => require('./commands/merchant').summary,
    'merchant balance': () => require('./commands/merchant').balance,
    'merchant today': () => require('./commands/merchant').today,
    'merchant query': () => require('./commands/merchant').query,
    'merchant trade': () => require('./commands/merchant').trade,
    status: () => require('./commands/status').run,
    'keepalive install': () => require('./commands/keepalive').install,
    'keepalive status': () => require('./commands/keepalive').status,
    'keepalive uninstall': () => require('./commands/keepalive').uninstall,
  };
  const groupCommands = {
    auth: ['login', 'status'],
    bills: ['list', 'summary'],
    keepalive: ['install', 'status', 'uninstall'],
    merchant: ['status', 'sync', 'list', 'summary', 'balance', 'today', 'query', 'trade'],
  };
  if (!first) throw usage('no command given');
  if (groupCommands[first]) {
    if (!second) throw usage(`missing subcommand for "${first}"`, { expected: groupCommands[first] });
    const key = `${first} ${second}`;
    if (!routes[key]) throw usage(`unknown subcommand "${key}"`, { expected: groupCommands[first].map((s) => `${first} ${s}`) });
    return { key, fn: routes[key](), consumed: 2 };
  }
  if (!routes[first]) throw usage(`unknown command "${first}"`, { commands: Object.keys(routes) });
  return { key: first, fn: routes[first](), consumed: 1 };
}

function emitOk(cmd, result, ctx, elapsedMs) {
  const payload = {
    ok: true,
    cmd: result.cmd || cmd,
    data: result.data === undefined ? null : result.data,
    meta: {
      version: VERSION,
      home: ctx.home,
      generatedAt: nowIso(),
      elapsedMs,
      ...(result.meta || {}),
    },
  };
  process.stdout.write(`${JSON.stringify(payload, null, ctx.pretty ? 2 : 0)}\n`);
}

function emitError(err, ctx, elapsedMs, cmd) {
  const isCli = err instanceof CliError;
  const body = {
    ok: false,
    cmd: cmd || null,
    error: isCli
      ? err.toJSON()
      : {
          code: codes.INTERNAL,
          message: String(err && err.message ? err.message : err),
          ...(process.env.ALIPAY_CLI_DEBUG ? { stack: String(err && err.stack) } : {}),
        },
    meta: { version: VERSION, home: ctx ? ctx.home : null, generatedAt: nowIso(), elapsedMs },
  };
  process.stderr.write(`${JSON.stringify(body, null, ctx && ctx.pretty ? 2 : 0)}\n`);
}

async function main(argv) {
  const t0 = Date.now();
  const { flags, positionals } = parseArgs(argv);

  if (flags.version) {
    process.stdout.write(`${JSON.stringify({ ok: true, cmd: 'version', data: { name: 'alipay-cli', version: VERSION, node: process.versions.node } })}\n`);
    return 0;
  }
  if (flags.help || (!positionals.length && !flags.version)) {
    process.stdout.write(HELP);
    return 0;
  }

  const home = homeDir(flags.home);
  const port = flags.port === undefined ? defaultPort() : Number(flags.port);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    emitError(usage(`bad --port: ${flags.port}`), { home, pretty: !!flags.pretty }, Date.now() - t0, null);
    return 1;
  }
  const log = makeLogger(!!flags.quiet);
  const ctx = { flags, positionals, home, port, log, pretty: !!flags.pretty, version: VERSION };

  let route = null;
  try {
    route = resolveRoute(positionals);
    ctx.positionals = positionals.slice(route.consumed);
    if (route.key !== 'mcp') ensureLayout(home);
    const result = await route.fn(ctx);
    if (route.key === 'mcp') return 0;
    if (!result || result.suppressEnvelope) return 0;
    emitOk(route.key, result, ctx, Date.now() - t0);
    return result.exitCode === undefined ? 0 : result.exitCode;
  } catch (err) {
    emitError(err, ctx, Date.now() - t0, route ? route.key : positionals[0] || null);
    return 1;
  }
}

module.exports = { main, parseArgs, resolveRoute, HELP, VERSION };
