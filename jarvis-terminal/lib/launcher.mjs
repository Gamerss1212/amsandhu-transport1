// launcher.mjs - helpers for the always-on watchdog (launch.mjs). Pure functions where possible so they can be tested.
// Nothing here trades. It only starts, watches and restarts the local Jarvis server and opens its window.
import { existsSync, readFileSync, readdirSync, statSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
import { parseEnv } from 'node:util';
import { spawnSync } from 'node:child_process';

// Server exit codes understood by the watchdog.
export const EXIT_STOP = 0;      // owner asked Jarvis to shut down: do not restart
export const EXIT_RESTART = 3;   // server asked for a clean restart (for example after a settings change)

/** Read KEY=VALUE pairs from .env without touching process.env (the server re-reads .env on every start). */
export function readEnvFile(path) {
  try { return parseEnv(readFileSync(path, 'utf8')); } catch { return {}; }
}

/** Restart delay in ms after the n-th consecutive crash (1-based). Grows to one minute and stays there. */
export function backoffMs(n) {
  const steps = [2000, 5000, 10000, 30000, 60000];
  return steps[Math.min(Math.max(n, 1), steps.length) - 1];
}

/** Candidate Chromium-based browsers (Chrome first, then Edge, then Chromium) for this platform. */
export function browserCandidates(platform = process.platform, env = process.env) {
  if (platform === 'win32') {
    const pf = env.ProgramFiles || 'C:\\Program Files', pf86 = env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)', local = env.LOCALAPPDATA || '';
    return [
      join(pf, 'Google\\Chrome\\Application\\chrome.exe'), join(pf86, 'Google\\Chrome\\Application\\chrome.exe'),
      local && join(local, 'Google\\Chrome\\Application\\chrome.exe'),
      join(pf86, 'Microsoft\\Edge\\Application\\msedge.exe'), join(pf, 'Microsoft\\Edge\\Application\\msedge.exe'),
    ].filter(Boolean);
  }
  if (platform === 'darwin') return [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
  ];
  return ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'microsoft-edge'];
}

/** First installed browser from browserCandidates, or null (then the default browser is used). */
export function findBrowser(platform = process.platform, env = process.env) {
  if (env.JARVIS_BROWSER && existsSync(env.JARVIS_BROWSER)) return env.JARVIS_BROWSER;
  for (const c of browserCandidates(platform, env)) {
    if (c.includes('/') || c.includes('\\')) { if (existsSync(c)) return c; continue; }
    const r = spawnSync('which', [c], { encoding: 'utf8' });
    if (r.status === 0 && r.stdout.trim()) return r.stdout.trim();
  }
  return null;
}

/**
 * Flags for the dedicated Jarvis window. A separate profile keeps these permissions away from the owner's normal browsing:
 * - --app: a clean window without tabs or address bar
 * - autoplay + fake media UI: Jarvis can speak and listen without the owner clicking "Allow" every time
 * - no background throttling: voice and timers keep working while the window is minimized
 */
export function browserArgs(url, profileDir) {
  return [
    `--app=${url}`, `--user-data-dir=${profileDir}`, '--no-first-run', '--no-default-browser-check',
    '--autoplay-policy=no-user-gesture-required', '--use-fake-ui-for-media-stream',
    '--disable-background-timer-throttling', '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows',
    '--window-size=1600,960',
  ];
}

/** Command that keeps the computer from idle-sleeping while the watchdog (pid) is alive. null if unsupported. */
export function keepAwakeCommand(pid, platform = process.platform) {
  if (platform === 'win32') {
    const ps = `$k=Add-Type -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint f);' -Name P -Namespace JarvisAwake -PassThru;` +
      `while(Get-Process -Id ${pid} -ErrorAction SilentlyContinue){[void]$k::SetThreadExecutionState(0x80000001);Start-Sleep -Seconds 50}`;
    return ['powershell.exe', ['-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-Command', ps]];
  }
  if (platform === 'darwin') return ['caffeinate', ['-i', '-w', String(pid)]];
  if (platform === 'linux') return ['systemd-inhibit', ['--what=idle', '--who=Jarvis', '--why=Jarvis is watching the markets', 'tail', `--pid=${pid}`, '-f', '/dev/null']];
  return null;
}

/** Log file for a given day; old logs beyond keepDays are deleted. */
export function logPath(dir, now = new Date()) { return join(dir, `jarvis-${now.toISOString().slice(0, 10)}.log`); }
export function pruneLogs(dir, keepDays = 14, now = Date.now()) {
  let removed = 0;
  try {
    for (const f of readdirSync(dir)) {
      if (!/^jarvis-\d{4}-\d\d-\d\d\.log$/.test(f)) continue;
      const p = join(dir, f);
      if (now - statSync(p).mtimeMs > keepDays * 86400000) { unlinkSync(p); removed++; }
    }
  } catch { /* no log folder yet */ }
  return removed;
}

/** True if a process with this pid exists (used for the single-instance lock). */
export function alive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
}
