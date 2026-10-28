'use strict';
// Paths, atomic writes, time, and exact decimal helpers. No third-party deps.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

const DEFAULT_HOME = path.join(os.homedir(), '.okx-cli');

function homeDir(override) {
  return path.resolve(override || process.env.OKX_CLI_HOME || DEFAULT_HOME);
}

function layout(home) {
  const h = homeDir(home);
  const data = path.join(h, 'data');
  return {
    home: h,
    profile: path.join(h, 'profile'),
    data,
    raw: path.join(data, 'raw'),
    tmp: path.join(data, 'tmp'),
    orders: path.join(data, 'orders.ndjson'),
    flows: path.join(data, 'flows.ndjson'),
    meta: path.join(data, 'meta.json'),
    template: path.join(h, 'session', 'request-template.json'),
    logs: path.join(h, 'logs'),
    exports: path.join(h, 'exports'),
    lock: path.join(h, 'lock'),
  };
}

function ensureDir(dir, mode) {
  fs.mkdirSync(dir, { recursive: true });
  if (mode != null) {
    try { fs.chmodSync(dir, mode); } catch { /* best effort */ }
  }
}

function ensureLayout(home) {
  const L = layout(home);
  ensureDir(L.home, 0o700);
  ensureDir(L.profile, 0o700);
  ensureDir(path.dirname(L.template), 0o700);
  ensureDir(L.data, 0o700);
  ensureDir(L.raw, 0o700);
  ensureDir(L.tmp, 0o700);
  ensureDir(L.logs, 0o700);
  ensureDir(L.exports, 0o700);
  return L;
}

function writeFileAtomic(file, data, mode) {
  const dir = path.dirname(file);
  ensureDir(dir);
  const tmp = path.join(dir, `.${path.basename(file)}.${process.pid}.${Date.now()}.tmp`);
  fs.writeFileSync(tmp, data, mode != null ? { mode } : undefined);
  fs.renameSync(tmp, file);
}

function readJson(file, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return fallback;
  }
}

function sha256(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

function sha256File(file) {
  return sha256(fs.readFileSync(file));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));
const nowIso = () => new Date().toISOString();

// ---- exact decimals (amounts are strings; never use float) -----------------
// toUnits("3000.00", 6) -> 3000000000n ; safe for the magnitudes here.
function toUnits(value, scale = 6) {
  const s = String(value ?? '').trim();
  if (!s) return 0n;
  const m = /^([+-]?)(\d*)(?:\.(\d*))?$/.exec(s);
  if (!m) return 0n;
  const sign = m[1] === '-' ? -1n : 1n;
  const whole = m[2] || '0';
  const frac = (m[3] || '').padEnd(scale, '0').slice(0, scale);
  return sign * (BigInt(whole || '0') * 10n ** BigInt(scale) + BigInt(frac || '0'));
}

function fromUnits(units, scale = 6, minFrac = 2) {
  const neg = units < 0n;
  let v = neg ? -units : units;
  const base = 10n ** BigInt(scale);
  const whole = v / base;
  let frac = (v % base).toString().padStart(scale, '0');
  frac = frac.slice(0, scale);
  while (frac.length > minFrac && frac.endsWith('0')) frac = frac.slice(0, -1);
  return `${neg ? '-' : ''}${whole}${frac ? `.${frac}` : ''}`;
}

function sumDecimals(values, scale = 6) {
  let acc = 0n;
  for (const v of values) acc += toUnits(v, scale);
  return fromUnits(acc, scale);
}

// ---- tiny arg parser -------------------------------------------------------
// Supports `--key value`, `--key=value`, boolean flags, and positionals.
function parseArgs(argv) {
  const flags = {};
  const positional = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--') { positional.push(...argv.slice(i + 1)); break; }
    if (a.startsWith('--')) {
      const eq = a.indexOf('=');
      if (eq > 0) { flags[a.slice(2, eq)] = a.slice(eq + 1); continue; }
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) flags[key] = true;
      else { flags[key] = next; i++; }
    } else {
      positional.push(a);
    }
  }
  return { flags, positional };
}

// YYYY-MM-DD -> epoch ms (UTC midnight) ; also accepts YYYY-MM (month start).
function parseDateStart(text) {
  if (!text) return null;
  const s = String(text).trim();
  let m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (m) return Date.UTC(+m[1], +m[2] - 1, +m[3]);
  m = /^(\d{4})-(\d{2})$/.exec(s);
  if (m) return Date.UTC(+m[1], +m[2] - 1, 1);
  m = /^(\d{4})$/.exec(s);
  if (m) return Date.UTC(+m[1], 0, 1);
  return null;
}

function parseDateEnd(text) {
  if (!text) return null;
  const s = String(text).trim();
  let m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (m) return Date.UTC(+m[1], +m[2] - 1, +m[3]) + 24 * 3600 * 1000 - 1;
  m = /^(\d{4})-(\d{2})$/.exec(s);
  if (m) return Date.UTC(+m[1], +m[2], 1) - 1;
  m = /^(\d{4})$/.exec(s);
  if (m) return Date.UTC(+m[1] + 1, 0, 1) - 1;
  return null;
}

/** A URL without its query and fragment: page URLs can carry session tokens. */
function safeUrl(url) {
  return String(url || '').replace(/[?#].*$/, '');
}

module.exports = {
  safeUrl,
  homeDir, layout, ensureDir, ensureLayout, writeFileAtomic, readJson,
  sha256, sha256File, sleep, nowIso,
  toUnits, fromUnits, sumDecimals,
  parseArgs, parseDateStart, parseDateEnd,
};
