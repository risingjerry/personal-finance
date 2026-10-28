'use strict';
// End-to-end CLI tests: every command is executed through bin/alipay against a
// throwaway --home, so nothing here touches the real account data.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { buildZip, statementCsv, yearRows } = require('./helpers/fixtures');

const BIN = path.join(__dirname, '..', 'bin', 'alipay');

function tempHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'alipay-cli-e2e-'));
}

function run(home, args, { expectOk = true } = {}) {
  const res = spawnSync(process.execPath, [BIN, ...args], {
    env: { ...process.env, ALIPAY_CLI_HOME: home, ALIPAY_CLI_QUIET: '1' },
    encoding: 'utf8',
    timeout: 60000,
  });
  const stdout = res.stdout.trim();
  const stderr = res.stderr.trim();
  let json = null;
  if (stdout && stdout.startsWith('{')) json = JSON.parse(stdout);
  if (expectOk) {
    assert.equal(res.status, 0, `expected exit 0 for ${args.join(' ')}, stderr: ${stderr}`);
    assert.ok(json, `expected JSON on stdout for ${args.join(' ')}, got: ${stdout.slice(0, 200)}`);
    assert.equal(json.ok, true);
  }
  return { status: res.status, stdout, stderr, json };
}

function makeExportFile(home, name, { begin, end, rows }) {
  const dir = path.join(home, 'inbox');
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, name);
  fs.writeFileSync(file, buildZip({ [`${name.replace(/\.zip$/, '')}_1.csv`]: Buffer.from(statementCsv({ begin, end, rows }), 'utf8') }));
  return file;
}

test('--version and --help work without a store', () => {
  const home = tempHome();
  const v = run(home, ['--version']);
  assert.equal(v.json.data.version, '1.0.0');
  const h = spawnSync(process.execPath, [BIN, '--help'], { encoding: 'utf8', env: { ...process.env, ALIPAY_CLI_HOME: home } });
  assert.equal(h.status, 0);
  assert.ok(h.stdout.includes('USAGE'));
  assert.ok(h.stdout.includes('alipay sync'));
});

test('import -> bills list -> bills summary -> export works end to end', () => {
  const home = tempHome();
  const zip = makeExportFile(home, '2025.zip', { begin: '2025-01-01', end: '2026-01-01', rows: yearRows(2025, 6) });

  const imported = run(home, ['import', zip]);
  assert.equal(imported.json.cmd, 'import');
  assert.equal(imported.json.data.added, 6);

  const list = run(home, ['bills', 'list', '--limit', '3']);
  assert.equal(list.json.cmd, 'bills.list');
  assert.equal(list.json.data.total, 6);
  assert.equal(list.json.data.returned, 3);
  assert.equal(list.json.meta.storeRecords, 6);

  const summary = run(home, ['bills', 'summary', '--by', 'month']);
  assert.equal(summary.json.data.groups[0].key, '2025-01');
  assert.equal(summary.json.data.totals.matched, 6);

  const outFile = path.join(home, 'export.csv');
  const exported = run(home, ['export', '--format', 'csv', '--from', '2025', '--out', outFile]);
  assert.equal(exported.json.data.rows, 6);
  assert.equal(exported.json.data.format, 'csv');
  const csv = fs.readFileSync(outFile, 'utf8');
  assert.ok(csv.startsWith('trade_no,merchant_order_no,created_at'));
  assert.equal(csv.trim().split('\r\n').length, 7);
});

test('filters narrow the result set', () => {
  const home = tempHome();
  const zip = makeExportFile(home, '2025.zip', { begin: '2025-01-01', end: '2026-01-01', rows: yearRows(2025, 4) });
  run(home, ['import', zip]);

  const out = run(home, ['bills', 'list', '--direction', 'out']);
  assert.equal(out.json.data.total, 3);
  const inOnly = run(home, ['bills', 'list', '--direction', 'in']);
  assert.equal(inOnly.json.data.total, 1);
  const byText = run(home, ['bills', 'list', '--q', 'shop a']);
  assert.equal(byText.json.data.total, 2);
  const byAmount = run(home, ['bills', 'list', '--min', '30.50']);
  assert.equal(byAmount.json.data.total, 2);
  const none = run(home, ['bills', 'list', '--from', '2030']);
  assert.equal(none.json.data.total, 0);
});

test('export --out - streams raw data without an envelope', () => {
  const home = tempHome();
  const zip = makeExportFile(home, '2025.zip', { begin: '2025-01-01', end: '2026-01-01', rows: yearRows(2025, 2) });
  run(home, ['import', zip]);
  const res = spawnSync(process.execPath, [BIN, 'export', '--format', 'ndjson', '--out', '-'], {
    env: { ...process.env, ALIPAY_CLI_HOME: home },
    encoding: 'utf8',
  });
  assert.equal(res.status, 0);
  const lines = res.stdout.trim().split('\n').filter(Boolean);
  assert.equal(lines.length, 2);
  assert.equal(JSON.parse(lines[0]).trade_no.length > 0, true);
});

