'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { acquireLock, lockStatus } = require('../src/lock');
const { CliError } = require('../src/errors');

function tempHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'alipay-lock-'));
}

test('acquire is exclusive and release makes it available again', () => {
  const home = tempHome();
  const lock = acquireLock(home, { cmd: 'sync' });
  assert.ok(fs.existsSync(path.join(home, 'lock')));
  assert.equal(lockStatus(home).locked, true);
  assert.throws(() => acquireLock(home, { cmd: 'sync' }), (err) => err instanceof CliError && err.code === 'locked' && err.details.pid === process.pid);
  lock.release();
  assert.equal(lockStatus(home).locked, false);
  const again = acquireLock(home, { cmd: 'import' });
  again.release();
});

test('a lock from a dead process is reclaimed', () => {
  const home = tempHome();
  fs.mkdirSync(path.join(home, 'data'), { recursive: true });
  fs.writeFileSync(
    path.join(home, 'lock'),
    JSON.stringify({ pid: 999999, startedAt: new Date(Date.now() - 1000).toISOString(), cmd: 'sync' }),
  );
  const lock = acquireLock(home, { cmd: 'import' });
  assert.equal(lock.info.pid, process.pid);
  lock.release();
});

test('an ancient lock is considered stale even if the pid is alive', () => {
  const home = tempHome();
  fs.mkdirSync(path.join(home, 'data'), { recursive: true });
  fs.writeFileSync(
    path.join(home, 'lock'),
    JSON.stringify({ pid: process.pid, startedAt: new Date(Date.now() - 10 * 60 * 60 * 1000).toISOString(), cmd: 'sync' }),
  );
  const lock = acquireLock(home, { cmd: 'import' });
  lock.release();
});

test('release does not delete a lock owned by someone else', () => {
  const home = tempHome();
  const lock = acquireLock(home, { cmd: 'a' });
  // Simulate another process taking over the lock file.
  fs.writeFileSync(path.join(home, 'lock'), JSON.stringify({ pid: 424242, startedAt: new Date().toISOString(), cmd: 'b' }));
  lock.release();
  assert.ok(fs.existsSync(path.join(home, 'lock')), 'foreign lock must survive');
  fs.unlinkSync(path.join(home, 'lock'));
});
