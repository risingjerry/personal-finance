'use strict';
// Keep-alive: a per-user launchd agent that touches the Alipay record page every
// few minutes so the web session does not time out from idleness.
//
// The server-side session can expire while idle. Periodic reads may reduce
// idle expiry, but cannot guarantee that a session stays logged in.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { CliError, codes } = require('./errors');
const { layout, ensureLayout, ensureDir, writeFileAtomic, fileExists } = require('./util');

const LABEL = 'com.alipay-cli.keepalive';
const DEFAULT_INTERVAL = 600; // seconds
const MIN_INTERVAL = 120;

function launchAgentsDir() {
  return path.join(os.homedir(), 'Library', 'LaunchAgents');
}

function plistPath() {
  return path.join(launchAgentsDir(), `${LABEL}.plist`);
}

function cliEntry() {
  return path.resolve(__dirname, '..', 'bin', 'alipay');
}

function guiDomain() {
  return `gui/${process.getuid()}`;
}

/** Absolute invocation used by the agent; no PATH assumptions. */
function agentCommand({ home }) {
  return [process.execPath, cliEntry(), 'auth', 'status', '--launch', '--quiet', '--home', home];
}

function buildPlist({ home, intervalSeconds, logPath }) {
  const args = agentCommand({ home }).map((a) => `    <string>${escapeXml(a)}</string>`).join('\n');
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
${args}
  </array>
  <key>StartInterval</key>
  <integer>${intervalSeconds}</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>${escapeXml(logPath)}</string>
  <key>StandardErrorPath</key>
  <string>${escapeXml(logPath)}</string>
</dict>
</plist>
`;
}

function escapeXml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function launchctl(args, { allowFailure = false } = {}) {
  try {
    const out = execFileSync('/bin/launchctl', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    return { ok: true, out: String(out || '').trim() };
  } catch (err) {
    if (allowFailure) return { ok: false, out: '', err: String(err.stderr || err.message || err).trim() };
    throw new CliError(codes.INTERNAL, `launchctl ${args.join(' ')} failed`, { stderr: String(err.stderr || err.message || err).trim() });
  }
}

function isLoaded() {
  const r = launchctl(['print', `${guiDomain()}/${LABEL}`], { allowFailure: true });
  return r.ok;
}

function install({ home, intervalSeconds = DEFAULT_INTERVAL }) {
  const interval = Math.max(MIN_INTERVAL, Math.round(Number(intervalSeconds) || DEFAULT_INTERVAL));
  const L = ensureLayout(home);
  ensureDir(L.logs, 0o700);
  ensureDir(launchAgentsDir(), 0o755);
  const logPath = path.join(L.logs, 'keepalive.log');
  const file = plistPath();
  writeFileAtomic(file, buildPlist({ home, intervalSeconds: interval, logPath }), 0o644);

  // Replace any previous registration so interval changes take effect.
  launchctl(['bootout', `${guiDomain()}/${LABEL}`], { allowFailure: true });
  let boot = launchctl(['bootstrap', guiDomain(), file], { allowFailure: true });
  if (!boot.ok) {
    // Older macOS: kickstart the legacy way.
    launchctl(['load', '-w', file], { allowFailure: true });
  }
  launchctl(['enable', `${guiDomain()}/${LABEL}`], { allowFailure: true });
  launchctl(['kickstart', '-k', `${guiDomain()}/${LABEL}`], { allowFailure: true });
  return { file, label: LABEL, intervalSeconds: interval, logPath, loaded: isLoaded() };
}

function uninstall() {
  const file = plistPath();
  const wasLoaded = isLoaded();
  launchctl(['bootout', `${guiDomain()}/${LABEL}`], { allowFailure: true });
  if (fileExists(file)) fs.unlinkSync(file);
  return { file, removed: !fileExists(file), wasLoaded };
}

function readTail(file, lines = 3) {
  try {
    const text = fs.readFileSync(file, 'utf8').trim().split('\n').filter(Boolean);
    return text.slice(-lines).map((l) => {
      try {
        return JSON.parse(l);
      } catch {
        return { raw: l.slice(0, 300) };
      }
    });
  } catch {
    return [];
  }
}

function status({ home }) {
  const L = layout(home);
  const file = plistPath();
  const loaded = isLoaded();
  let intervalSeconds = null;
  let args = null;
  if (fileExists(file)) {
    const text = fs.readFileSync(file, 'utf8');
    const m = /<key>StartInterval<\/key>\s*<integer>(\d+)<\/integer>/.exec(text);
    intervalSeconds = m ? Number(m[1]) : null;
    const arr = /<key>ProgramArguments<\/key>\s*<array>([\s\S]*?)<\/array>/.exec(text);
    args = arr ? [...arr[1].matchAll(/<string>([^<]*)<\/string>/g)].map((x) => x[1]) : null;
  }
  const tail = readTail(path.join(L.logs, 'keepalive.log'));
  const last = tail.length ? tail[tail.length - 1] : null;
  return {
    installed: fileExists(file),
    loaded,
    label: LABEL,
    plist: file,
    intervalSeconds,
    command: args,
    lastRun: last ? (last.meta && last.meta.generatedAt) || last.generatedAt || null : null,
    lastSession: last && last.data ? last.data.session : null,
    log: path.join(L.logs, 'keepalive.log'),
    hint: loaded
      ? null
      : 'run `alipay keepalive install` so the session survives idle time, then `alipay auth login` once more if it already expired',
  };
}

module.exports = { install, uninstall, status, buildPlist, LABEL, DEFAULT_INTERVAL };