test('unknown command and bad flags fail with a JSON error on stderr and exit 1', () => {
  const home = tempHome();
  const unknown = spawnSync(process.execPath, [BIN, 'nope'], { env: { ...process.env, ALIPAY_CLI_HOME: home }, encoding: 'utf8' });
  assert.equal(unknown.status, 1);
  assert.equal(unknown.stdout.trim(), '');
  const err = JSON.parse(unknown.stderr.trim());
  assert.equal(err.ok, false);
  assert.equal(err.error.code, 'usage');

  const badFlag = spawnSync(process.execPath, [BIN, 'bills', 'list', '--direction', 'sideways'], {
    env: { ...process.env, ALIPAY_CLI_HOME: home },
    encoding: 'utf8',
  });
  assert.equal(badFlag.status, 1);
  assert.equal(JSON.parse(badFlag.stderr.trim()).error.code, 'bad_input');
});

test('bills commands work on an empty store', () => {
  const home = tempHome();
  const list = run(home, ['bills', 'list']);
  assert.equal(list.json.data.total, 0);
  const summary = run(home, ['bills', 'summary']);
  assert.equal(summary.json.data.totals.matched, 0);
  assert.equal(summary.json.data.totals.net, '0.00');
});

test('import with no matching files is a usage error', () => {
  const home = tempHome();
  const res = spawnSync(process.execPath, [BIN, 'import'], { env: { ...process.env, ALIPAY_CLI_HOME: home }, encoding: 'utf8' });
  assert.equal(res.status, 1);
  assert.equal(JSON.parse(res.stderr.trim()).error.code, 'usage');
});

test('doctor --offline reports checks and never needs the network', () => {
  const home = tempHome();
  // Any existing file stands in for Chrome, so the check passes on machines without it (CI).
  const env = { ...process.env, ALIPAY_CLI_HOME: home, ALIPAY_CLI_CHROME: process.execPath };
  const res = spawnSync(process.execPath, [BIN, 'doctor', '--offline', '--port', '1'], { env, encoding: 'utf8', timeout: 60000 });
  const json = JSON.parse(res.stdout.trim());
  assert.equal(json.cmd, 'doctor');
  const names = json.data.checks.map((c) => c.name);
  for (const n of ['node', 'websocket', 'gb18030-decoder', 'chrome-binary', 'bills-store', 'sync-state', 'lock']) {
    assert.ok(names.includes(n), `missing doctor check: ${n}`);
  }
  assert.equal(json.data.checks.find((c) => c.name === 'session').status, 'skip');
  // A fresh home has no data yet: that is a warning, not a failure.
  assert.equal(json.data.ok, true);
  assert.equal(res.status, 0);
});

test('auth status without a browser reports browser_down and stays offline', () => {
  const home = tempHome();
  const res = spawnSync(process.execPath, [BIN, 'auth', 'status', '--port', '1'], { env: { ...process.env, ALIPAY_CLI_HOME: home }, encoding: 'utf8', timeout: 60000 });
  assert.equal(res.status, 0, res.stderr);
  const json = JSON.parse(res.stdout.trim());
  assert.equal(json.data.session, 'browser_down');
  assert.equal(json.data.browser.up, false);
});

test('MCP stdio server answers initialize and tools/list', () => {
  const home = tempHome();
  const zip = makeExportFile(home, '2025.zip', { begin: '2025-01-01', end: '2026-01-01', rows: yearRows(2025, 2) });
  run(home, ['import', zip]);
  const input = [
    JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05' } }),
    JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }),
    JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list' }),
    JSON.stringify({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'bills_summary', arguments: { by: 'year' } } }),
  ].join('\n');
  const res = spawnSync(process.execPath, [BIN, 'mcp'], {
    env: { ...process.env, ALIPAY_CLI_HOME: home },
    input,
    encoding: 'utf8',
    timeout: 30000,
  });
  const replies = res.stdout.trim().split('\n').filter(Boolean).map((l) => JSON.parse(l));
  assert.equal(replies.length, 3);
  assert.equal(replies[0].result.serverInfo.name, 'alipay-cli');
  assert.deepEqual(replies[1].result.tools.map((t) => t.name), ['bills_list', 'bills_summary', 'bills_status']);
  const summary = JSON.parse(replies[2].result.content[0].text);
  assert.equal(summary.totals.matched, 2);
});
