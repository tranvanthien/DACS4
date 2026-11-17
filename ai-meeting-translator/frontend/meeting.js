/**
 * meeting.js — Logic phòng họp
 * Tích hợp WebSocket, WebRTC, AutoSTT, TTS, Presenter Mode
 */

// API và WS_BASE đã được khai báo trong app.js
const WS_BASE = (typeof window !== 'undefined' && window.location && window.location.protocol === 'https:') ? ('wss://' + window.location.host) : (typeof window !== 'undefined' && window.location && window.location.protocol === 'http:') ? ((window.location.port === '8000' || window.location.port === '' || window.location.port === '80') ? ('ws://' + window.location.host) : ('ws://' + (window.location.hostname || '127.0.0.1') + ':8000')) : 'ws://127.0.0.1:8000';

function getMeetingAuthHeaders(extra = {}) {
    const h = { 'Content-Type': 'application/json', ...extra };
    const token = (typeof U !== 'undefined' && U.getToken) ? U.getToken() : ((new URLSearchParams(location.search)).get('token') || '');
    if (token) h['Authorization'] = 'Bearer ' + token;
    return h;
}

// ── State ─────────────────────────────────────────────────────────────────
let ws = null;
let currentUserId = null;
let currentRoomCode = null;
let currentUserName = 'Bạn';
let currentSpeakLang = 'vi';
let currentListenLang = 'en';
let roomPeers = [];
let languages = [];
let participants = new Map(); // userId → { name, speakLang }

// TTS state
let ttsEnabled = true;
let ttsAudioStore = new Map();
let pendingMsgIds = [];
let ttsQueue = [];
let ttsPlaying = false;
let audioUnlocked = false;

// Presenter mode state
let _presenterPeerId = null;   // 'local' hoặc peerId
let _presenterType = null;     // 'screen' | 'camera'
let _localCamStream = null;

// Host state
let currentHostId = null;
let currentMeetingMode = 'normal';

// ── Raise hand state ──────────────────────────────────────────────────────
let myHandRaised = false;
let raisedHandsMap = new Map(); // userId → userName (ordered)

// ── Chat state ────────────────────────────────────────────────────────────
let chatOpen = false;
let chatUnread = 0;

// ── URL params ────────────────────────────────────────────────────────────
function getUrlParams() {
    const p = new URLSearchParams(window.location.search);
    return {
        room: p.get('room') || '',
        userId: parseInt(p.get('userId') || '0'),
        speakLang: p.get('speakLang') || 'vi',
        listenLang: p.get('listenLang') || 'en',
        userName: p.get('userName') || 'User',
    };
}

// ── Init ──────────────────────────────────────────────────────────────────
async function init() {
    const params = getUrlParams();
    if (!params.room || !params.userId) {
        window.location.href = 'index.html';
        return;
    }

    currentRoomCode = params.room;
    currentUserId = params.userId;
    currentSpeakLang = params.speakLang;
    currentListenLang = params.listenLang;
    currentUserName = params.userName;

    // Load room info (host_id)
    try {
        const res = await fetch(`${API}/api/rooms/${currentRoomCode}`);
        if (res.ok) {
            const roomInfo = await res.json();
            currentHostId = roomInfo.host_id;
            applyMeetingMode(roomInfo.meeting_mode || 'normal');
        }
    } catch (_) { }

    // Load languages
    try {
        const res = await fetch(API + '/api/languages');
        if (res.ok) languages = await res.json();
    } catch (_) { }

    // Populate language selectors
    const opts = languages.map(l => `<option value="${l.code}">${l.flag || ''} ${l.name}</option>`).join('');
    ['meetSpeakLang', 'meetListenLang'].forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.innerHTML = opts; el.value = id.includes('Speak') ? currentSpeakLang : currentListenLang; }
    });

    document.getElementById('roomCodeDisplay').textContent = currentRoomCode;
    document.getElementById('roomNameDisplay').textContent = `Phòng ${currentRoomCode}`;

    participants.set(currentUserId, { name: currentUserName, speakLang: currentSpeakLang });
    renderParticipants();
    renderLocalVideoTile();

    AutoSTT.onStatusChange = onAutoSTTStatus;
    WebRTCService.onStateChange = onWebRTCEvent;
    WebRTCService.init();

    document.addEventListener('click', function _unlock() {
        audioUnlocked = true;
        if (!ttsPlaying && ttsQueue.length > 0) ttsPlayNext();
    }, { once: false });

    connectWS();

    // Load lịch sử chat
    setTimeout(loadChatHistory, 800);

    // Đóng reaction picker khi click ngoài
    document.addEventListener('click', (e) => {
        const wrap = document.getElementById('reactionWrap');
        if (wrap && !wrap.contains(e.target)) closeReactionPicker();
    });
}

// ── WebSocket ─────────────────────────────────────────────────────────────
function connectWS() {
    setConnStatus('connecting');
    const token = (typeof U !== 'undefined' && U.getToken) ? U.getToken() : ((new URLSearchParams(location.search)).get('token') || '');
    const wsUrl = token ? `${WS_BASE}/ws/rooms/${currentRoomCode}/${currentUserId}?token=${encodeURIComponent(token)}` : `${WS_BASE}/ws/rooms/${currentRoomCode}/${currentUserId}`;
    ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
        setConnStatus('connected');
        setSTTStatus('idle', 'Sẵn sàng — tham gia cuộc gọi để bắt đầu');
    };

    ws.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
            const buf = event.data;
            if (buf.byteLength >= 4) {
                const hdr = new Uint8Array(buf, 0, 4);
                if (hdr[0] === 84 && hdr[1] === 84 && hdr[2] === 83 && hdr[3] === 0) {
                    onTTSAudioFrame(buf.slice(4));
                    return;
                }
            }
            return;
        }
        try { handleMessage(JSON.parse(event.data)); } catch (_) { }
    };

    ws.onclose = () => {
        setConnStatus('disconnected');
        setSTTStatus('idle', 'Mất kết nối');
        setTimeout(() => {
            if (!ws || ws.readyState === WebSocket.CLOSED) connectWS();
        }, 3000);
    };

    ws.onerror = () => setConnStatus('disconnected');
}

