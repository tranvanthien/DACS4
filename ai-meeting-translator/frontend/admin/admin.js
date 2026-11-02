/* admin.js — Shared utils cho Admin Portal LinguaMeet
 * Xác thực thật qua Header:
 *   X-Admin-Email
 *   X-Admin-Key (lấy từ settings.ADMIN_SECRET_KEY trả về khi login)
 */

const API = (window.location.port === '8000') ? '' : 'http://127.0.0.1:8000';

/* ── Admin session (localStorage) ── */
const A = {
  save: d => { try { localStorage.setItem('lm_admin', JSON.stringify(d)); } catch (_) { } },
  load: () => { try { return JSON.parse(localStorage.getItem('lm_admin') || 'null'); } catch (_) { return null; } },
  clear: () => localStorage.removeItem('lm_admin'),
  require: () => {
    const a = A.load();
    if (!a || !a.email || !a.admin_key) {
      location.href = 'login.html';
      return null;
    }
    return a;
  },
};

function getAuthHeaders() {
  const a = A.load();
  const headers = { 'Content-Type': 'application/json' };
  if (a && a.email) headers['X-Admin-Email'] = a.email;
  if (a && a.admin_key) headers['X-Admin-Key'] = a.admin_key;
  return headers;
}

/* ── Toast ── */
const T = (() => {
  const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
  function show(type, title, msg, ms = 4000) {
    let c = document.getElementById('toast-container');
    if (!c) { c = document.createElement('div'); c.id = 'toast-container'; document.body.appendChild(c); }
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `<span>${icons[type] || 'ℹ️'}</span>
      <div style="flex:1"><div class="toast-title">${xe(title)}</div>${msg ? `<div class="toast-msg">${xe(msg)}</div>` : ''}</div>
      <button class="toast-close" onclick="this.closest('.toast').remove()">✕</button>`;
    c.appendChild(el);
    if (ms > 0) setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, ms);
  }
  return {
    ok: (t, m, d) => show('success', t, m, d),
    err: (t, m, d) => show('error', t, m, d),
    warn: (t, m, d) => show('warning', t, m, d),
    info: (t, m, d) => show('info', t, m, d),
  };
})();

/* ── Modal ── */
function openModal(id) { document.getElementById(id)?.classList.add('show'); }
function closeModal(id) { document.getElementById(id)?.classList.remove('show'); }

