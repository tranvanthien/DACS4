/**
 * app.js — Shared utilities cho AI Meeting Translator
 * Toast notifications, modal helpers, language utils
 */

const API = 'http://127.0.0.1:8000';

// ── Toast ──────────────────────────────────────────────────────────────────
const Toast = (() => {
    let _container = null;

    function _getContainer() {
        if (!_container) {
            _container = document.getElementById('toast-container');
            if (!_container) {
                _container = document.createElement('div');
                _container.id = 'toast-container';
                document.body.appendChild(_container);
            }
        }
        return _container;
    }

    const ICONS = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };

    function show(type, title, msg, duration = 4000) {
        const c = _getContainer();
        const el = document.createElement('div');
        el.className = `toast ${type}`;
        el.innerHTML = `
      <span class="toast-icon">${ICONS[type] || 'ℹ️'}</span>
      <div class="toast-body">
        <div class="toast-title">${esc(title)}</div>
        ${msg ? `<div class="toast-msg">${esc(msg)}</div>` : ''}
      </div>
      <button class="toast-close" onclick="this.closest('.toast').remove()">✕</button>
    `;
        c.appendChild(el);
        if (duration > 0) {
            setTimeout(() => {
                el.classList.add('removing');
                setTimeout(() => el.remove(), 350);
            }, duration);
        }
    }

    return {
        success: (t, m, d) => show('success', t, m, d),
        error: (t, m, d) => show('error', t, m, d),
        warning: (t, m, d) => show('warning', t, m, d),
        info: (t, m, d) => show('info', t, m, d),
    };
})();

// ── Modal helpers ──────────────────────────────────────────────────────────
function openModal(id) {
    const el = document.getElementById(id);
    if (el) {
        el.classList.add('active');
        el.addEventListener('click', function handler(e) {
            if (e.target === el) { closeModal(id); el.removeEventListener('click', handler); }
        });
    }
}
function closeModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
}

// ── Escape HTML ────────────────────────────────────────────────────────────
function esc(s) {
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Format time ───────────────────────────────────────────────────────────
function fmtTime(date) {
    return new Date(date).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' });
}
function fmtDate(date) {
    return new Date(date).toLocaleDateString('vi-VN', { day: '2-digit', month: '2-digit', year: 'numeric' });
}
function fmtDatetime(date) {
    const d = new Date(date);
    return `${d.toLocaleDateString('vi-VN')} ${d.toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' })}`;
}

// ── Session storage helpers ────────────────────────────────────────────────
const Session = {
    save(data) { try { localStorage.setItem('amt_user', JSON.stringify(data)); } catch (_) { } },
    load() { try { return JSON.parse(localStorage.getItem('amt_user') || 'null'); } catch (_) { return null; } },
    clear() { localStorage.removeItem('amt_user'); },
};

// ── Copy to clipboard ──────────────────────────────────────────────────────
async function copyToClipboard(text, feedbackEl) {
    try {
        await navigator.clipboard.writeText(text);
        if (feedbackEl) {
            const orig = feedbackEl.textContent;
            feedbackEl.textContent = '✓ Đã sao chép';
            setTimeout(() => { feedbackEl.textContent = orig; }, 1500);
        }
        return true;
    } catch (_) { return false; }
}