// ── Message handler ───────────────────────────────────────────────────────
function handleMessage(data) {
    switch (data.type) {
        case 'room_peers':
            roomPeers = data.peers || [];
            break;

        case 'user_joined':
            if (data.user_id !== currentUserId) {
                if (!participants.has(data.user_id)) {
                    participants.set(data.user_id, { name: data.full_name || `User ${data.user_id}`, speakLang: data.speaking_language || '?', avatarUrl: data.avatar_url || null });
                }
                renderParticipants();
                showVideoTileForPeer(data.user_id);
                Toast && Toast.info('Có người vào phòng', `${data.full_name || 'User ' + data.user_id} đã tham gia`);
            }
            break;

        case 'user_left':
            participants.delete(data.user_id);
            raisedHandsMap.delete(data.user_id);
            renderParticipants();
            renderRaisedHands();
            removeVideoTile(data.user_id);
            if (typeof WebRTCService !== 'undefined') WebRTCService.handlePeerLeft(data.user_id);
            Toast && Toast.warning('Có người rời phòng', `User ${data.user_id} đã rời`);
            break;

        case 'room_closed':
            Toast && Toast.warning('Phòng đã đóng', data.message || 'Cuộc họp đã kết thúc');
            if (ws) { ws.onclose = null; ws.close(); ws = null; }
            setTimeout(() => { window.location.href = 'index.html'; }, 2000);
            break;

        case 'transcript':
            onTranscript(data);
            break;

        case 'translation':
            onTranslation(data);
            break;

        case 'translation_error':
            onTranslationError(data);
            break;

        case 'tts_start':
            onTTSStart(data);
            break;

        case 'tts_error':
            onTTSError(data);
            break;

        case 'transcript_rejected':
            setSTTStatus('idle', 'Không nhận diện được — thử lại');
            break;

        case 'webrtc_ready':
            // Peer mới join báo sẵn sàng. Bên cũ KHÔNG chủ động offer —
            // người mới đã tự offer trong joinCall của họ.
            // Chỉ offer nếu ta vào phòng TRƯỚC họ nhưng joinCall đã bỏ lỡ họ
            // (ví dụ: họ join ngay lúc ta đang joinCall).
            if (typeof WebRTCService !== 'undefined' && WebRTCService.isInCall
                && data.user_id !== currentUserId
                && !WebRTCService.hasPeer(data.user_id)) {
                // Người mới hơn (id lớn hơn) sẽ offer, ta chờ offer của họ.
                // Nếu id của ta lớn hơn (ta join sau), ta offer họ để tránh bỏ sót.
                if (currentUserId > data.user_id) {
                    doOffer(data.user_id);
                }
            }
            break;

        case 'webrtc_offer':
            if (typeof WebRTCService !== 'undefined') {
                WebRTCService.setWebSocket(ws);
                WebRTCService.handleOffer(data.from_user_id, data.sdp);
            }
            break;

        case 'webrtc_answer':
            if (typeof WebRTCService !== 'undefined')
                WebRTCService.handleAnswer(data.from_user_id, data.sdp);
            break;

        case 'webrtc_ice_candidate':
            if (typeof WebRTCService !== 'undefined')
                WebRTCService.handleIceCandidate(data.from_user_id, data.candidate);
            break;

        case 'webrtc_track_state':
            if (typeof WebRTCService !== 'undefined')
                WebRTCService.handleTrackState?.(data.from_user_id, { camera: data.camera, screen: data.screen });
            break;

        case 'webrtc_track_meta':
            if (typeof WebRTCService !== 'undefined')
                WebRTCService.handleTrackMeta?.(data.from_user_id, { track_id: data.track_id, kind: data.kind });
            break;

        case 'webrtc_leave':
            if (typeof WebRTCService !== 'undefined') WebRTCService.handlePeerLeft(data.user_id);
            removeVideoTile(data.user_id);
            break;

        case 'webrtc_peer_unavailable':
            console.warn('[WS] Peer unavailable:', data.target_user_id, data.reason);
            break;

        case 'audio_start':
            updateParticipantSpeaking(data.user_id, true);
            break;

        case 'audio_stop':
            updateParticipantSpeaking(data.user_id, false);
            break;

        case 'error':
            console.error('[WS] Error:', data.message);
            break;

        case 'raise_hand':
            onRaiseHand(data);
            break;

        case 'raised_hands_state':
            raisedHandsMap.clear();
            (data.hands || []).forEach(h => raisedHandsMap.set(h.user_id, h.user_name));
            renderRaisedHands();
            updateVideoTileHands();
            break;

        case 'reaction':
            onReaction(data);
            break;

        case 'room_mode':
            applyMeetingMode(data.meeting_mode);
            break;

        case 'room_mode_changed':
            applyMeetingMode(data.meeting_mode);
            Toast && Toast.info('Chế độ cuộc họp', data.message || 'Chế độ cuộc họp đã được cập nhật');
            break;

        case 'chat_message':
            onChatMessage(data);
            break;
    }
}
// ── Video tiles ───────────────────────────────────────────────────────────
function renderLocalVideoTile() {
    const grid = document.getElementById('videoGrid');
    if (!grid) return;
    let tile = document.getElementById('tile-local');
    const isHost = currentUserId === currentHostId;
    const localUser = (typeof U !== 'undefined' && U.load) ? U.load() : null;
    const avatarUrl = localUser && localUser.avatar_url ? localUser.avatar_url : null;
    const avatarHtml = avatarUrl
        ? `<img src="${typeof API !== 'undefined' ? API : ''}${avatarUrl}" style="width:56px;height:56px;border-radius:50%;object-fit:cover;" />`
        : `<div class="avatar-big">${currentUserName.charAt(0).toUpperCase()}</div>`;
    if (!tile) {
        tile = document.createElement('div');
        tile.className = 'video-tile' + (isHost ? ' is-host' : '');
        tile.id = 'tile-local';
        tile.innerHTML = `
      <div class="video-placeholder">
        ${avatarHtml}
        <div class="peer-name">${esc(currentUserName)} (Bạn)</div>
      </div>
      <div class="video-tile-info">
        <span class="tile-name">${esc(currentUserName)}</span>
        <span class="tile-mic-off" id="localMicIcon">🎤</span>
        <span class="tile-hand hidden" id="tileHand-${currentUserId}">✋</span>
      </div>
      ${isHost ? '<div class="host-badge">👑 Chủ phòng</div>' : ''}
    `;
        if (isHost) grid.prepend(tile);
        else grid.appendChild(tile);
    }
    updateVideoEmpty();
}

