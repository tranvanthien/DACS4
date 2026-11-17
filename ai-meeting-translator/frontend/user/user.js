/* user.js — Shared utils cho User Portal */
function _resolveBackend() {
  const isHttp = typeof window !== 'undefined' && window.location && window.location.protocol.startsWith('http');
  if (!isHttp) {
    return { api: 'http://127.0.0.1:8000', ws: 'ws://127.0.0.1:8000' };
  }
  const loc = window.location;
  const isHttps = loc.protocol === 'https:';
  const proto = isHttps ? 'https:' : 'http:';
  const wsProto = isHttps ? 'wss:' : 'ws:';
  if (loc.port === '8000' || loc.port === '80' || loc.port === '443' || loc.port === '') {
    return { api: proto + '//' + loc.host, ws: wsProto + '//' + loc.host };
  }
  const host = (loc.hostname || '127.0.0.1') + ':8000';
  return { api: proto + '//' + host, ws: wsProto + '//' + host };
}
const _cfg = _resolveBackend();
const API = _cfg.api;
const WS_BASE = _cfg.ws;

/* ── Session ── */
const U = {
  save: d => { try { sessionStorage.setItem('lm_user', JSON.stringify(d)); } catch (_) { } },
  load: () => { try { return JSON.parse(sessionStorage.getItem('lm_user') || 'null'); } catch (_) { return null; } },
  clear: () => sessionStorage.removeItem('lm_user'),
  require: () => { const u = U.load(); if (!u || !u.token) { location.href = 'login.html'; return null; } return u; },
  getAuthHeaders: (extra = {}) => {
    const u = U.load();
    const h = { 'Content-Type': 'application/json', ...extra };
    if (u && u.token) h['Authorization'] = 'Bearer ' + u.token;
    return h;
  },
  getToken: () => {
    const u = U.load();
    return (u && u.token) ? u.token : '';
  },
};

/* ── Toast ── */
const T = (() => {
  const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
  function show(type, title, msg, ms = 4000) {
    let c = document.getElementById('toast-container');
    if (!c) { c = document.createElement('div'); c.id = 'toast-container'; document.body.appendChild(c); }
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `<span>${icons[type] || 'ℹ️'}</span>
      <div style="flex:1"><div class="toast-title">${x(title)}</div>${msg ? `<div class="toast-msg">${x(msg)}</div>` : ''}</div>
      <button class="toast-close" onclick="this.closest('.toast').remove()">✕</button>`;
    c.appendChild(el);
    if (ms > 0) setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, ms);
  }
  return {
    ok: (t, m, d) => show('success', t, m, d), err: (t, m, d) => show('error', t, m, d),
    warn: (t, m, d) => show('warning', t, m, d), info: (t, m, d) => show('info', t, m, d)
  };
})();

/* ── Modal ── */
function openModal(id) { const e = document.getElementById(id); if (e) e.classList.add('show'); }
function closeModal(id) { const e = document.getElementById(id); if (e) e.classList.remove('show'); }

/* ── Escape ── */
function x(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

/* ── Copy ── */
function copyText(text, btn) { navigator.clipboard.writeText(text).then(() => { if (btn) { const o = btn.textContent; btn.textContent = '✓'; setTimeout(() => btn.textContent = o, 1500); } }).catch(() => { }); }

/* ── Time ── */
function fmtDT(d) { return new Date(d).toLocaleString('vi-VN', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }); }

/* ── Nav: render user menu ── */
function renderNav(activePage = '') {
  const user = U.load();
  const nav = document.getElementById('topnav');
  if (!nav || !user) return;
  const init = (user.full_name || 'U').charAt(0).toUpperCase();
  const pages = [
    { href: 'home.html', label: 'Trang chủ' },
    { href: 'history.html', label: 'Lịch sử họp' },
    { href: 'profile.html', label: 'Cài đặt' },
  ];
  nav.innerHTML = `
    <a class="topnav-brand" href="home.html">
      <div class="nav-icon">🌐</div> LinguaMeet
    </a>
    <div class="topnav-links">
      ${pages.map(p => `<a href="${p.href}" class="${activePage === p.href ? 'active' : ''}">${p.label}</a>`).join('')}
    </div>
    <div class="topnav-right" style="display:flex;align-items:center;gap:10px;">
      <button class="btn-nav-settings" title="Cài đặt nhanh" onclick="if(window.openSettingsModal){openSettingsModal();}else{location.href='profile.html';}">⚙️</button>
      <div class="user-menu-wrap">
        <div class="user-menu-btn" onclick="toggleDropdown()">
          <div class="avatar av-sm">${user.avatar_url ? `<img src="${API}${user.avatar_url}" style="width:100%;height:100%;object-fit:cover;border-radius:50%;" />` : init}</div>
          <span class="uname">${x(user.full_name || '')}</span>
          <span class="chevron">▾</span>
        </div>
        <div class="user-dropdown" id="userDropdown">
          <a href="profile.html">👤 Hồ sơ &amp; Cài đặt chi tiết</a>
          <a href="javascript:void(0)" onclick="if(window.openSettingsModal){openSettingsModal();}else{location.href='profile.html';}">⚙️ Cài đặt nhanh</a>
          <div class="dd-divider"></div>
          <button class="danger" onclick="doLogout()">🚪 Đăng xuất</button>
        </div>
      </div>
    </div>
  `;
  document.addEventListener('click', e => {
    if (!e.target.closest('.user-menu-wrap')) document.getElementById('userDropdown')?.classList.remove('open');
  });
}

function toggleDropdown() {
  document.getElementById('userDropdown')?.classList.toggle('open');
}

function doLogout() {
  U.clear();
  location.href = 'login.html';
}

/* ── Languages loader ── */
async function loadLangs() {
  try { const r = await fetch(API + '/api/languages'); if (r.ok) return await r.json(); } catch (_) { }
  return [];
}

function populateLangSelect(el, langs, val) {
  el.innerHTML = langs.map(l => `<option value="${l.code}">${l.flag || ''} ${l.name}</option>`).join('');
  if (val) el.value = val;
}

/* ── Enter room helper ── */
function goRoom(code, userId, speakLang, listenLang, userName, mic = true, cam = false) {
  const isMic = (mic === true || mic === '1' || mic === 'true');
  const isCam = (cam === true || cam === '1' || cam === 'true');
  const p = new URLSearchParams({
    room: code,
    userId,
    speakLang,
    listenLang,
    userName,
    mic: isMic ? '1' : '0',
    cam: isCam ? '1' : '0',
    token: U.getToken()
  });
  location.href = `room.html?${p}`;
}
