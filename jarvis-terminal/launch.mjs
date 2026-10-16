// launch.mjs - keeps Jarvis online 24/7 without any button presses.
// Starts server.mjs, restarts it after a crash or a hang, opens the Jarvis window (mic and speech allowed without clicks),
// keeps the computer from idle-sleeping, and writes daily logs to data/logs/. Close this window to stop Jarvis.
// Flags: --no-browser (never open the window), --autostart (started by Windows at sign-in).
import { spawn } from 'node:child_process';
import { createWriteStream, existsSync, mkdirSync, readFileSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readEnvFile, backoffMs, findBrowser, browserArgs, keepAwakeCommand, logPath, pruneLogs, alive, EXIT_STOP, EXIT_RESTART } from './lib/launcher.mjs';

const major = Number(process.versions.node.split('.')[0]);
if (major < 24) { console.error(`Jarvis needs Node.js 24 or newer (this computer has ${process.versions.node}). Install it from https://nodejs.org and start again.`); process.exit(1); }

const root = dirname(fileURLToPath(import.meta.url)), flags = new Set(process.argv.slice(2));
const fileEnv = readEnvFile(join(root, '.env')), setting = k => process.env[k] ?? fileEnv[k];
const port = Number(setting('PORT') || 4317);
if (!Number.isInteger(port) || port < 1024 || port > 65535) { console.error('PORT must be a whole number between 1024 and 65535.'); process.exit(1); }
const url = `http://127.0.0.1:${port}`, dataDir = setting('JARVIS_DATA_DIR') || join(root, 'data'), logDir = join(dataDir, 'logs');
const openWindowMode = flags.has('--no-browser') ? 'never' : (setting('JARVIS_OPEN_WINDOW') || 'always');
mkdirSync(logDir, { recursive: true });

let logDay = '', logStream = null;
function writeLog(chunk) {
  const now = new Date(), day = now.toISOString().slice(0, 10);
  if (day !== logDay) { logStream?.end(); logDay = day; logStream = createWriteStream(logPath(logDir, now), { flags: 'a' }); pruneLogs(logDir); }
  logStream.write(chunk);
}
function say(message) { const line = `[watchdog ${new Date().toISOString()}] ${message}\n`; process.stdout.write(line); writeLog(line); }

async function healthy() {
  try {
    const r = await fetch(`${url}/healthz`, { signal: AbortSignal.timeout(4000) });
    if (r.ok) return (await r.json()).app === 'jarvis-terminal';
    if (r.status !== 404) return false;
    const page = await fetch(url, { signal: AbortSignal.timeout(4000) });   // older server without /healthz
    return page.ok && (await page.text()).includes('JARVIS');
  } catch { return false; }
}

let windowOpened = false;
function openWindow() {
  if (windowOpened || openWindowMode === 'never') return;
  windowOpened = true;
  const browser = findBrowser();
  const [cmd, args] = browser ? [browser, browserArgs(url, join(dataDir, 'browser-profile'))]
    : process.platform === 'win32' ? ['cmd.exe', ['/d', '/c', 'start', '', url]] : process.platform === 'darwin' ? ['open', [url]] : ['xdg-open', [url]];
  const p = spawn(cmd, args, { detached: true, stdio: 'ignore' });
  p.on('error', () => say(`Could not open a window. Open ${url} in your browser.`)); p.unref();
  say(browser ? `Opened the Jarvis window (${basename(browser)}).` : `Opened ${url} in your default browser.`);
}

// Single instance: a second launch just opens the window of the Jarvis that is already running.
// The running watchdog rewrites its lock every 20 s, so a lock older than 90 s (or a dead pid) is stale.
const lockFile = join(dataDir, 'watchdog.pid');
let otherPid = 0, lockAge = Infinity;
try { otherPid = Number(readFileSync(lockFile, 'utf8')); lockAge = Date.now() - statSync(lockFile).mtimeMs; } catch { /* first run */ }
if (otherPid !== process.pid && alive(otherPid) && lockAge < 90000) { say('Jarvis is already running.'); openWindow(); process.exit(0); }
if (await healthy()) { say(`A Jarvis server is already answering at ${url} (started outside the watchdog).`); openWindow(); process.exit(0); }
const touchLock = () => { try { writeFileSync(lockFile, String(process.pid)); } catch { /* data folder unavailable */ } };
touchLock();

if (setting('JARVIS_KEEP_AWAKE') !== '0') {
  const k = keepAwakeCommand(process.pid);
  if (k) { const p = spawn(k[0], k[1], { stdio: 'ignore', windowsHide: true }); p.on('error', () => {}); }
}

let child = null, stopping = false, crashes = 0, startedAt = 0, misses = 0;
function startServer() {
  startedAt = Date.now(); misses = 0;
  // .env is re-read on every start, so settings edits apply after a restart.
  child = spawn(process.execPath, [...(existsSync(join(root, '.env')) ? ['--env-file=.env'] : []), 'server.mjs'], {
    cwd: root, env: { ...process.env, JARVIS_SUPERVISED: '1' }, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
  child.stdout.on('data', d => { process.stdout.write(d); writeLog(d); });
  child.stderr.on('data', d => { process.stderr.write(d); writeLog(d); });
  child.on('error', e => say(`Could not start the server: ${e.message}`));
  child.on('exit', onExit);
}
function onExit(code, signal) {
  child = null;
  if (stopping) return finish();
  if (code === EXIT_STOP) { say('Jarvis was shut down from the app.'); return finish(); }
  if (code === EXIT_RESTART) { say('Restarting Jarvis as requested.'); return startServer(); }
  if (Date.now() - startedAt > 10 * 60000) crashes = 0;
  const wait = backoffMs(++crashes);
  say(`Jarvis stopped unexpectedly (${signal || `exit code ${code}`}). Restarting in ${wait / 1000} s (attempt ${crashes}).`);
  setTimeout(() => { if (!stopping) startServer(); }, wait);
}
function finish() {
  try { if (Number(readFileSync(lockFile, 'utf8')) === process.pid) unlinkSync(lockFile); } catch { /* already gone */ }
  logStream?.end(() => process.exit(0));
  setTimeout(() => process.exit(0), 1500).unref();
}
function stop() {
  if (stopping) return;
  stopping = true; say('Stopping Jarvis...');
  if (!child) return finish();
  child.kill('SIGTERM');
  setTimeout(() => { child?.kill('SIGKILL'); finish(); }, 8000).unref();
}
process.on('SIGINT', stop); process.on('SIGTERM', stop); process.on('SIGHUP', stop);

say(`Starting Jarvis${flags.has('--autostart') ? ' (automatic start at sign-in)' : ''}. Keep this window open; closing it stops Jarvis.`);
startServer();
for (let i = 0; i < 240 && child && !(await healthy()); i++) await new Promise(r => setTimeout(r, 250));
if (child && await healthy()) { say(`Jarvis is online at ${url} (paper trading only).`); openWindow(); }

// Hang detection: three missed health checks in a row (about a minute) and the server is restarted.
setInterval(async () => {
  if (!stopping) touchLock();
  if (!child || stopping || Date.now() - startedAt < 30000) return;
  if (await healthy()) { misses = 0; openWindow(); return; }
  if (++misses >= 3) { say('Jarvis stopped responding for about a minute. Restarting it.'); misses = 0; child?.kill('SIGKILL'); }
}, 20000);
