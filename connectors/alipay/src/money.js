'use strict';
// Exact decimal money handling. Amounts stay strings end-to-end; arithmetic uses
// BigInt cents so no float ever touches a value.

function toCents(input) {
  if (input == null) throw new Error('amount is null');
  const s = String(input).trim();
  const m = /^([+-])?(\d+)(?:\.(\d{1,2}))?$/.exec(s);
  if (!m) throw new Error(`invalid amount: ${JSON.stringify(input)}`);
  const sign = m[1] === '-' ? -1n : 1n;
  const whole = BigInt(m[2]);
  const frac = (m[3] || '').padEnd(2, '0');
  const cents = whole * 100n + BigInt(frac);
  return sign * cents;
}

function formatCents(cents) {
  const neg = cents < 0n;
  const abs = neg ? -cents : cents;
  const whole = abs / 100n;
  const frac = abs % 100n;
  return `${neg ? '-' : ''}${whole.toString()}.${frac.toString().padStart(2, '0')}`;
}

// Normalise any accepted money string to a 2-decimal string ("42.1" -> "42.10").
function normalizeAmount(input) {
  return formatCents(toCents(input));
}

function isValidAmount(input) {
  try {
    toCents(input);
    return true;
  } catch {
    return false;
  }
}

// Sum a list of decimal strings exactly.
function sumAmounts(values) {
  let total = 0n;
  for (const v of values) total += toCents(v);
  return formatCents(total);
}

function compareAmounts(a, b) {
  const ca = toCents(a);
  const cb = toCents(b);
  return ca < cb ? -1 : ca > cb ? 1 : 0;
}

// Percent of a/b as a string with 2 decimals (e.g. refund ratio). b must be > 0.
function ratioPercent(a, b) {
  const ca = toCents(a);
  const cb = toCents(b);
  if (cb === 0n) return '0.00';
  const scaled = (ca * 10000n) / cb; // 4 decimals of percent precision
  return formatCents((scaled * 100n) / 10000n);
}

module.exports = { toCents, formatCents, normalizeAmount, isValidAmount, sumAmounts, compareAmounts, ratioPercent };
