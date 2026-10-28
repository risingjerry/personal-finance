'use strict';
// Shared helpers: paths, atomic writes, hashing, time, logging.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

const DEFAULT_HOME = path.join(os.homedir(), '.alipay-cli');

function homeDir(override) {
  return path.resolve(override || process.env.ALIPAY_CLI_HOME || DEFAULT_HOME);
}

function layout(home) {
  const h = homeDir(home);
  const data = path.join(h, 'data');
  return {
    home: h,
    profile: path.join(h, 'profile'),
    vault: path.join(h, 'vault'),
    vaultCookies: path.join(h, 'vault', 'cookies.json'),
    legacyCookies: path.join(h, 'vault', 'cookies-all.json'),
    data,
    raw: path.join(data, 'raw'),
    tmp: path.join(data, 'tmp'),
    bills: path.join(data, 'bills.ndjson'),
    meta: path.join(data, 'meta.json'),
    logs: path.join(h, 'logs'),
    lock: path.join(h, 'lock'),
  };
}

function ensureDir(dir, mode) {
  fs.mkdirSync(dir, { recursive: true });
  if (mode != null) {
    try {
      fs.chmodSync(dir, mode);
    } catch {
      /* best effort (non-POSIX or already ok) */
    }
  }
}

function ensureLayout(home) {
  const L = layout(home);
  ensureDir(L.home, 0o700);
  ensureDir(L.profile, 0o700);
  ensureDir(L.vault, 0o700);
  ensureDir(L.data, 0o700);
  ensureDir(L.raw, 0o700);
  ensureDir(L.tmp, 0o700);
  ensureDir(L.logs, 0o700);
  return L;
}

function writeFileAtomic(file, data, mode) {
  const dir = path.dirname(file);
  ensureDir(dir);
  const tmp = path.join(dir, `.${path.basename(file)}.${process.pid}.${Date.now()}.tmp`);
  fs.writeFileSync(tmp, data, mode != null ? { mode } : undefined);
  if (mode != null) {
    try {
      fs.chmodSync(tmp, mode);
    } catch {
      /* ignore */
    }
  }
  fs.renameSync(tmp, file);
}

function sha256(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

function sha256File(file) {
  return sha256(fs.readFileSync(file));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));

function nowIso() {
  return new Date().toISOString();
}

// Local wall-clock in the account's timezone (Alipay statements are +08:00).
const SH_TZ = 'Asia/Shanghai';

function shanghaiParts(date = new Date()) {
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: SH_TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
  const out = {};
  for (const p of fmt.formatToParts(date)) {
    if (p.type !== 'literal') out[p.type] = p.value;
  }
  // Intl may render hour 24 for midnight in some ICU versions; normalise.
  if (out.hour === '24') out.hour = '00';
  return out;
}

function shanghaiToday(date = new Date()) {
  const p = shanghaiParts(date);
  return `${p.year}-${p.month}-${p.day}`;
}

function shanghaiNow(date = new Date()) {
  const p = shanghaiParts(date);
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}+08:00`;
}

function addDays(isoDate, days) {
  const d = new Date(`${isoDate}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

// Convert YYYY-MM-DD -> YYYY.MM.DD (Alipay query format).
function alipayDate(isoDate) {
  return isoDate.replace(/-/g, '.');
}

function isIsoDate(s) {
  return typeof s === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(s) && !Number.isNaN(Date.parse(`${s}T00:00:00Z`));
}

function pad2(n) {
  return String(n).padStart(2, '0');
}

function stampCompact(date = new Date()) {
  const p = shanghaiParts(date);
  return `${p.year}${p.month}${p.day}_${p.hour}${p.minute}${p.second}`;
}

function sanitizeLabel(s) {
  return String(s).replace(/[^A-Za-z0-9._-]+/g, '_').slice(0, 120) || 'import';
}

// Progress/diagnostics go to stderr; stdout is reserved for one JSON object.
function makeLogger(quiet) {
  const written = [];
  const write = (line) => {
    written.push(line);
    if (!quiet) process.stderr.write(`[alipay] ${line}\n`);
  };
  write.lines = written;
  return write;
}

function fileExists(p) {
  try {
    fs.accessSync(p);
    return true;
  } catch {
    return false;
  }
}

function statSafe(p) {
  try {
    return fs.statSync(p);
  } catch {
    return null;
  }
}

function humanBytes(n) {
  if (n == null) return null;
  const units = ['B', 'KB', 'MB', 'GB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)}${units[i]}`;
}

/** A URL without its query and fragment: page URLs can carry session tokens. */
function safeUrl(url) {
  return String(url || '').replace(/[?#].*$/, '');
}

module.exports = {
  safeUrl,
  DEFAULT_HOME,
  SH_TZ,
  homeDir,
  layout,
  ensureDir,
  ensureLayout,
  writeFileAtomic,
  sha256,
  sha256File,
  sleep,
  nowIso,
  shanghaiParts,
  shanghaiToday,
  shanghaiNow,
  addDays,
  alipayDate,
  isIsoDate,
  pad2,
  stampCompact,
  sanitizeLabel,
  makeLogger,
  fileExists,
  statSafe,
  humanBytes,
};