function showVideoTileForPeer(peerId) {
    const grid = document.getElementById('videoGrid');
    if (!grid || document.getElementById(`tile-${peerId}`)) return;
    const peer = participants.get(peerId) || {};
    const name = peer.name || `User ${peerId}`;
    const isHost = peerId === currentHostId;
    const avatarHtml = peer.avatarUrl
        ? `<img src="${typeof API !== 'undefined' ? API : ''}${peer.avatarUrl}" style="width:56px;height:56px;border-radius:50%;object-fit:cover;" />`
        : `<div class="avatar-big">${name.charAt(0).toUpperCase()}</div>`;
    const tile = document.createElement('div');
    tile.className = 'video-tile' + (isHost ? ' is-host' : '');
    tile.id = `tile-${peerId}`;
    tile.innerHTML = `
    <div class="video-placeholder">
      ${avatarHtml}
      <div class="peer-name">${esc(name)}</div>
    </div>
    <div class="video-tile-info">
      <span class="tile-name">${esc(name)}</span>
      <span class="tile-mic-off" id="peerMicIcon-${peerId}">🎤</span>
      <span class="tile-hand hidden" id="tileHand-${peerId}">✋</span>
    </div>
    ${isHost ? '<div class="host-badge">👑 Chủ phòng</div>' : ''}
  `;
    const thumbStrip = grid.querySelector('.presenter-thumbnails');
    if (thumbStrip) {
        if (isHost) thumbStrip.prepend(tile);
        else thumbStrip.appendChild(tile);
    } else {
        if (isHost) grid.prepend(tile);
        else grid.appendChild(tile);
    }
    updateVideoEmpty();
}

function removeVideoTile(peerId) {
    const tile = document.getElementById(`tile-${peerId}`);
    if (tile) tile.remove();
    if (_presenterPeerId === peerId) exitPresenterMode();
    updateVideoEmpty();
}

function updateVideoEmpty() {
    const grid = document.getElementById('videoGrid');
    const empty = document.getElementById('videoEmpty');
    if (!grid || !empty) return;
    const count = grid.querySelectorAll('.video-tile').length;
    empty.style.display = count > 0 ? 'none' : 'flex';
}

function updateParticipantSpeaking(userId, speaking) {
    const tile = document.getElementById(`tile-${userId}`);
    if (tile) tile.classList.toggle('speaking', speaking);
    const memberItem = document.getElementById(`member-item-${userId}`);
    if (memberItem) memberItem.classList.toggle('speaking', speaking);
}

// ── WebRTC Events ─────────────────────────────────────────────────────────
function onWebRTCEvent(event) {
    switch (event.type) {
        case 'call_joined': {
            document.getElementById('btnJoinCall').classList.add('hidden');
            document.getElementById('btnLeaveCall').classList.remove('hidden');
            document.getElementById('btnToggleMic').classList.remove('hidden');
            document.getElementById('btnToggleCam')?.classList.remove('hidden');
            document.getElementById('btnToggleScreen').classList.remove('hidden');
            updateWebRTCStatus('Đang trong cuộc gọi');

            if (WebRTCService.localStream) {
                const track = WebRTCService.localStream.getAudioTracks()[0];
                if (track && track.readyState === 'live') {
                    if (currentMeetingMode === 'ai') {
                        // Chế độ AI: tắt WebRTC audio track để bên kia không nghe giọng gốc,
                        // chỉ nghe giọng AI qua TTS pipeline.
                        WebRTCService.localStream.getAudioTracks().forEach(t => { t.enabled = false; });
                        AutoSTT.start(WebRTCService.localStream, ws, currentSpeakLang);
                    }
                    document.getElementById('btnToggleMic').classList.add('active');
                }
            }
            break;
        }

        case 'call_left':
            document.getElementById('btnJoinCall').classList.remove('hidden');
            document.getElementById('btnLeaveCall').classList.add('hidden');
            document.getElementById('btnToggleMic').classList.add('hidden');
            document.getElementById('btnToggleMic').classList.remove('active');
            document.getElementById('btnToggleCam')?.classList.add('hidden');
            document.getElementById('btnToggleCam')?.classList.remove('active');
            document.getElementById('btnToggleScreen').classList.add('hidden');
            document.getElementById('btnToggleScreen').classList.remove('active');
            exitPresenterMode();
            AutoSTT.stop();
            updateWebRTCStatus('Chưa trong cuộc gọi');
            setSTTStatus('idle', 'Chưa trong cuộc gọi');
            break;

        case 'mic_toggled': {
            const micBtn = document.getElementById('btnToggleMic');
            if (micBtn) {
                micBtn.classList.toggle('off', !event.enabled);
                const localMicIcon = document.getElementById('localMicIcon');
                if (localMicIcon) localMicIcon.textContent = event.enabled ? '🎤' : '🔇';
            }
            if (!event.enabled) {
                AutoSTT.stop();
                setSTTStatus('idle', 'Mic đã tắt');
            } else if (WebRTCService.localStream && currentMeetingMode === 'ai') {
                setTimeout(() => {
                    if (!AutoSTT.isRunning() && currentMeetingMode === 'ai') {
                        AutoSTT.start(WebRTCService.localStream, ws, currentSpeakLang);
                    }
                }, 100);
            }
            break;
        }

        case 'mic_error':
            Toast && Toast.error('Lỗi micro', event.reason || event.error?.message);
            updateWebRTCStatus('Lỗi microphone');
            break;

        case 'peer_stream':
            showVideoTileForPeer(event.peerId);
            updateWebRTCStatus(`${WebRTCService.peerCount} người kết nối`);
            break;

        case 'peer_closed':
            removeVideoTile(event.peerId);
            updateWebRTCStatus(WebRTCService.peerCount > 0 ? `${WebRTCService.peerCount} người kết nối` : 'Đang trong cuộc gọi');
            break;

        case 'screen_toggled': {
            const btn = document.getElementById('btnToggleScreen');
            if (btn) btn.classList.toggle('active', event.enabled);
            if (event.enabled && event.stream) {
                enterPresenterMode('local', event.stream, 'screen');
                Toast && Toast.info('Chia sẻ màn hình', 'Đang chia sẻ màn hình của bạn');
            } else {
                if (_localCamStream) {
                    enterPresenterMode('local', _localCamStream, 'camera');
                } else {
                    exitPresenterMode();
                }
                Toast && Toast.info('Chia sẻ màn hình', 'Đã dừng chia sẻ');
            }
            break;
        }

        case 'cam_toggled':
            if (event.enabled && event.stream) {
                _localCamStream = event.stream;
                if (!WebRTCService.isScreenOn) {
                    enterPresenterMode('local', event.stream, 'camera');
                }
            } else {
                _localCamStream = null;
                if (_presenterPeerId === 'local' && _presenterType === 'camera') {
                    exitPresenterMode();
                }
            }
            break;

        case 'peer_video':
            if (event.stream) {
                const isScreenSharing = _presenterPeerId === 'local' && _presenterType === 'screen';
                if (!isScreenSharing) {
                    enterPresenterMode(event.peerId, event.stream, 'camera');
                }
            } else {
                if (_presenterPeerId === event.peerId) {
                    if (_localCamStream && !WebRTCService.isScreenOn) {
                        enterPresenterMode('local', _localCamStream, 'camera');
                    } else {
                        exitPresenterMode();
                    }
                }
            }
            break;

        case 'connection_state':
            if (event.state === 'failed') {
                Toast && Toast.error('Kết nối thất bại', `Peer ${event.peerId}`);
            }
            break;

        case 'autoplay_blocked':
            document.getElementById('autoplayBanner').classList.add('visible');
            break;
    }
}

