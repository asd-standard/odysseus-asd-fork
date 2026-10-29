// static/js/backupAdmin.js — Admin full-instance backup panel
import uiModule from './ui.js';

function el(id) { return document.getElementById(id); }
function esc(value) { return uiModule.esc(String(value ?? '')); }

function fmtDate(value) {
  if (!value) return 'Never';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function fmtBytes(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return '0 B';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

function setMsg(text, kind) {
  const node = el('backup-msg');
  if (!node) return;
  node.textContent = text || '';
  node.className = kind === 'error' ? 'admin-error' : (kind === 'success' ? 'admin-success' : '');
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', ...options });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const message = data && (data.detail || data.message);
    throw new Error(message || `Request failed (${response.status})`);
  }
  return data;
}

let bound = false;
let lastStatus = null;

function renderRoots(roots) {
  const list = el('backup-root-list');
  const info = el('backup-roots');
  const safeRoots = Array.isArray(roots) ? roots : [];
  if (list) {
    list.innerHTML = safeRoots
      .map(root => `<option value="${esc(root.path)}">${esc(root.host_path || root.path)}</option>`)
      .join('');
  }
  if (!info) return;
  if (!safeRoots.length) {
    info.textContent = 'No backup roots are mounted. Set APP_BACKUP_DIR in .env and restart the container.';
    return;
  }
  info.innerHTML = safeRoots.map(root => {
    const host = root.host_path ? ` · host: ${esc(root.host_path)}` : '';
    const free = root.free_bytes != null ? ` · ${esc(fmtBytes(root.free_bytes))} free` : '';
    const state = root.exists
      ? (root.writable ? 'mounted, writable' : 'mounted, not writable')
      : 'not mounted';
    return `<div>${esc(root.path)} — ${esc(state)}${host}${free}</div>`;
  }).join('');
}

function renderStatus(data) {
  lastStatus = data;
  const node = el('backup-status');
  if (!node) return;
  const state = data.state || {};
  const settings = data.settings || {};
  const lines = [];
  lines.push(`<div><strong>Status:</strong> ${state.running ? '<span class="admin-error">backup in progress</span>' : 'idle'}</div>`);
  lines.push(`<div><strong>Last success:</strong> ${esc(fmtDate(state.last_success))}</div>`);
  lines.push(`<div><strong>Last attempt:</strong> ${esc(fmtDate(state.last_run))}</div>`);
  lines.push(`<div><strong>Next scheduled:</strong> ${esc(settings.enabled ? fmtDate(data.next_run) : 'disabled')}</div>`);
  if (state.last_file) {
    lines.push(`<div><strong>Latest archive:</strong> ${esc(state.last_file)} (${esc(fmtBytes(state.last_size))})</div>`);
  }
  if (state.last_download_at) {
    lines.push(`<div><strong>Last full backup downloaded:</strong> ${esc(fmtDate(state.last_download_at))}</div>`);
  }
  if (data.free_bytes != null) {
    lines.push(`<div><strong>Free space:</strong> ${esc(data.free_display || fmtBytes(data.free_bytes))}</div>`);
  }
  if (data.host_directory) {
    lines.push(`<div><strong>Host folder:</strong> ${esc(data.host_directory)}</div>`);
  }
  if (state.last_error) {
    lines.push(`<div class="admin-error"><strong>Last error:</strong> ${esc(state.last_error)}</div>`);
  }
  node.innerHTML = lines.join('');
}

function renderSettings(data) {
  const settings = data.settings || {};
  const enabled = el('backup-enabled');
  const directory = el('backup-directory');
  const interval = el('backup-interval');
  const retention = el('backup-retention');
  const attachments = el('backup-include-attachments');
  const research = el('backup-include-research');
  const notify = el('backup-notify');
  if (enabled) enabled.checked = !!settings.enabled;
  if (directory) directory.value = settings.directory || '';
  if (interval) interval.value = String(settings.interval_hours ?? 8);
  if (retention) retention.value = String(settings.retention ?? 21);
  if (attachments) attachments.checked = settings.include_attachments !== false;
  if (research) research.checked = settings.include_research !== false;
  if (notify) notify.checked = settings.notify_on_failure !== false;

  const restore = el('backup-restore-cmd');
  if (restore) restore.textContent = data.restore_command || '';
  const host = el('backup-host-dir');
  if (host) host.textContent = data.host_directory ? `Host path: ${data.host_directory}` : '';
  renderRoots(data.roots);
  if (data.directory_error) setMsg(data.directory_error, 'error');
}

async function loadList() {
  const node = el('backup-list');
  if (!node) return;
  let payload;
  try {
    payload = await api('/api/admin/backup/list');
  } catch (e) {
    node.innerHTML = `<div class="admin-error">${esc(e.message)}</div>`;
    return;
  }
  const backups = Array.isArray(payload.backups) ? payload.backups : [];
  if (!backups.length) {
    node.innerHTML = '<div class="admin-empty">No backups yet. Run one now or enable the schedule.</div>';
    return;
  }
  node.innerHTML = backups.map(item => {
    const name = String(item.name || '');
    const reason = item.reason === 'scheduled' ? 'automatic' : 'manual';
    const version = item.app_version ? ` · v${esc(item.app_version)}` : '';
    const href = `/api/admin/backup/download/${encodeURIComponent(name)}`;
    return `<div class="admin-user-row" style="display:flex;align-items:center;justify-content:space-between;gap:10px;">
      <div class="admin-user-info" style="min-width:0;">
        <div style="min-width:0;">
          <div class="admin-user-name" style="overflow-wrap:anywhere;">${esc(name)}</div>
          <div style="font-size:10px;opacity:0.5;">${esc(fmtDate(item.created_at))} · ${esc(item.size_display || fmtBytes(item.size))} · ${esc(reason)}${version}</div>
        </div>
      </div>
      <div style="display:flex;gap:6px;align-items:center;flex-shrink:0;">
        <a class="admin-btn-sm" href="${href}" download style="text-decoration:none;">Download</a>
        <button class="admin-btn-delete" data-backup-delete="${esc(name)}" style="font-size:11px;">Delete</button>
      </div>
    </div>`;
  }).join('');

  node.querySelectorAll('[data-backup-delete]').forEach(button => {
    button.addEventListener('click', async () => {
      const name = button.dataset.backupDelete;
      let ok = false;
      try {
        ok = await uiModule.styledConfirm(
          `Delete "${name}"? This removes the archive and its checksum.`,
          { confirmText: 'Delete', danger: true },
        );
      } catch (e) {
        ok = window.confirm(`Delete "${name}"?`);
      }
      if (!ok) return;
      button.disabled = true;
      try {
        await api(`/api/admin/backup/${encodeURIComponent(name)}`, { method: 'DELETE' });
        setMsg('Backup deleted.', 'success');
        await loadAll();
      } catch (e) {
        setMsg(e.message, 'error');
        button.disabled = false;
      }
    });
  });
}

async function loadAll() {
  let data;
  try {
    data = await api('/api/admin/backup/settings');
  } catch (e) {
    setMsg(e.message, 'error');
    return;
  }
  renderSettings(data);
  renderStatus(data);
  await loadList();
}

async function saveSettings() {
  const button = el('backup-save-btn');
  if (button) { button.disabled = true; button.textContent = 'Saving…'; }
  setMsg('');
  try {
    const payload = {
      enabled: !!(el('backup-enabled') && el('backup-enabled').checked),
      directory: (el('backup-directory')?.value || '').trim(),
      interval_hours: Number(el('backup-interval')?.value || 8),
      retention: Number(el('backup-retention')?.value || 21),
      include_attachments: !!(el('backup-include-attachments') && el('backup-include-attachments').checked),
      include_research: !!(el('backup-include-research') && el('backup-include-research').checked),
      notify_on_failure: !!(el('backup-notify') && el('backup-notify').checked),
    };
    const data = await api('/api/admin/backup/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    renderSettings(data);
    renderStatus(data);
    setMsg('Backup settings saved.', 'success');
  } catch (e) {
    setMsg(e.message, 'error');
  } finally {
    if (button) { button.disabled = false; button.textContent = 'Save settings'; }
  }
}

async function runBackup() {
  const button = el('backup-run-btn');
  if (button) { button.disabled = true; button.textContent = 'Backing up…'; }
  setMsg('Creating a full backup. This can take a few seconds…');
  try {
    const result = await api('/api/admin/backup/run', { method: 'POST' });
    const backup = result.backup || {};
    setMsg(`Created ${backup.name || 'backup'} (${backup.size_display || fmtBytes(backup.size)}).`, 'success');
    await loadAll();
  } catch (e) {
    setMsg(e.message, 'error');
  } finally {
    if (button) { button.disabled = false; button.textContent = 'Run backup now'; }
  }
}

async function copyRestoreCommand() {
  const command = el('backup-restore-cmd')?.textContent || '';
  if (!command) return;
  try {
    await navigator.clipboard.writeText(command);
    setMsg('Restore command copied.', 'success');
    return;
  } catch (e) {
    // Clipboard API is unavailable on some plain-HTTP deployments.
  }
  try {
    const textarea = document.createElement('textarea');
    textarea.value = command;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    const copied = document.execCommand('copy');
    document.body.removeChild(textarea);
    setMsg(copied ? 'Restore command copied.' : 'Select the command text and copy it.', copied ? 'success' : 'error');
  } catch (e) {
    setMsg('Select the command text and copy it.', 'error');
  }
}

export function initBackupAdmin() {
  if (bound) return;
  bound = true;
  el('backup-save-btn')?.addEventListener('click', saveSettings);
  el('backup-run-btn')?.addEventListener('click', runBackup);
  el('backup-refresh-btn')?.addEventListener('click', loadAll);
  el('backup-copy-restore-btn')?.addEventListener('click', copyRestoreCommand);
}

export async function refreshBackupAdmin() {
  if (!bound) initBackupAdmin();
  await loadAll();
}
