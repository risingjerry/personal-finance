/*
 * One-time Didi web login for `python3 -m connectors.didi login`.
 *
 * Opens a real, visible Chromium with the owner's own persistent profile,
 * lets the owner log in by hand (QR / SMS), and waits until the order-list
 * request succeeds.  Then it snapshots the storage state (cookies +
 * localStorage) and the current entry URL so later headless runs never need
 * to log in again.
 *
 * Input on stdin (never argv): profile_dir, url, executable_path, timeout_ms,
 * state_path, entry_url_path.
 * It never prints cookie/token values.
 */

'use strict';

const fs = require('fs');

function readInput() {
  try {
    return JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
  } catch (err) {
    return {};
  }
}

function say(line) {
  process.stdout.write(line + '\n');
}

function countOrders(value, depth) {
  if (!value || depth > 8) return 0;
  if (Array.isArray(value)) {
    let total = 0;
    for (const item of value) total += countOrders(item, depth + 1);
    return total;
  }
  if (typeof value !== 'object') return 0;
  let total = 0;
  if (value.order_id || value.orderId || value.oid) total += 1;
  for (const key of Object.keys(value)) {
    const child = value[key];
    if (child && typeof child === 'object') total += countOrders(child, depth + 1);
  }
  return total;
}

async function main(input) {
  const { chromium } = require('playwright');
  const timeoutMs = Number(input.timeout_ms) || 600000;
  const profileDir = input.profile_dir;
  if (!profileDir) throw new Error('missing profile_dir');
  fs.mkdirSync(profileDir, { recursive: true, mode: 0o700 });
  const launch = {
    headless: false,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    viewport: { width: 1280, height: 960 },
    args: ['--start-maximized'],
  };
  if (input.executable_path) launch.executablePath = input.executable_path;
  const context = await chromium.launchPersistentContext(profileDir, launch);
  const pages = context.pages();
  const page = pages.length ? pages[0] : await context.newPage();
  try { await page.bringToFront(); } catch (err) { /* ignore */ }

  let ok = null;
  let lastLoginError = '';
  page.on('response', (response) => {
    let pathname = '';
    try { pathname = new URL(response.url()).pathname; } catch (err) { return; }
    if (!pathname.includes('/passenger/history')) return;
    response.json().then((payload) => {
      const errno = payload && payload.errno;
      if (errno === 0 || errno === '0') {
        ok = countOrders(payload, 0);
      } else if (errno !== undefined) {
        lastLoginError = String(payload.errmsg || errno).slice(0, 120);
      }
    }).catch(() => {});
  });

  say('浏览器已打开。请完成滴滴登录（扫码或短信验证码），然后点进「订单」页。');
  say('等待中…… 最多 ' + Math.round(timeoutMs / 1000) + ' 秒；登录成功后订单列表一出就会自动保存。');

  try {
    await page.goto(input.url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  } catch (err) {
    say('打开页面失败：' + (err && err.name ? err.name : 'Error') + '（可手动在窗口里打开滴滴）');
  }

  const deadline = Date.now() + timeoutMs;
  let lastHint = Date.now();
  while (Date.now() < deadline) {
    if (ok !== null) break;
    await page.waitForTimeout(1000);
    if (Date.now() - lastHint > 15000) {
      lastHint = Date.now();
      let where = '';
      try {
        const current = new URL(page.url());
        where = current.hash ? current.hash.split('?')[0] : current.pathname;
      } catch (err) { where = ''; }
      say('仍在等待订单列表' + (where ? '（当前：' + where + '）' : '') +
          (lastLoginError ? '；服务端提示：' + lastLoginError : ''));
    }
  }

  if (ok === null) {
    say('LOGIN_TIMEOUT 没有等到成功的订单列表请求');
    await context.close();
    process.exitCode = 4;
    return;
  }

  const finalUrl = page.url();
  const state = await context.storageState();
  const stateJson = JSON.stringify(state, null, 2) + '\n';
  fs.writeFileSync(input.state_path + '.tmp', stateJson, { mode: 0o600 });
  fs.chmodSync(input.state_path + '.tmp', 0o600);
  fs.renameSync(input.state_path + '.tmp', input.state_path);
  if (input.entry_url_path) {
    fs.writeFileSync(input.entry_url_path + '.tmp', finalUrl + '\n', { mode: 0o600 });
    fs.chmodSync(input.entry_url_path + '.tmp', 0o600);
    fs.renameSync(input.entry_url_path + '.tmp', input.entry_url_path);
  }
  say('LOGIN_OK orders=' + ok + ' cookies=' + state.cookies.length +
      ' origins=' + state.origins.length);
  say('登录态已保存到本机（0600）。之后 `didi fetch --browser` 不再需要登录。');
  await context.close();
}

main(readInput()).catch((err) => {
  say('LOGIN_ERROR ' + (err && err.message ? String(err.message).slice(0, 300) : 'unknown'));
  process.exitCode = 1;
});