// ── Presenter Mode ────────────────────────────────────────────────────────
function enterPresenterMode(presenterId, stream, type = 'screen') {
    const grid = document.getElementById('videoGrid');
    if (!grid) return;

    if (grid.classList.contains('presenter-mode') && _presenterPeerId === presenterId && _presenterType === type) {
        const mainVideo = grid.querySelector('.presenter-main video');
        if (mainVideo) mainVideo.srcObject = stream;
        return;
    }

    _presenterPeerId = presenterId;
    _presenterType = type;
    grid.classList.add('presenter-mode');
    grid.dataset.presenterType = type;

    const isLocal = presenterId === 'local';
    const presenterName = isLocal ? currentUserName : (participants.get(presenterId)?.name || `User ${presenterId}`);
    const labelText = type === 'screen'
        ? `🖥️ ${esc(presenterName)} đang chia sẻ màn hình`
        : `📷 ${esc(presenterName)} đang trình bày`;

    const tiles = [...grid.querySelectorAll('.video-tile')];

    let mainArea = grid.querySelector('.presenter-main');
    if (!mainArea) {
        mainArea = document.createElement('div');
        mainArea.className = 'presenter-main';
        grid.insertBefore(mainArea, grid.firstChild);
    }
    mainArea.innerHTML = `
        <div class="presenter-label">
            <span class="presenter-dot"></span>
            ${labelText}
        </div>
        <video autoplay playsinline></video>
    `;
    const mainVideo = mainArea.querySelector('video');
    mainVideo.muted = true;
    mainVideo.srcObject = stream;
    mainVideo.play().catch(e => console.warn('[PresenterVideo] play failed:', e));

    let thumbStrip = grid.querySelector('.presenter-thumbnails');
    if (!thumbStrip) {
        thumbStrip = document.createElement('div');
        thumbStrip.className = 'presenter-thumbnails';
        grid.appendChild(thumbStrip);
    }

    tiles.forEach(tile => {
        const presenterTileId = isLocal ? 'tile-local' : `tile-${presenterId}`;
        tile.classList.toggle('is-presenter', tile.id === presenterTileId);
        thumbStrip.appendChild(tile);
    });
}

function exitPresenterMode() {
    const grid = document.getElementById('videoGrid');
    if (!grid || !grid.classList.contains('presenter-mode')) return;

    _presenterPeerId = null;
    _presenterType = null;
    grid.classList.remove('presenter-mode');
    delete grid.dataset.presenterType;

    const thumbStrip = grid.querySelector('.presenter-thumbnails');
    if (thumbStrip) {
        [...thumbStrip.querySelectorAll('.video-tile')].forEach(tile => {
            tile.classList.remove('is-presenter');
            grid.appendChild(tile);
        });
        thumbStrip.remove();
    }

    const mainArea = grid.querySelector('.presenter-main');
    if (mainArea) {
        const v = mainArea.querySelector('video');
        if (v) v.srcObject = null;
        mainArea.remove();
    }
}

// ── Room Mode helper ──────────────────────────────────────────────────────
function applyMeetingMode(mode) {
    currentMeetingMode = (mode === 'ai' || mode === 'ai_translation') ? 'ai' : 'normal';
    const subBar = document.getElementById('subtitlesBar');
    const isNormal = currentMeetingMode === 'normal';

    if (isNormal) {
        AutoSTT.stop();
        if (subBar) subBar.classList.add('hidden');
        ttsQueue.length = 0;
        ttsPlaying = false;
        setSTTStatus('idle', 'Chế độ họp bình thường (AI tắt)');
        // Chế độ bình thường: bật lại WebRTC audio track để bên kia nghe giọng thật
        if (WebRTCService.localStream) {
            WebRTCService.localStream.getAudioTracks().forEach(t => { t.enabled = true; });
        }
    } else {
        if (subBar) subBar.classList.remove('hidden');
        // Chế độ AI: tắt WebRTC audio track để bên kia chỉ nghe TTS, không nghe giọng gốc
        if (WebRTCService.localStream) {
            WebRTCService.localStream.getAudioTracks().forEach(t => { t.enabled = false; });
        }
        if (WebRTCService.isInCall && WebRTCService.localStream) {
            const track = WebRTCService.localStream.getAudioTracks()[0];
            if (track && track.readyState === 'live' && !AutoSTT.isRunning()) {
                AutoSTT.start(WebRTCService.localStream, ws, currentSpeakLang);
            }
        }
    }
}

