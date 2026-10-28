'use strict';
// Single-flight lock: ~/.alipay-cli/lock with O_CREAT|O_EXCL + liveness check.
// A lock left behind by a dead process (or one older than staleMs) is reclaimed.
const fs = require('node:fs');
const { CliError, codes } = require('./errors');
const { ensureLayout, nowIso } = require('./util');

const STALE_MS = 2 * 60 * 60 * 1000; // 2h

function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  if (pid === process.pid) return true;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code === 'EPERM';
  }
}

function readLock(lockPath) {
  try {
    return JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  } catch {
    return null;
  }
}

function describeHolder(info, lockPath) {
  if (!info) return { lock: lockPath, pid: null, startedAt: null };
  const ageMs = info.startedAt ? Date.now() - Date.parse(info.startedAt) : null;
  return { lock: lockPath, pid: info.pid ?? null, startedAt: info.startedAt ?? null, ageMs, cmd: info.cmd ?? null };
}

/**
 * Acquire the lock or throw codes.LOCKED.
 * @param {string} home
 * @param {{cmd?:string, staleMs?:number}} opts
 */
function acquireLock(home, opts = {}) {
  const staleMs = opts.staleMs ?? STALE_MS;
  const L = ensureLayout(home);
  const payload = JSON.stringify({ pid: process.pid, startedAt: nowIso(), cmd: opts.cmd || null, host: require('node:os').hostname() });

  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const fd = fs.openSync(L.lock, 'wx', 0o600);
      fs.writeSync(fd, payload);
      fs.fsyncSync(fd);
      fs.closeSync(fd);
      let released = false;
      return {
        path: L.lock,
        info: JSON.parse(payload),
        release() {
          if (released) return;
          released = true;
          try {
            const info = readLock(L.lock);
            if (!info || info.pid === process.pid) fs.unlinkSync(L.lock);
          } catch {
            /* already gone */
          }
        },
      };
    } catch (err) {
      if (err.code !== 'EEXIST') throw err;
      const info = readLock(L.lock);
      const ageMs = info && info.startedAt ? Date.now() - Date.parse(info.startedAt) : Infinity;
      const alive = info && pidAlive(info.pid);
      const stale = !alive || ageMs > staleMs;
      if (stale && attempt === 0) {
        try {
          fs.unlinkSync(L.lock);
        } catch {
          /* raced with another reclaimer */
        }
        continue;
      }
      throw new CliError(codes.LOCKED, 'another alipay-cli run holds the lock', {
        ...describeHolder(info, L.lock),
        hint: stale ? 'stale lock detected; retry' : 'wait for the other run to finish',
      });
    }
  }
  throw new CliError(codes.LOCKED, 'could not acquire lock', { lock: L.lock });
}

function lockStatus(home) {
  const L = ensureLayout(home);
  if (!fs.existsSync(L.lock)) return { locked: false, path: L.lock };
  const info = readLock(L.lock);
  const alive = info ? pidAlive(info.pid) : false;
  return { locked: alive, path: L.lock, ...describeHolder(info, L.lock), pidAlive: alive };
}

module.exports = { acquireLock, lockStatus, pidAlive, STALE_MS };
