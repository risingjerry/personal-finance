'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildPlist, LABEL, DEFAULT_INTERVAL } = require('../src/keepalive');

test('keepalive plist carries the exact agent command and interval', () => {
  const xml = buildPlist({ home: '/tmp/home', intervalSeconds: 600, logPath: '/tmp/home/logs/keepalive.log' });
  assert.match(xml, new RegExp(`<string>${LABEL}</string>`));
  assert.match(xml, /<key>StartInterval<\/key>\s*<integer>600<\/integer>/);
  assert.match(xml, /<string>auth<\/string>\s*<string>status<\/string>\s*<string>--launch<\/string>/);
  assert.match(xml, /<string>\/tmp\/home<\/string>/);
  assert.match(xml, /<key>RunAtLoad<\/key>\s*<true\/>/);
});

test('keepalive plist escapes paths instead of emitting invalid XML', () => {
  const xml = buildPlist({ home: '/tmp/a&b<c>', intervalSeconds: DEFAULT_INTERVAL, logPath: '/tmp/a&b<c>/k.log' });
  assert.match(xml, /\/tmp\/a&amp;b&lt;c&gt;/);
  assert.doesNotMatch(xml, /a&b<c>/);
});

test('cli manifest matches package.json and stays read-only', () => {
  const pkg = require('../package.json');
  const manifest = require('../cli.json');
  assert.equal(manifest.schema, 'cli.manifest.v1');
  assert.equal(manifest.version, pkg.version);
  assert.equal(manifest.entry.bin, 'bin/alipay');
  assert.equal(manifest.safety.readOnly, true);
  assert.ok(manifest.commands.some((c) => c.name === 'bills list'));
  assert.ok(manifest.commands.some((c) => c.name === 'sync'));
  assert.equal(manifest.skill.name, 'alipay-bills');
  assert.deepEqual(manifest.mcp.tools, ['bills_list', 'bills_summary', 'bills_status']);
  assert.equal(manifest.home.modes.dirs, '0700');
});