// ── AutoSTT status → UI ───────────────────────────────────────────────────
function onAutoSTTStatus(status) {
    const labels = {
        off: '',
        cho: 'Đang chờ giọng nói...',
        nghe: '🎤 Đang nghe...',
        'nhan-dien': '⚙️ Đang nhận diện...',
        dich: '🌍 Đang dịch...',
    };
    const states = {
        off: 'idle', cho: 'idle', nghe: 'listening',
        'nhan-dien': 'recognizing', dich: 'recognizing',
    };
    setSTTStatus(states[status] || 'idle', labels[status] || status);

    const sttBtn = document.getElementById('btnSTT');
    if (sttBtn) sttBtn.classList.toggle('active-stt', status === 'nghe' || status === 'nhan-dien');
}

// ── Transcript ────────────────────────────────────────────────────────────
function onTranscript(data) {
    const isMe = data.user_id === currentUserId;
    const lang = languages.find(l => l.code === (data.source_language || data.speaking_language));
    const time = new Date().toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' });

    const feed = document.getElementById('transcriptFeed');
    if (!feed) return;
    if (data.message_id && feed.querySelector(`[data-msg-id="${data.message_id}"]`)) return;

    const entry = document.createElement('div');
    entry.className = `msg-entry msg-pending${isMe ? ' msg-me' : ''}`;
    entry.dataset.msgId = data.message_id || `local-${Date.now()}-${data.user_id}`;
    entry.innerHTML = `
    <div class="msg-header">
      <span class="msg-user">${esc(isMe ? 'Bạn' : `User ${data.user_id}`)}</span>
      <span class="msg-lang">${lang ? `${lang.flag} ${lang.code}` : (data.source_language || '')}</span>
      <span class="msg-time">${time}</span>
    </div>
    <div class="msg-original">${esc(data.text)}</div>
    <div class="msg-translated" style="font-style:italic;opacity:.6;font-size:.8rem;">
      <span class="spinner spinner-sm"></span> Đang dịch...
    </div>
  `;
    feed.appendChild(entry);
    feed.scrollTop = feed.scrollHeight;

    showSubtitle({ userId: data.user_id, speakLang: lang, original: data.text, translated: null, status: 'Đang dịch...' });

    updateParticipantSpeaking(data.user_id, true);
    setTimeout(() => updateParticipantSpeaking(data.user_id, false), 2000);
}

// ── Translation ───────────────────────────────────────────────────────────
function onTranslation(data) {
    const feed = document.getElementById('transcriptFeed');
    if (feed && data.message_id) {
        const entry = feed.querySelector(`[data-msg-id="${data.message_id}"]`);
        if (entry) {
            entry.classList.remove('msg-pending');
            const translatedEl = entry.querySelector('.msg-translated');
            if (translatedEl) {
                const tgtLang = languages.find(l => l.code === data.target_language);
                translatedEl.style = '';
                translatedEl.innerHTML = `
          <span style="font-size:.68rem;color:var(--gray-400);">${tgtLang ? tgtLang.flag + ' ' : ''}${data.target_language}</span><br>
          ${esc(data.translated_text)}
        `;
            }
        }
    }

    const translationFeed = document.getElementById('translationFeed');
    if (!translationFeed) return;
    if (data.message_id && translationFeed.querySelector(`[data-msg-id="${data.message_id}"]`)) return;

    const isMe = data.user_id === currentUserId;
    const srcLang = languages.find(l => l.code === data.source_language);
    const tgtLang = languages.find(l => l.code === data.target_language);
    const time = new Date().toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' });

    const entry = document.createElement('div');
    entry.className = `msg-entry${isMe ? ' msg-me' : ''}`;
    entry.dataset.msgId = data.message_id || '';
    entry.innerHTML = `
    <div class="msg-header">
      <span class="msg-user">${esc(isMe ? 'Bạn' : `User ${data.user_id}`)}</span>
      <span class="msg-lang">${srcLang ? srcLang.flag : ''} → ${tgtLang ? tgtLang.flag : ''}</span>
      <span class="msg-time">${time}</span>
    </div>
    <div class="msg-original">${esc(data.original_text)}</div>
    <div class="msg-translated">${esc(data.translated_text)}</div>
    <div class="tts-row">
      <span class="tts-status-text ${data.tts_pending ? 'loading' : ''}">${data.tts_pending ? '🔊 Đang tạo giọng...' : ''}</span>
      <button class="btn-replay" onclick="replayTTS(this)">▶ Phát lại</button>
    </div>
  `;
    translationFeed.appendChild(entry);
    translationFeed.scrollTop = translationFeed.scrollHeight;

    showSubtitle({
        userId: data.user_id, speakLang: srcLang,
        original: data.original_text, translated: data.translated_text, status: '',
    });
}

// ── Translation error ─────────────────────────────────────────────────────
function onTranslationError(data) {
    const feed = document.getElementById('transcriptFeed');
    if (feed && data.message_id) {
        const entry = feed.querySelector(`[data-msg-id="${data.message_id}"]`);
        if (entry) {
            entry.classList.remove('msg-pending');
            const translatedEl = entry.querySelector('.msg-translated');
            if (translatedEl) {
                translatedEl.style = '';
                translatedEl.innerHTML = `<span style="color:var(--danger);font-size:.78rem;">⚠ Dịch thất bại</span>`;
            }
        }
    }
}

// ── TTS ───────────────────────────────────────────────────────────────────
function onTTSStart(data) {
    pendingMsgIds.push(data.message_id);
    setTTSBlockStatus(data.message_id, 'loading', '🔊 Đang tạo giọng...');
}

function onTTSAudioFrame(mp3Bytes) {
    if (!ttsEnabled) { pendingMsgIds.shift(); return; }
    const msgId = pendingMsgIds.shift();
    if (!msgId) { console.warn('[TTS] Audio received but no pending msgId — dropped'); return; }
    ttsAudioStore.set(msgId, mp3Bytes);
    setTTSBlockStatus(msgId, '', '');
    showReplayBtn(msgId);
    ttsQueue.push(msgId);
    if (!ttsPlaying) ttsPlayNext();
}

