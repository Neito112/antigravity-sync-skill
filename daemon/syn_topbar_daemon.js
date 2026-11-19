#!/usr/bin/env node
'use strict';

/**
 * Antigravity Topbar "Syn" Daemon (Windows / macOS / Linux)
 * Connects to Antigravity CDP (DevToolsActivePort), injects a native-looking
 * "Syn" button into the Antigravity topbar ([data-testid="pane-titlebar-toolbar"]),
 * supports 2-way conflict-free Smart Sync for multi-machine usage, and provides
 * a configurable Auto-Sync interval (in minutes).
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

if (typeof globalThis.WebSocket !== 'function') {
  console.error('[syn-daemon] Node.js >= 22 with built-in WebSocket is required.');
  process.exit(1);
}

const ROOT_DIR = path.resolve(__dirname, '..');
const ENGINE_PY = path.join(ROOT_DIR, 'scripts', 'sync_engine.py');
const PID_FILE = path.join(ROOT_DIR, 'syn-daemon.pid');
const LOG_FILE = path.join(ROOT_DIR, 'syn-daemon.log');
const STATE_FILE = path.join(ROOT_DIR, 'syn-state.json');

function resolvePortFile() {
  if (process.env.AGY_PORT_FILE) return process.env.AGY_PORT_FILE;
  const home = os.homedir();
  let candidates;
  if (process.platform === 'win32') {
    const appData = process.env.APPDATA || path.join(home, 'AppData', 'Roaming');
    candidates = [
      path.join(appData, 'Antigravity', 'DevToolsActivePort'),
      path.join(appData, 'antigravity', 'DevToolsActivePort'),
    ];
  } else if (process.platform === 'darwin') {
    const appSupport = path.join(home, 'Library', 'Application Support');
    candidates = [
      path.join(appSupport, 'Antigravity', 'DevToolsActivePort'),
      path.join(appSupport, 'antigravity', 'DevToolsActivePort'),
    ];
  } else {
    const xdgConfig = process.env.XDG_CONFIG_HOME || path.join(home, '.config');
    candidates = [
      path.join(xdgConfig, 'Antigravity', 'DevToolsActivePort'),
      path.join(xdgConfig, 'antigravity', 'DevToolsActivePort'),
    ];
  }
  return candidates.find((p) => fs.existsSync(p)) || candidates[0];
}

function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}\n`;
  try {
    if (fs.existsSync(LOG_FILE) && fs.statSync(LOG_FILE).size > 512 * 1024) {
      fs.writeFileSync(LOG_FILE, '');
    }
    fs.appendFileSync(LOG_FILE, line);
  } catch (_) {}
  if (process.stdout.isTTY || process.env.AGY_DAEMON_STDOUT === '1') {
    process.stdout.write(line);
  }
}

function isPidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (_) {
    return false;
  }
}

function acquirePidLock() {
  if (fs.existsSync(PID_FILE)) {
    const existingPid = parseInt(fs.readFileSync(PID_FILE, 'utf8').trim(), 10);
    if (existingPid && existingPid !== process.pid && isPidAlive(existingPid)) {
      log(`Another syn-daemon instance is already running (PID ${existingPid}). Exiting.`);
      process.exit(0);
    }
  }
  fs.writeFileSync(PID_FILE, String(process.pid), 'utf8');
}

function removePidLock() {
  try {
    if (fs.existsSync(PID_FILE)) {
      const pid = parseInt(fs.readFileSync(PID_FILE, 'utf8').trim(), 10);
      if (pid === process.pid) fs.unlinkSync(PID_FILE);
    }
  } catch (_) {}
}

function normalizeRepoUrl(url) {
  if (!url || typeof url !== 'string') return null;
  return url.trim().replace(/\.git$/i, '');
}

function detectLocalRemoteUrl() {
  try {
    const gitConfig = path.join(os.homedir(), '.gemini', 'antigravity-sync-repo', '.git', 'config');
    if (!fs.existsSync(gitConfig)) return null;
    const raw = fs.readFileSync(gitConfig, 'utf8');
    const originBlock = raw.match(/\[remote "origin"\][\s\S]*?(?=\n\[|$)/i);
    if (!originBlock) return null;
    const urlMatch = originBlock[0].match(/^\s*url\s*=\s*(.+)$/m);
    return urlMatch ? normalizeRepoUrl(urlMatch[1]) : null;
  } catch (_) {
    return null;
  }
}

function readState() {
  const defaults = {
    lastSyncAt: null,
    lastAction: null,
    lastSummary: null,
    autoSyncMinutes: 0,
    remoteUrl: detectLocalRemoteUrl() || null,
    storageDir: readStorageDir(),
  };
  try {
    if (fs.existsSync(STATE_FILE)) {
      const parsed = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
      const merged = { ...defaults, ...parsed };
      if (!merged.remoteUrl) merged.remoteUrl = detectLocalRemoteUrl() || null;
      return merged;
    }
  } catch (_) {}
  return defaults;
}

function readStorageDir() {
  try {
    const cfg = path.join(ROOT_DIR, 'syn-config.json');
    if (fs.existsSync(cfg)) return JSON.parse(fs.readFileSync(cfg, 'utf8')).storage_dir || null;
  } catch (_) {}
  return null;
}

function saveState(patch) {
  const next = { ...readState(), ...patch };
  try {
    fs.writeFileSync(STATE_FILE, JSON.stringify(next, null, 2), 'utf8');
  } catch (_) {}
  return { ...next, storageDir: readStorageDir() };
}

function findPythonExe() {
  return process.platform === 'win32' ? 'python' : 'python3';
}

const connectedTargets = new Map();
let activeSyncPromise = null;
let autoSyncTimer = null;

function broadcast(expr) {
  for (const conn of connectedTargets.values()) {
    conn.send('Runtime.evaluate', { expression: expr, returnByValue: false });
  }
}

function runSyncEngine(action, extraArgs = []) {
  if (activeSyncPromise) return activeSyncPromise;
  activeSyncPromise = new Promise((resolve) => {
    const py = findPythonExe();
    const child = spawn(py, [ENGINE_PY, action, '--json', ...extraArgs], {
      cwd: ROOT_DIR,
      windowsHide: true,
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d.toString(); });
    child.stderr.on('data', (d) => { stderr += d.toString(); });
    child.on('close', (code) => {
      activeSyncPromise = null;
      let parsed = null;
      try {
        const lines = stdout.trim().split(/\r?\n/);
        parsed = JSON.parse(lines[lines.length - 1] || '{}');
      } catch (_) {}
      if (code === 0 && parsed && parsed.ok) {
        let summary = '';
        if (action === 'sync') {
          summary = `Smart Sync: ↑${parsed.pushed_up || 0} ↓${parsed.pulled_down || 0}` + (parsed.pushed ? ' (pushed)' : ' (up to date)');
        } else if (action === 'set-location') {
          summary = `Storage: ${parsed.storage_dir}`;
        } else if (action === 'backup') {
          summary = parsed.pushed ? `Pushed ${parsed.copied_files} files` : `Up to date (${parsed.copied_files} files)`;
        } else {
          summary = `Restored ${parsed.restored_files} files`;
        }
        const st = saveState({
          lastSyncAt: new Date().toISOString(),
          lastAction: action,
          lastSummary: summary,
          remoteUrl: normalizeRepoUrl(parsed.remote_url) || readState().remoteUrl,
        });
        resolve({ ...parsed, state: st });
      } else {
        resolve({
          ok: false,
          action,
          error: (parsed && parsed.error) || stderr.trim() || `Exit code ${code}`,
          state: readState(),
        });
      }
    });
  });
  return activeSyncPromise;
}

function scheduleAutoSync(minutes) {
  if (autoSyncTimer) {
    clearInterval(autoSyncTimer);
    autoSyncTimer = null;
  }
  const mins = Math.max(0, Math.min(1440, parseInt(minutes, 10) || 0));
  const st = saveState({ autoSyncMinutes: mins });
  if (mins > 0) {
    log(`Auto-Sync scheduled every ${mins} minute(s) (2-way conflict-free sync)`);
    autoSyncTimer = setInterval(async () => {
      log(`Auto-Sync tick (${mins}m)`);
      broadcast(`window.__AGY_SYN_SET_BUSY__ && window.__AGY_SYN_SET_BUSY__(true, 'Auto-syncing...')`);
      const result = await runSyncEngine('sync');
      log(`Auto-Sync finished: ok=${result.ok}`);
      broadcast(`window.__AGY_SYN_ON_RESULT__ && window.__AGY_SYN_ON_RESULT__(${JSON.stringify(result)})`);
    }, mins * 60 * 1000);
  } else {
    log('Auto-Sync disabled');
  }
  return st;
}

function buildInjectedClientScript(initialState) {
  const stateJson = JSON.stringify(initialState);
  return `(() => {
    window.__AGY_SYN_STATE__ = ${stateJson};

    const BTN_ID = 'agy-topbar-syn-btn';
    const POP_ID = 'agy-topbar-syn-popover';
    const STYLE_ID = 'agy-topbar-syn-style';

    if (!document.getElementById(STYLE_ID)) {
      const style = document.createElement('style');
      style.id = STYLE_ID;
      style.textContent = \`
        @keyframes agySynSpin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
        #\${BTN_ID} {
          -webkit-app-region: no-drag;
          display: inline-flex;
          align-items: center;
          gap: 5px;
          height: 26px;
          padding: 0 9px;
          border-radius: 6px;
          font-size: 12px;
          font-weight: 500;
          color: var(--foreground, #e4e4e7);
          background: rgba(255, 255, 255, 0.06);
          border: 1px solid rgba(255, 255, 255, 0.12);
          cursor: pointer;
          transition: background 140ms ease, border-color 140ms ease, color 140ms ease;
          user-select: none;
          margin-right: 4px;
        }
        #\${BTN_ID}:hover {
          background: rgba(56, 189, 248, 0.16);
          border-color: rgba(56, 189, 248, 0.45);
          color: #ffffff;
        }
        #\${BTN_ID}[data-syncing="1"] svg {
          animation: agySynSpin 0.9s linear infinite;
          color: #38bdf8;
        }
        #\${BTN_ID} .syn-auto-dot {
          width: 6px;
          height: 6px;
          border-radius: 999px;
          background: #4ade80;
          box-shadow: 0 0 6px rgba(74, 222, 128, 0.8);
        }
        #\${POP_ID} {
          position: fixed;
          top: 42px;
          right: 16px;
          width: 300px;
          z-index: 99999;
          border-radius: 12px;
          padding: 12px;
          background: rgba(20, 22, 28, 0.98);
          border: 1px solid rgba(255, 255, 255, 0.14);
          box-shadow: 0 14px 36px rgba(0, 0, 0, 0.5);
          color: #f4f4f5;
          font-family: system-ui, -apple-system, sans-serif;
          font-size: 12.5px;
          -webkit-app-region: no-drag;
        }
        #\${POP_ID} .syn-title {
          font-weight: 600;
          font-size: 13px;
          margin-bottom: 4px;
          display: flex;
          justify-content: space-between;
          align-items: center;
        }
        #\${POP_ID} .syn-meta {
          font-size: 11px;
          color: #a1a1aa;
          margin-bottom: 10px;
          line-height: 1.4;
          word-break: break-word;
        }
        #\${POP_ID} .syn-actions {
          display: flex;
          flex-direction: column;
          gap: 6px;
        }
        #\${POP_ID} button.syn-act {
          width: 100%;
          text-align: left;
          padding: 7px 10px;
          border-radius: 8px;
          border: 1px solid rgba(255, 255, 255, 0.1);
          background: rgba(255, 255, 255, 0.05);
          color: #f4f4f5;
          cursor: pointer;
          font-size: 12px;
          font-weight: 500;
          display: flex;
          align-items: center;
          justify-content: space-between;
        }
        #\${POP_ID} button.syn-act:hover:not(:disabled) {
          background: rgba(56, 189, 248, 0.18);
          border-color: rgba(56, 189, 248, 0.45);
        }
        #\${POP_ID} button.syn-act:disabled {
          opacity: 0.55;
          cursor: wait;
        }
        #\${POP_ID} .syn-auto-box {
          margin-top: 9px;
          padding: 8px 9px;
          border-radius: 8px;
          background: rgba(255, 255, 255, 0.04);
          border: 1px solid rgba(255, 255, 255, 0.08);
        }
        #\${POP_ID} .syn-auto-label {
          font-size: 11px;
          color: #d4d4d8;
          margin-bottom: 6px;
          display: flex;
          justify-content: space-between;
          align-items: center;
        }
        #\${POP_ID} .syn-presets {
          display: flex;
          gap: 4px;
          margin-bottom: 6px;
        }
        #\${POP_ID} button.syn-pill {
          flex: 1;
          padding: 4px 0;
          border-radius: 6px;
          border: 1px solid rgba(255, 255, 255, 0.12);
          background: rgba(255, 255, 255, 0.04);
          color: #e4e4e7;
          font-size: 11px;
          cursor: pointer;
        }
        #\${POP_ID} button.syn-pill.active {
          background: rgba(56, 189, 248, 0.25);
          border-color: rgba(56, 189, 248, 0.65);
          color: #ffffff;
          font-weight: 600;
        }
        #\${POP_ID} .syn-custom-row {
          display: flex;
          gap: 5px;
          align-items: center;
        }
        #\${POP_ID} input.syn-min-input {
          width: 72px;
          padding: 4px 7px;
          border-radius: 6px;
          border: 1px solid rgba(255, 255, 255, 0.16);
          background: rgba(0, 0, 0, 0.35);
          color: #ffffff;
          font-size: 12px;
          outline: none;
        }
        #\${POP_ID} button.syn-set-btn {
          flex: 1;
          padding: 5px 8px;
          border-radius: 6px;
          border: 1px solid rgba(56, 189, 248, 0.45);
          background: rgba(56, 189, 248, 0.18);
          color: #ffffff;
          font-size: 11.5px;
          font-weight: 500;
          cursor: pointer;
        }
        #\${POP_ID} button.syn-set-btn:hover {
          background: rgba(56, 189, 248, 0.3);
        }
        #\${POP_ID} .syn-status {
          margin-top: 8px;
          font-size: 11.5px;
          padding: 6px 8px;
          border-radius: 6px;
          background: rgba(255, 255, 255, 0.04);
          color: #d4d4d8;
          display: none;
        }
      \`;
      document.head.appendChild(style);
    }

    function formatTime(iso) {
      if (!iso) return 'Never';
      try { return new Date(iso).toLocaleString(); } catch (_) { return iso; }
    }

    function closePopover() {
      const pop = document.getElementById(POP_ID);
      if (pop) pop.remove();
    }

    function renderButtonLabel() {
      const btn = document.getElementById(BTN_ID);
      if (!btn) return;
      const mins = (window.__AGY_SYN_STATE__ && window.__AGY_SYN_STATE__.autoSyncMinutes) || 0;
      const nextText = mins > 0 ? 'Syn · ' + mins + 'm' : 'Syn';
      const nextDisplay = mins > 0 ? 'inline-block' : 'none';
      const labelSpan = btn.querySelector('.syn-btn-label');
      const dot = btn.querySelector('.syn-auto-dot');
      if (labelSpan && labelSpan.textContent !== nextText) labelSpan.textContent = nextText;
      if (dot && dot.style.display !== nextDisplay) dot.style.display = nextDisplay;
    }

    function renderAutoSyncUI() {
      const pop = document.getElementById(POP_ID);
      if (!pop) return;
      const mins = (window.__AGY_SYN_STATE__ && window.__AGY_SYN_STATE__.autoSyncMinutes) || 0;
      const badge = pop.querySelector('.syn-auto-badge');
      if (badge) badge.textContent = mins > 0 ? 'Every ' + mins + 'm' : 'Off';
      const inp = pop.querySelector('.syn-min-input');
      if (inp && document.activeElement !== inp) inp.value = mins > 0 ? String(mins) : '';
      pop.querySelectorAll('button.syn-pill').forEach((p) => {
        p.classList.toggle('active', Number(p.dataset.mins) === mins);
      });
    }

    function setSyncing(isSyncing, statusText, isError) {
      const btn = document.getElementById(BTN_ID);
      if (btn) btn.setAttribute('data-syncing', isSyncing ? '1' : '0');
      const pop = document.getElementById(POP_ID);
      if (!pop) return;
      pop.querySelectorAll('button.syn-act').forEach((b) => { b.disabled = isSyncing; });
      const stEl = pop.querySelector('.syn-status');
      if (stEl && statusText) {
        stEl.style.display = 'block';
        stEl.style.color = isError ? '#fca5a5' : '#86efac';
        stEl.textContent = statusText;
      }
      const metaEl = pop.querySelector('.syn-meta');
      if (metaEl && window.__AGY_SYN_STATE__) {
        const s = window.__AGY_SYN_STATE__;
        metaEl.innerHTML = 'Last sync: ' + formatTime(s.lastSyncAt) + (s.lastSummary ? '<br>' + s.lastSummary : '');
      }
    }

    window.__AGY_SYN_SET_BUSY__ = (busy, text) => {
      setSyncing(busy, text, false);
    };

    window.__AGY_SYN_ON_STATE__ = (st) => {
      if (st) window.__AGY_SYN_STATE__ = st;
      renderButtonLabel();
      renderAutoSyncUI();
    };

    window.__AGY_SYN_ON_RESULT__ = (res) => {
      if (res && res.state) window.__AGY_SYN_STATE__ = res.state;
      renderButtonLabel();
      renderAutoSyncUI();
      if (res && res.ok) {
        let msg = '';
        if (res.action === 'sync') {
          msg = 'Smart Sync: ↑' + (res.pushed_up || 0) + ' ↓' + (res.pulled_down || 0) + (res.pushed ? ' (pushed)' : ' (up to date)');
        } else if (res.action === 'backup') {
          msg = res.pushed ? 'Backed up & pushed ' + res.copied_files + ' files!' : 'Already up to date (' + res.copied_files + ' files)';
        } else {
          msg = 'Restored ' + res.restored_files + ' files!';
        }
        setSyncing(false, msg, false);
      } else {
        setSyncing(false, 'Error: ' + ((res && res.error) || 'Unknown error'), true);
      }
    };

    function sendTrigger(payload) {
      if (typeof window.agySynTrigger === 'function') {
        window.agySynTrigger(JSON.stringify(payload));
      }
    }

    function openPopover(anchorEl) {
      if (document.getElementById(POP_ID)) {
        closePopover();
        return;
      }
      const rect = anchorEl.getBoundingClientRect();
      const s = window.__AGY_SYN_STATE__ || {};
      const mins = s.autoSyncMinutes || 0;
      const pop = document.createElement('div');
      pop.id = POP_ID;
      pop.style.top = Math.round(rect.bottom + 8) + 'px';
      pop.style.right = Math.max(12, Math.round(window.innerWidth - rect.right)) + 'px';
      pop.innerHTML = \`
        <div class="syn-title">
          <span>Antigravity Sync</span>
          <span style="font-size:10.5px; color:#38bdf8;">Multi-PC Safe</span>
        </div>
        <div class="syn-meta">Last sync: \${formatTime(s.lastSyncAt)}\${s.lastSummary ? '<br>' + s.lastSummary : ''}</div>
        <div class="syn-actions">
          <button class="syn-act" data-act="sync">
            <span>🔄 Smart Sync Now (2-Way Merge)</span>
          </button>
          <button class="syn-act" data-act="backup">
            <span>⬆ Force Full Backup</span>
          </button>
          <button class="syn-act" data-act="restore">
            <span>⬇ Pull & Restore to Local</span>
          </button>
          <button class="syn-act" data-act="open">
            <span>↗ Open GitHub Backup Repo</span>
          </button>
        </div>
        <div class="syn-auto-box">
          <div class="syn-auto-label">
            <span>⏱ Auto-Sync (phút / min)</span>
            <span class="syn-auto-badge" style="color:#4ade80; font-weight:600;">\${mins > 0 ? 'Every ' + mins + 'm' : 'Off'}</span>
          </div>
          <div class="syn-presets">
            <button type="button" class="syn-pill" data-mins="0">Off</button>
            <button type="button" class="syn-pill" data-mins="5">5m</button>
            <button type="button" class="syn-pill" data-mins="15">15m</button>
            <button type="button" class="syn-pill" data-mins="30">30m</button>
          </div>
          <div class="syn-custom-row">
            <input type="number" min="0" max="1440" placeholder="Phút..." class="syn-min-input" value="\${mins > 0 ? mins : ''}" />
            <button type="button" class="syn-set-btn">Set Auto-Sync</button>
          </div>
        </div>
        <div class="syn-auto-box">
          <div class="syn-auto-label">
            <span>📁 Thư mục lưu (ổ tùy chọn)</span>
          </div>
          <div class="syn-custom-row">
            <input type="text" placeholder="D:\\AntigravitySync" class="syn-min-input syn-path-input" style="width:150px" value="" />
            <button type="button" class="syn-set-btn syn-path-btn">Set</button>
          </div>
          <div style="font-size:10.5px;color:#a1a1aa;margin-top:5px;">Đường dẫn ảo luôn giữ tại ~/.gemini/antigravity-sync-repo</div>
        </div>
        <div class="syn-status"></div>
      \`;
      document.body.appendChild(pop);
      const pathInput = pop.querySelector('.syn-path-input');
      pathInput.value = s.storageDir || '';
      pop.querySelector('.syn-path-btn').addEventListener('click', () => {
        const v = pathInput.value.trim();
        if (!v) return;
        setSyncing(true, 'Moving sync folder to ' + v + ' ...', false);
        sendTrigger({ action: 'set_location', path: v });
      });
      renderAutoSyncUI();

      pop.querySelector('[data-act="sync"]').addEventListener('click', () => {
        setSyncing(true, 'Running 2-way conflict-free sync...', false);
        sendTrigger({ action: 'sync' });
      });

      pop.querySelector('[data-act="backup"]').addEventListener('click', () => {
        setSyncing(true, 'Backing up & pushing to GitHub...', false);
        sendTrigger({ action: 'backup' });
      });

      pop.querySelector('[data-act="restore"]').addEventListener('click', (e) => {
        const btn = e.currentTarget;
        if (btn.dataset.armed !== '1') {
          btn.dataset.armed = '1';
          btn.querySelector('span').textContent = '⚠ Click again to confirm Restore';
          setTimeout(() => {
            if (btn.isConnected) {
              btn.dataset.armed = '0';
              btn.querySelector('span').textContent = '⬇ Pull & Restore to Local';
            }
          }, 4000);
          return;
        }
        btn.dataset.armed = '0';
        btn.querySelector('span').textContent = '⬇ Pull & Restore to Local';
        setSyncing(true, 'Pulling & restoring from GitHub...', false);
        sendTrigger({ action: 'restore' });
      });

      pop.querySelector('[data-act="open"]').addEventListener('click', () => {
        const url = (window.__AGY_SYN_STATE__ && window.__AGY_SYN_STATE__.remoteUrl) || 'https://github.com';
        window.open(url, '_blank');
        closePopover();
      });

      pop.querySelectorAll('button.syn-pill').forEach((p) => {
        p.addEventListener('click', () => {
          const m = Number(p.dataset.mins) || 0;
          sendTrigger({ action: 'set_interval', minutes: m });
          setSyncing(false, m > 0 ? 'Auto-Sync set to every ' + m + ' min' : 'Auto-Sync turned off', false);
        });
      });

      pop.querySelector('.syn-set-btn').addEventListener('click', () => {
        const inp = pop.querySelector('.syn-min-input');
        const m = Math.max(0, Math.min(1440, parseInt(inp.value, 10) || 0));
        sendTrigger({ action: 'set_interval', minutes: m });
        setSyncing(false, m > 0 ? 'Auto-Sync set to every ' + m + ' min' : 'Auto-Sync turned off', false);
      });
    }

    function ensureSynButton() {
      const existing = document.getElementById(BTN_ID);
      if (existing) {
        if (existing.querySelector('.syn-btn-label')) {
          renderButtonLabel();
          return;
        }
        existing.remove();
      }
      const toolbar = document.querySelector('[data-testid="pane-titlebar-toolbar"]');
      if (!toolbar) return;
      const inner = toolbar.querySelector('div') || toolbar;

      const btn = document.createElement('button');
      btn.id = BTN_ID;
      btn.type = 'button';
      btn.title = 'Antigravity GitHub Sync (2-Way Safe + Auto-Sync)';
      btn.innerHTML = \`
        <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
          <path d="M3 3v5h5"/>
          <path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/>
          <path d="M16 16h5v5"/>
        </svg>
        <span class="syn-btn-label">Syn</span>
        <span class="syn-auto-dot" style="display:none;"></span>
      \`;
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        openPopover(btn);
      });
      inner.insertBefore(btn, inner.firstChild);
      renderButtonLabel();
    }

    if (!window.__AGY_SYN_INIT__) {
      window.__AGY_SYN_INIT__ = true;
      document.addEventListener('click', (e) => {
        const pop = document.getElementById(POP_ID);
        const btn = document.getElementById(BTN_ID);
        if (pop && !pop.contains(e.target) && (!btn || !btn.contains(e.target))) {
          closePopover();
        }
      });
      // Lightweight polling instead of subtree MutationObserver to avoid freezing React/Electron streaming
      setInterval(ensureSynButton, 2500);
    }

    ensureSynButton();
  })();`;
}

function readPortFile() {
  const portFile = resolvePortFile();
  if (!fs.existsSync(portFile)) return null;
  const raw = fs.readFileSync(portFile, 'utf8').trim();
  const [portLine] = raw.split(/\r?\n/);
  const port = parseInt(portLine, 10);
  return Number.isFinite(port) ? port : null;
}

async function fetchTargets(port) {
  const res = await fetch(`http://127.0.0.1:${port}/json/list`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function connectTarget(target) {
  if (connectedTargets.has(target.id)) return;
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let nextId = 1;
  const send = (method, params = {}) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ id: nextId++, method, params }));
    }
  };

  connectedTargets.set(target.id, { ws, send });

  ws.onopen = () => {
    log(`Connected to target: ${target.title}`);
    send('Runtime.enable');
    send('Runtime.addBinding', { name: 'agySynTrigger' });
    send('Runtime.evaluate', {
      expression: buildInjectedClientScript(readState()),
      returnByValue: false,
    });
  };

  ws.onmessage = async (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.method === 'Runtime.bindingCalled' && msg.params && msg.params.name === 'agySynTrigger') {
      let payload = {};
      try { payload = JSON.parse(msg.params.payload || '{}'); } catch (_) {}

      if (payload.action === 'set_interval') {
        const st = scheduleAutoSync(payload.minutes);
        broadcast(`window.__AGY_SYN_ON_STATE__ && window.__AGY_SYN_ON_STATE__(${JSON.stringify(st)})`);
        return;
      }

      if (payload.action === 'set_location') {
        const dir = String(payload.path || '').trim();
        if (!dir) return;
        log(`Set storage location: ${dir}`);
        const result = await runSyncEngine('set-location', ['--path', dir]);
        broadcast(`window.__AGY_SYN_ON_RESULT__ && window.__AGY_SYN_ON_RESULT__(${JSON.stringify(result)})`);
        return;
      }

      const action = ['backup', 'restore', 'sync'].includes(payload.action) ? payload.action : 'sync';
      log(`Triggered ${action} from topbar Syn button`);
      const result = await runSyncEngine(action);
      log(`Completed ${action}: ok=${result.ok}`);
      broadcast(`window.__AGY_SYN_ON_RESULT__ && window.__AGY_SYN_ON_RESULT__(${JSON.stringify(result)})`);
    }
  };

  ws.onclose = () => connectedTargets.delete(target.id);
  ws.onerror = () => connectedTargets.delete(target.id);
}

async function pollLoop() {
  try {
    const port = readPortFile();
    if (port) {
      const targets = await fetchTargets(port);
      for (const t of targets) {
        if (t.type === 'page' && t.url && t.url.startsWith('https://127.0.0.1') && t.webSocketDebuggerUrl) {
          connectTarget(t);
        }
      }
    }
  } catch (_) {}
}

function main() {
  acquirePidLock();
  process.on('exit', removePidLock);
  process.on('SIGINT', () => { removePidLock(); process.exit(0); });
  process.on('SIGTERM', () => { removePidLock(); process.exit(0); });

  const initialState = readState();
  if (initialState.autoSyncMinutes > 0) {
    scheduleAutoSync(initialState.autoSyncMinutes);
  }

  log('syn_topbar_daemon started');
  pollLoop();
  setInterval(pollLoop, 3000);
}

main();