/* ── Escape HTML ── */
function xe(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/* ── Format ── */
function fmtDT(d) {
  if (!d) return '—';
  return new Date(d).toLocaleString('vi-VN', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}
function fmtDate(d) {
  if (!d) return '—';
  return new Date(d).toLocaleDateString('vi-VN', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

/* ── Sidebar render ── */
const NAV_ITEMS = [
  { href: 'dashboard.html', icon: '📊', label: 'Dashboard' },
  { href: 'users.html', icon: '👥', label: 'Người dùng' },
  { href: 'rooms.html', icon: '📹', label: 'Cuộc họp' },
  { href: 'languages.html', icon: '🌐', label: 'Ngôn ngữ' },
  { href: 'monitor.html', icon: '🔍', label: 'Giám sát AI' },
  { href: 'settings.html', icon: '⚙️', label: 'Cài đặt' },
];

function renderSidebar(activePage = '') {
  const admin = A.load();
  const el = document.getElementById('adminSidebar');
  if (!el || !admin) return;
  const init = (admin.full_name || 'A').charAt(0).toUpperCase();
  el.innerHTML = `
    <div class="sidebar-brand">
      <div class="s-icon">🛡️</div>
      <div>
        <div class="s-name">LinguaMeet</div>
        <div class="s-sub">Admin Portal</div>
      </div>
    </div>
    <nav class="sidebar-nav">
      <div class="nav-section">Hệ thống</div>
      ${NAV_ITEMS.map(n => `
        <a href="${n.href}" class="nav-item ${activePage === n.href ? 'active' : ''}">
          <span class="ni">${n.icon}</span> ${n.label}
        </a>`).join('')}
    </nav>
    <div class="sidebar-user">
      <div class="su-av">${init}</div>
      <div style="flex:1;min-width:0;">
        <div class="su-name" title="${xe(admin.full_name)}">${xe(admin.full_name || 'Admin')}</div>
        <div style="font-size:.68rem;color:rgba(255,255,255,.5);overflow:hidden;text-overflow:ellipsis;" title="${xe(admin.email)}">${xe(admin.email || '')}</div>
      </div>
      <button class="su-logout" title="Đăng xuất" onclick="adminLogout()">🚪</button>
    </div>
  `;
}

function adminLogout() {
  A.clear();
  location.href = 'login.html';
}

/* ── Topbar render ── */
function renderTopbar(title = '') {
  const el = document.getElementById('adminTopbar');
  if (!el) return;
  const admin = A.load() || {};
  el.innerHTML = `
    <button class="btn btn-ghost btn-sm" id="sidebarToggleBtn" onclick="document.getElementById('adminSidebar').classList.toggle('open')" style="display:none;">☰</button>
    <span class="atb-title">${xe(title)}</span>
    <div class="atb-right">
      <span class="badge badge-ok" style="font-size:.75rem;padding:4px 10px;display:flex;align-items:center;gap:6px;">
        <span class="dot dot-ok dot-pulse"></span>
        Admin: ${xe(admin.email || 'Xác thực')}
      </span>
      <a href="../meeting.html" target="_blank" class="btn btn-ghost btn-xs" style="margin-left:8px;" title="Vào phòng họp">
        🌐 Đi đến phòng họp →
      </a>
    </div>
  `;
  // Show toggle on mobile
  if (window.innerWidth <= 768) document.getElementById('sidebarToggleBtn').style.display = 'inline-flex';
  window.addEventListener('resize', () => {
    const btn = document.getElementById('sidebarToggleBtn');
    if (btn) btn.style.display = window.innerWidth <= 768 ? 'inline-flex' : 'none';
  });
}

/* ── Pagination helper ── */
function renderPagination(containerId, total, page, pageSize, onPageFnName) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const el = document.getElementById(containerId);
  if (!el) return;
  const start = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const end = Math.min(page * pageSize, total);
  el.innerHTML = `
    <span class="pagination-info">Hiển thị ${start}–${end} / ${total}</span>
    <div class="pagination-btns">
      <button class="pg-btn" onclick="${onPageFnName}(${page - 1})" ${page <= 1 ? 'disabled' : ''}>‹</button>
      ${Array.from({ length: Math.min(totalPages, 5) }, (_, i) => {
    const p = Math.max(1, Math.min(page - 2, totalPages - 4)) + i;
    return p <= totalPages ? `<button class="pg-btn ${p === page ? 'active' : ''}" onclick="${onPageFnName}(${p})">${p}</button>` : '';
  }).join('')}
      <button class="pg-btn" onclick="${onPageFnName}(${page + 1})" ${page >= totalPages ? 'disabled' : ''}>›</button>
    </div>
  `;
}

/* ── API helpers với Headers xác thực Admin ── */
async function handleApiResponse(r) {
  if (r.status === 401 || r.status === 403) {
    const d = await r.json().catch(() => ({}));
    if (location.pathname.indexOf('login.html') === -1) {
      T.err('Hết phiên làm việc', d.detail || 'Quyền Admin không hợp lệ');
      setTimeout(() => { A.clear(); location.href = 'login.html'; }, 1500);
    }
    throw new Error(d.detail || `Lỗi quyền truy cập (${r.status})`);
  }
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(d.detail || `${r.status} ${r.statusText}`);
  }
  return r.json();
}

async function apiGet(path) {
  const r = await fetch(API + path, {
    method: 'GET',
    headers: getAuthHeaders(),
  });
  return handleApiResponse(r);
}

async function apiPost(path, body) {
  const r = await fetch(API + path, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: JSON.stringify(body),
  });
  return handleApiResponse(r);
}

async function apiPut(path, body) {
  const r = await fetch(API + path, {
    method: 'PUT',
    headers: getAuthHeaders(),
    body: JSON.stringify(body),
  });
  return handleApiResponse(r);
}

async function apiDelete(path) {
  const r = await fetch(API + path, {
    method: 'DELETE',
    headers: getAuthHeaders(),
  });
  return handleApiResponse(r);
}

async function apiHealthCheck(path = '/api/health', timeoutMs = 5000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(API + path, { signal: ctrl.signal });
    clearTimeout(timer);
    return { ok: r.ok, status: r.status };
  } catch (e) {
    clearTimeout(timer);
    throw e;
  }
}

/* ── Activity log ── */
const ActivityLog = (() => {
  const _log = [];
  return {
    add(msg, type = 'info') {
      _log.unshift({ msg, type, time: new Date() });
      if (_log.length > 50) _log.pop();
    },
    get(n = 10) { return _log.slice(0, n); },
    clear() { _log.length = 0; },
  };
})();