function onTTSError(data) {
    const idx = pendingMsgIds.indexOf(data.message_id);
    if (idx !== -1) pendingMsgIds.splice(idx, 1);
    setTTSBlockStatus(data.message_id, 'error', '⚠ Không tạo được giọng nói');
}

function ttsPlayNext() {
    if (ttsQueue.length === 0) { ttsPlaying = false; return; }
    ttsPlaying = true;
    const msgId = ttsQueue.shift();
    const bytes = ttsAudioStore.get(msgId);
    if (!bytes) { ttsPlayNext(); return; }
    playTTSBytes(bytes, () => ttsPlayNext(), () => ttsPlayNext());
}

function playTTSBytes(mp3Bytes, onEnded, onError) {
    AutoSTT.setTTSPlaying(true);
    const blob = new Blob([mp3Bytes], { type: 'audio/mpeg' });

    function _doPlay() {
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        audio.onended = () => { URL.revokeObjectURL(url); AutoSTT.setTTSPlaying(false); onEnded(); };
        audio.onerror = () => { URL.revokeObjectURL(url); AutoSTT.setTTSPlaying(false); onError(); };
        const p = audio.play();
        if (p !== undefined) {
            p.catch(err => {
                console.warn('[TTS] Autoplay blocked:', err.message);
                URL.revokeObjectURL(url);
                document.getElementById('autoplayBanner').classList.add('visible');
                document.addEventListener('click', function retry() {
                    document.removeEventListener('click', retry);
                    document.getElementById('autoplayBanner').classList.remove('visible');
                    audioUnlocked = true;
                    _doPlay();
                }, { once: true });
            });
        }
    }
    _doPlay();
}

function replayTTS(btn) {
    const entry = btn.closest('.msg-entry');
    if (!entry) return;
    const bytes = ttsAudioStore.get(entry.dataset.msgId);
    if (!bytes) return;
    playTTSBytes(bytes, () => { }, () => { });
}

function setTTSBlockStatus(msgId, state, text) {
    if (!msgId) return;
    const feed = document.getElementById('translationFeed');
    if (!feed) return;
    const entry = feed.querySelector(`[data-msg-id="${msgId}"]`);
    if (!entry) return;
    const span = entry.querySelector('.tts-status-text');
    if (span) { span.className = `tts-status-text ${state}`; span.textContent = text; }
}

function showReplayBtn(msgId) {
    if (!msgId) return;
    const feed = document.getElementById('translationFeed');
    if (!feed) return;
    const entry = feed.querySelector(`[data-msg-id="${msgId}"]`);
    if (!entry) return;
    const btn = entry.querySelector('.btn-replay');
    if (btn) btn.classList.add('visible');
}

function toggleTTS() {
    ttsEnabled = !ttsEnabled;
    const toggle = document.getElementById('ttsToggle');
    if (toggle) toggle.classList.toggle('on', ttsEnabled);
    if (!ttsEnabled) { ttsQueue.length = 0; ttsPlaying = false; }
    if (ws && ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: 'tts_toggle', enabled: ttsEnabled }));
    Toast && Toast.info('TTS', ttsEnabled ? 'Giọng đọc AI bật' : 'Giọng đọc AI tắt');
}

function unlockAudio() {
    audioUnlocked = true;
    document.getElementById('autoplayBanner').classList.remove('visible');
    if (!ttsPlaying && ttsQueue.length > 0) ttsPlayNext();
}

// ── Language save ─────────────────────────────────────────────────────────
async function saveLanguages() {
    const speakLang = document.getElementById('meetSpeakLang').value;
    const listenLang = document.getElementById('meetListenLang').value;
    try {
        const res = await fetch(`${API}/api/users/${currentUserId}/languages`, {
            method: 'PUT', headers: getMeetingAuthHeaders(),
            body: JSON.stringify({ speaking_language: speakLang, listening_language: listenLang }),
        });
        if (!res.ok) throw new Error();
        currentSpeakLang = speakLang;
        currentListenLang = listenLang;
        AutoSTT.updateLanguage(speakLang);
        Toast && Toast.success('Đã lưu', `Nói ${speakLang}, nghe ${listenLang}`);
    } catch (_) {
        Toast && Toast.error('Lỗi', 'Không thể lưu ngôn ngữ');
    }
}

// ── WebRTC actions ────────────────────────────────────────────────────────
function _log_meeting(...a) { console.log('[Meeting]', ...a); }

async function joinCall() {
    if (!currentUserId || !ws || ws.readyState !== WebSocket.OPEN) {
        Toast && Toast.error('Chưa kết nối', 'Chưa kết nối WebSocket');
        return;
    }
    WebRTCService.setWebSocket(ws);
    try {
        // Gửi offer đến tất cả peer đã có trong phòng trước khi ta join.
        // Người mới vào luôn là offerer, người cũ sẽ answer.
        const peersToOffer = roomPeers.filter(id => Number(id) !== Number(currentUserId));
        _log_meeting(`joinCall: peers=${JSON.stringify(roomPeers)}, offering to: ${JSON.stringify(peersToOffer)}`);
        await WebRTCService.joinCall(currentUserId, peersToOffer);
    } catch (e) {
        const msg = e.message?.includes('HTTPS') || e.message?.includes('localhost')
            ? 'Trang cần chạy qua HTTPS hoặc localhost mới dùng được microphone'
            : e.message;
        Toast && Toast.error('Không vào được cuộc gọi', msg);
    }
}

function leaveCall() {
    AutoSTT.stop();
    if (typeof WebRTCService !== 'undefined' && WebRTCService.isInCall) WebRTCService.leaveCall();
}

function toggleMic() {
    if (typeof WebRTCService !== 'undefined') WebRTCService.toggleMic();
}

async function toggleCam() {
    if (!WebRTCService.isInCall) {
        Toast && Toast.error('Chưa trong cuộc gọi', 'Hãy tham gia cuộc gọi trước');
        return;
    }
    try {
        const on = await WebRTCService.toggleCamera();
        const btn = document.getElementById('btnToggleCam');
        if (btn) btn.classList.toggle('active', on);
    } catch (e) {
        Toast && Toast.error('Lỗi camera', e.message);
    }
}

