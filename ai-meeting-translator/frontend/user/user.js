/* user.js — Shared utils cho User Portal */
const API = 'http://127.0.0.1:8000';
const WS_BASE = 'ws://127.0.0.1:8000';

/* ── Session ── */
const U = {
  save: d => { try { sessionStorage.setItem('lm_user', JSON.stringify(d)); } catch (_) { } },
  load: () => { try { return JSON.parse(sessionStorage.getItem('lm_user') || 'null'); } catch (_) { return null; } },
  clear: () => sessionStorage.removeItem('lm_user'),
  require: () => { const u = U.load(); if (!u) { location.href = 'login.html'; } return u; },
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
          <div class="avatar av-sm">${init}</div>
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
function goRoom(code, userId, speakLang, listenLang, userName) {
  const p = new URLSearchParams({ room: code, userId, speakLang, listenLang, userName });
  location.href = `room.html?${p}`;
}
