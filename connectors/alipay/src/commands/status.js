'use strict';
// `alipay status` — one-screen view over both stores plus the browser session.
// Read-only and offline-safe: it never launches Chrome and never calls Alipay.
const { BrowserSession } = require('../browser');
const { loadBills, readMeta } = require('../store');
const merchant = require('./merchant');
const { layout, nowIso } = require('../util');

async function run(ctx) {
  const { home, port } = ctx;
  const L = layout(home);
  const { records } = loadBills(home);
  const meta = readMeta(home);
  const byYear = new Map();
  for (const r of records) {
    if (r.year == null) continue;
    byYear.set(r.year, (byYear.get(r.year) || 0) + 1);
  }
  const years = [...byYear.entries()].sort((a, b) => a[0] - b[0]);

  let browserUp = false;
  try {
    const browser = new BrowserSession({ home, port, log: () => {} });
    browserUp = await browser.isUp();
    await browser.dispose();
  } catch {
    browserUp = false;
  }

  let business = null;
  try {
    const res = await merchant.status({ home });
    business = res.data;
  } catch (err) {
    business = { error: err.message };
  }

  return {
    cmd: 'status',
    data: {
      personal: {
        store: L.bills,
        records: records.length,
        firstYear: years.length ? years[0][0] : null,
        lastYear: years.length ? years[years.length - 1][0] : null,
        byYear: Object.fromEntries(years),
        yearsKnown: Object.keys(meta.years || {}).length,
        lastSyncAt: meta.lastSyncAt || null,
        rateLimit: meta.rateLimit || null,
      },
      business,
      session: {
        chromeUp: browserUp,
        port,
        hint: browserUp ? 'a scan may still be required; run `alipay auth status`' : 'Chrome is not running; `alipay auth login` starts it',
      },
      checkedAt: nowIso(),
    },
  };
}

module.exports = { run };