async function toggleScreen() {
    if (!WebRTCService.isInCall) {
        Toast && Toast.error('Chưa trong cuộc gọi', 'Hãy tham gia cuộc gọi trước');
        return;
    }
    try {
        await WebRTCService.toggleScreen();
    } catch (e) {
        Toast && Toast.error('Lỗi chia sẻ màn hình', e.message);
    }
}

async function doOffer(peerId) {
    WebRTCService.setWebSocket(ws);
    try { await WebRTCService.createOffer(peerId); } catch (e) { console.error('[WebRTC] doOffer', e); }
}

// ── Leave meeting ─────────────────────────────────────────────────────────
function leaveMeeting() {
    AutoSTT.stop();
    leaveCall();
    if (ws) ws.close();
    try {
        fetch(`${API}/api/rooms/${currentRoomCode}/leave`, { method: 'POST', headers: getMeetingAuthHeaders() }).catch(() => { });
    } catch (_) { }
    window.location.href = 'index.html';
}

// ── Copy room code ────────────────────────────────────────────────────────
function copyRoomCode() {
    const btn = document.getElementById('btnCopyCode');
    copyToClipboard(currentRoomCode, btn);
    Toast && Toast.success('Đã sao chép', currentRoomCode);
}

// ── Clear feeds ───────────────────────────────────────────────────────────
function clearTranscript() {
    const el = document.getElementById('transcriptFeed');
    if (el) el.innerHTML = '';
}
function clearTranslation() {
    const el = document.getElementById('translationFeed');
    if (el) el.innerHTML = '';
    ttsAudioStore.clear();
    ttsQueue.length = 0;
}

// ── Subtitle ──────────────────────────────────────────────────────────────
let _subtitleTimer = null;

function showSubtitle({ userId, speakLang, original, translated, status }) {
    const box = document.getElementById('subtitleOverlay');
    const speakerEl = document.getElementById('subSpeaker');
    const origEl = document.getElementById('subOriginal');
    const transEl = document.getElementById('subTranslated');
    const statusEl = document.getElementById('subStatus');
    if (!box) return;

    const isMe = userId === currentUserId;
    const langStr = speakLang ? `${speakLang.flag} ` : '';
    speakerEl.textContent = `${isMe ? 'Bạn' : `User ${userId}`} ${langStr}`;
    origEl.textContent = original || '';
    transEl.textContent = translated || '';
    statusEl.textContent = status || '';
    box.style.display = 'block';

    clearTimeout(_subtitleTimer);
    _subtitleTimer = setTimeout(() => { box.style.display = 'none'; }, translated ? 6000 : 8000);
}

// ── Participants ──────────────────────────────────────────────────────────
function renderParticipants() {
    const container = document.getElementById('participantsList');
    const countEl = document.getElementById('participantCount');
    if (!container) return;

    if (countEl) countEl.textContent = participants.size;

    if (participants.size === 0) {
        const empty = `<div class="members-empty"><div class="me-icon">👥</div>Chưa có ai</div>`;
        container.innerHTML = empty;
        const panel = document.getElementById('panelParticipantsList');
        if (panel) panel.innerHTML = empty;
        return;
    }

    const html = [...participants.entries()].map(([uid, p]) => {
        const isMe = uid === currentUserId;
        const isHost = uid === currentHostId;
        const initial = (p.name || 'U').charAt(0).toUpperCase();
        const langStr = p.speakLang && p.speakLang !== '?' ? `🗣 ${p.speakLang}` : '';
        return `<div class="member-item" id="member-item-${uid}">
            <div class="member-av">${initial}<span class="speak-ring"></span></div>
            <div class="member-info">
                <div class="member-name">${esc(p.name || `User ${uid}`)}</div>
                ${langStr ? `<div class="member-langs">${langStr}</div>` : ''}
            </div>
            ${isHost ? '<span class="member-badge-host">👑 Host</span>' : ''}
            ${isMe ? '<span class="member-badge-you">Bạn</span>' : ''}
            ${raisedHandsMap.has(uid) ? '<span class="member-hand-icon">✋</span>' : ''}
        </div>`;
    }).join('');

    container.innerHTML = html;
    const panel = document.getElementById('panelParticipantsList');
    if (panel) panel.innerHTML = html;
}

// ── UI helpers ────────────────────────────────────────────────────────────
function setConnStatus(state) {
    const el = document.getElementById('connStatusPill');
    if (!el) return;
    el.className = `top-conn-pill ${state}`;
    const labels = { connected: '🟢 Đã kết nối', connecting: '🟡 Đang kết nối...', disconnected: '🔴 Mất kết nối' };
    el.textContent = labels[state] || state;
}

function updateWebRTCStatus(text) {
    const el = document.getElementById('webrtcStatusText');
    if (el) el.textContent = text;
}

function setSTTStatus(state, text) {
    const bar = document.getElementById('sttStatusBar');
    if (!bar) return;
    bar.className = `stt-status-bar ${state}`;
    bar.innerHTML = `<span class="stt-dot"></span> ${esc(text)}`;
}

// ── Tabs ──────────────────────────────────────────────────────────────────
function switchTab(tabName) {
    document.querySelectorAll('.ai-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tabName));
    document.querySelectorAll('.ai-pane').forEach(p => p.classList.toggle('active', p.id === `pane-${tabName}`));
}

// ── Copy helper ───────────────────────────────────────────────────────────
function copyToClipboard(text, btn) {
    navigator.clipboard.writeText(text).then(() => {
        if (btn) { const orig = btn.textContent; btn.textContent = '✓'; setTimeout(() => { btn.textContent = orig; }, 1500); }
    }).catch(() => { });
}

function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Raise Hand ────────────────────────────────────────────────────────────

function toggleHand() {
    myHandRaised = !myHandRaised;
    const btn = document.getElementById('btnRaiseHand');
    if (btn) btn.classList.toggle('active', myHandRaised);
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            type: 'raise_hand',
            raised: myHandRaised,
            user_name: currentUserName,
        }));
    }
}

function onRaiseHand(data) {
    const uid = data.user_id;
    if (data.raised) {
        raisedHandsMap.set(uid, data.user_name || `User ${uid}`);
        if (uid !== currentUserId) {
            Toast && Toast.info('Giơ tay', `${esc(data.user_name || 'Ai đó')} đã giơ tay ✋`);
        }
    } else {
        raisedHandsMap.delete(uid);
    }
    renderRaisedHands();
    updateVideoTileHands();
    updateMemberHandIcon(uid, data.raised);
}

