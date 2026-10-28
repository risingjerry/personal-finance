/*
 * Read-only 携程拿去花 bill records for `ctrip fetch`.
 *
 * The 拿去花 H5 pages (jr.ctrip.com) encrypt their request bodies, but the
 * responses are plain JSON.  So instead of forging requests, this opens each
 * record tab in a headless Chromium seeded with the owner's existing Ctrip
 * login cookies, scrolls to the end of the list, and returns the JSON the page
 * itself received.  It never clicks, types, repays or changes anything.
 *
 * Input on stdin (never argv): {cookies, executable_path, timeout_ms, tabs}.
 * Output: one JSON line on stdout.  Cookie values are never printed.
 */

'use strict';

const fs = require('fs');

const RECORDS = 'https://jr.ctrip.com/m/nano/page/ctrip/records';
const API = /\/restapi\/fintech\/nano\/(queryConsumerRecords|queryRepayRecords|queryRefundRecords|queryInstallRecords|queryProcessPostTradeOrder)\.do/;
const END_MARKERS = ['没有更多了', '暂无记录'];
const LOGIN_HINT = /passport|login|signin/i;

function readInput() {
  try { return JSON.parse(fs.readFileSync(0, 'utf8') || '{}'); } catch (err) { return {}; }
}

async function readTab(context, tab, timeoutMs) {
  const page = await context.newPage();
  const responses = [];
  page.on('response', async (response) => {
    const url = response.url();
    const m = API.exec(url);
    if (!m) return;
    try { responses.push({ api: m[1], status: response.status(), body: await response.json() }); }
    catch (err) { responses.push({ api: m[1], status: response.status(), body: null }); }
  });
  await page.goto(`${RECORDS}?billType=${encodeURIComponent(tab)}`, { waitUntil: 'networkidle', timeout: timeoutMs });
  if (LOGIN_HINT.test(new URL(page.url()).hostname + new URL(page.url()).pathname)) {
    await page.close();
    return { tab, login_required: true, responses: [] };
  }
  // Scroll until the list says it has ended, or nothing new arrives three times in a row.
  let idle = 0, seen = responses.length, ended = false;
  for (let i = 0; i < 60 && idle < 3; i++) {
    const text = await page.evaluate(() => document.body ? document.body.innerText : '');
    if (END_MARKERS.some((t) => text.includes(t))) { ended = true; break; }
    await page.mouse.wheel(0, 4000);
    await page.waitForTimeout(1200);
    if (responses.length === seen) idle += 1; else { idle = 0; seen = responses.length; }
  }
  await page.close();
  return { tab, ended, responses };
}

async function main(input) {
  const { chromium } = require('playwright');
  const timeoutMs = Number(input.timeout_ms) || 45000;
  const launch = { headless: true };
  if (input.executable_path) launch.executablePath = input.executable_path;
  const browser = await chromium.launch(launch);
  try {
    // The H5 pages are built for the app's web view; a phone-sized context renders the full list.
    const context = await browser.newContext({
      locale: 'zh-CN', timezoneId: 'Asia/Shanghai', viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true,
      userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
    });
    await context.addCookies(input.cookies || []);
    const tabs = [];
    for (const tab of input.tabs || ['trade', 'repay', 'refund', 'stage', 'afterPay']) {
      tabs.push(await readTab(context, tab, timeoutMs));
    }
    process.stdout.write(JSON.stringify({ ok: true, tabs }) + '\n');
  } finally {
    await browser.close();
  }
}

main(readInput()).catch((err) => {
  process.stdout.write(JSON.stringify({ ok: false, error: err && err.name ? String(err.name) : 'Error', message: String(err && err.message || '').slice(0, 200) }) + '\n');
  process.exitCode = 1;
});