function renderRaisedHands() {
    const section = document.getElementById('raisedHandsSection');
    const list = document.getElementById('raisedHandsList');
    if (!section || !list) return;
    if (raisedHandsMap.size === 0) {
        section.classList.add('hidden');
        list.innerHTML = '';
        return;
    }
    section.classList.remove('hidden');
    let html = '';
    let idx = 1;
    raisedHandsMap.forEach((name, uid) => {
        html += `<div class="raised-hand-item">
            <span class="rh-num">${idx}</span>
            <span class="rh-name">${esc(name)}</span>
            <span class="rh-icon">✋</span>
        </div>`;
        idx++;
    });
    list.innerHTML = html;
}

function updateVideoTileHands() {
    raisedHandsMap.forEach((_, uid) => {
        const el = document.getElementById(`tileHand-${uid}`);
        if (el) el.classList.remove('hidden');
    });
    // Ẩn những ai đã hạ tay
    document.querySelectorAll('[id^="tileHand-"]').forEach(el => {
        const uid = parseInt(el.id.replace('tileHand-', ''));
        if (!raisedHandsMap.has(uid)) el.classList.add('hidden');
    });
}

function updateMemberHandIcon(uid, raised) {
    const el = document.getElementById(`member-item-${uid}`);
    if (!el) return;
    let handEl = el.querySelector('.member-hand-icon');
    if (raised) {
        if (!handEl) {
            handEl = document.createElement('span');
            handEl.className = 'member-hand-icon';
            handEl.textContent = '✋';
            el.appendChild(handEl);
        }
    } else {
        if (handEl) handEl.remove();
    }
}

// ── Reactions ─────────────────────────────────────────────────────────────

function toggleReactionPicker() {
    const picker = document.getElementById('reactionPicker');
    if (picker) picker.classList.toggle('visible');
}

function closeReactionPicker() {
    const picker = document.getElementById('reactionPicker');
    if (picker) picker.classList.remove('visible');
}

function sendReaction(emoji) {
    closeReactionPicker();
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'reaction', emoji, user_name: currentUserName }));
    }
}

function onReaction(data) {
    showFloatingReaction(data.emoji, data.user_name || `User ${data.user_id}`);
}

function showFloatingReaction(emoji, userName) {
    const overlay = document.getElementById('reactionOverlay');
    if (!overlay) return;
    const el = document.createElement('div');
    el.className = 'floating-reaction';
    // Random vị trí ngang
    el.style.left = `${10 + Math.random() * 80}%`;
    el.innerHTML = `<span class="fr-emoji">${emoji}</span><span class="fr-name">${esc(userName)}</span>`;
    overlay.appendChild(el);
    setTimeout(() => el.remove(), 3500);
}

// ── Chat ──────────────────────────────────────────────────────────────────

function toggleChat() {
    chatOpen = !chatOpen;
    const panel = document.getElementById('chatPanel');
    const btn = document.getElementById('btnToggleChat');
    const body = document.querySelector('.meeting-body');
    if (panel) panel.classList.toggle('hidden', !chatOpen);
    if (btn) btn.classList.toggle('active', chatOpen);
    if (body) body.classList.toggle('chat-visible', chatOpen);
    if (chatOpen) {
        chatUnread = 0;
        updateChatBadge();
        scrollChatToBottom();
    }
}

async function loadChatHistory() {
    try {
        const res = await fetch(`${API}/api/rooms/${currentRoomCode}/messages`, { headers: getMeetingAuthHeaders() });
        if (!res.ok) return;
        const messages = await res.json();
        messages.forEach(m => appendChatMessage({
            user_id: m.user_id,
            user_name: m.user_name,
            content: m.content,
            sent_at: m.sent_at,
            fromHistory: true,
        }));
    } catch (_) { }
}

function sendChat() {
    const input = document.getElementById('chatInput');
    if (!input) return;
    const content = input.value.trim();
    if (!content) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        Toast && Toast.error('Chưa kết nối', 'Không thể gửi tin nhắn');
        return;
    }
    ws.send(JSON.stringify({
        type: 'chat_message',
        content,
        user_name: currentUserName,
    }));
    input.value = '';
}

function onChatKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChat();
    }
}

function onChatMessage(data) {
    appendChatMessage(data);
    if (!chatOpen) {
        chatUnread++;
        updateChatBadge();
    }
}

function appendChatMessage(data) {
    const feed = document.getElementById('chatMessages');
    if (!feed) return;

    // Xóa empty state
    const empty = feed.querySelector('.chat-empty');
    if (empty) empty.remove();

    const isMe = data.user_id === currentUserId;
    const name = data.user_name || `User ${data.user_id}`;
    const initial = name.charAt(0).toUpperCase();
    const time = data.sent_at
        ? new Date(data.sent_at).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' })
        : new Date().toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' });

    const el = document.createElement('div');
    el.className = `chat-msg${isMe ? ' chat-msg-me' : ''}`;
    el.innerHTML = isMe
        ? `<div class="chat-msg-body">
            <div class="chat-msg-content">${esc(data.content)}</div>
            <div class="chat-msg-meta">${esc(name)} · ${time}</div>
           </div>
           <div class="chat-avatar chat-avatar-me">${initial}</div>`
        : `<div class="chat-avatar">${initial}</div>
           <div class="chat-msg-body">
            <div class="chat-msg-sender">${esc(name)}</div>
            <div class="chat-msg-content">${esc(data.content)}</div>
            <div class="chat-msg-meta">${time}</div>
           </div>`;
    feed.appendChild(el);
    scrollChatToBottom();
}

function scrollChatToBottom() {
    const feed = document.getElementById('chatMessages');
    if (feed) feed.scrollTop = feed.scrollHeight;
}

function updateChatBadge() {
    const badge = document.getElementById('chatBadge');
    if (!badge) return;
    if (chatUnread > 0) {
        badge.textContent = chatUnread > 99 ? '99+' : chatUnread;
        badge.classList.remove('hidden');
    } else {
        badge.classList.add('hidden');
    }
}

// Boot
document.addEventListener('DOMContentLoaded', init);
