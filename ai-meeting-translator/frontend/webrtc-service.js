/**
 * WebRTC Service — Module 09.3
 * Quản lý RTCPeerConnection mesh cho cuộc gọi âm thanh/video nhiều người.
 */

const WebRTCService = (() => {
    const API = 'http://127.0.0.1:8000';

    let _iceServers = [
        { urls: 'stun:stun.l.google.com:19302' },
        { urls: 'stun:stun1.l.google.com:19302' },
    ];
    let _localUserId = null;
    let _localStream = null;       // audio (mic)
    let _localVideoStream = null;  // camera
    let _localScreenStream = null; // screen share
    let _isMicOn = true;
    let _isCamOn = false;
    let _isScreenOn = false;
    let _isInCall = false;
    let _onStateChange = null;

    const _peers = new Map();           // peerId → RTCPeerConnection
    const _remoteDescSet = new Map();   // peerId → bool
    const _iceCandidateBuffer = new Map(); // peerId → []
    const _audioElements = new Map();   // peerId → <audio>
    let _ws = null;

    // ── Helpers ───────────────────────────────────────────────────────────
    function _emit(event) {
        if (typeof _onStateChange === 'function') {
            try { _onStateChange(event); } catch (e) { console.error('[WebRTC]', e); }
        }
    }
    function _log(...a) { console.log('[WebRTC]', ...a); }
    function _warn(...a) { console.warn('[WebRTC]', ...a); }
    function _send(msg) {
        if (_ws && _ws.readyState === WebSocket.OPEN) _ws.send(JSON.stringify(msg));
        else _warn('WS not open:', msg.type);
    }

    // ── ICE config ────────────────────────────────────────────────────────
    async function _loadIceConfig() {
        try {
            const res = await fetch(`${API}/api/webrtc/ice-config`);
            if (res.ok) {
                const data = await res.json();
                if (data.iceServers?.length) {
                    _iceServers = data.iceServers;
                    _log('ICE config loaded');
                }
            }
        } catch (e) { _warn('ICE config fallback:', e.message); }
    }

    // ── Peer management ───────────────────────────────────────────────────
    function _hasPeer(id) { return _peers.has(id); }

    function _createPeer(peerId) {
        if (_peers.has(peerId)) return _peers.get(peerId);

        const pc = new RTCPeerConnection({ iceServers: _iceServers });
        _peers.set(peerId, pc);
        _remoteDescSet.set(peerId, false);
        _iceCandidateBuffer.set(peerId, []);

        // Gắn local audio track
        if (_localStream) _localStream.getTracks().forEach(t => pc.addTrack(t, _localStream));

        pc.ontrack = (e) => {
            _log(`Track from peer ${peerId}: ${e.track.kind}`);
            const stream = e.streams[0];
            if (e.track.kind === 'audio') {
                _attachAudio(peerId, stream);
            } else if (e.track.kind === 'video') {
                const kind = _inferVideoKind(e.track);
                _emit({ type: 'peer_video', peerId, stream, kind });
                e.track.onended = () => {
                    // replaceTrack có thể ended track cũ trong khi track mới vẫn live
                    const live = pc.getReceivers().find(r =>
                        r.track && r.track.kind === 'video' && r.track.readyState === 'live' && r.track !== e.track
                    );
                    if (live) return;
                    _log(`Video track ended from peer ${peerId}`);
                    _emit({ type: 'peer_video', peerId, stream: null, kind });
                };
            }
            _emit({ type: 'peer_stream', peerId, stream });
        };

        pc.onicecandidate = (e) => {
            if (e.candidate) _send({ type: 'webrtc_ice_candidate', target_user_id: peerId, candidate: e.candidate.toJSON() });
        };

        pc.oniceconnectionstatechange = () => {
            _log(`ICE ${peerId}: ${pc.iceConnectionState}`);
            _emit({ type: 'ice_state', peerId, state: pc.iceConnectionState });
            if (['disconnected', 'failed', 'closed'].includes(pc.iceConnectionState)) _closePeer(peerId);
        };

        pc.onconnectionstatechange = () => {
            _log(`Conn ${peerId}: ${pc.connectionState}`);
            _emit({ type: 'connection_state', peerId, state: pc.connectionState });
        };

        _log(`Created peer ${peerId}`);
        return pc;
    }

    function _attachAudio(peerId, stream) {
        let audio = _audioElements.get(peerId);
        if (!audio) {
            audio = document.createElement('audio');
            audio.autoplay = true;
            audio.playsinline = true;
            audio.style.display = 'none';
            document.body.appendChild(audio);
            _audioElements.set(peerId, audio);
        }
        audio.srcObject = stream;
        const p = audio.play();
        if (p !== undefined) {
            p.catch(err => {
                _warn('Autoplay blocked for peer ' + peerId + ': ' + err.message);
                _emit({ type: 'autoplay_blocked', peerId });
                document.addEventListener('click', function retry() {
                    audio.play().catch(() => { });
                    document.removeEventListener('click', retry);
                }, { once: true });
            });
        }
    }

    function _closePeer(peerId) {
        const pc = _peers.get(peerId);
        if (pc) {
            pc.ontrack = pc.onicecandidate = pc.oniceconnectionstatechange = pc.onconnectionstatechange = null;
            try { pc.close(); } catch (_) { }
            _peers.delete(peerId);
        }
        _remoteDescSet.delete(peerId);
        _iceCandidateBuffer.delete(peerId);
        const audio = _audioElements.get(peerId);
        if (audio) { audio.srcObject = null; audio.remove(); _audioElements.delete(peerId); }
        _emit({ type: 'peer_closed', peerId });
        _log(`Closed peer ${peerId}`);
    }

    async function _flushIceCandidates(peerId) {
        const pc = _peers.get(peerId);
        const buf = _iceCandidateBuffer.get(peerId) || [];
        if (!pc || !buf.length) return;
        const toFlush = buf.splice(0);
        for (const c of toFlush) {
            try { await pc.addIceCandidate(new RTCIceCandidate(c)); }
            catch (e) { _warn('addIceCandidate flush:', e.message); }
        }
    }

    function _inferVideoKind(track) {
        if (!track) return 'camera';
        const s = (typeof track.getSettings === 'function' && track.getSettings()) || {};
        if (s.displaySurface) return 'screen';
        if (track.contentHint === 'detail') return 'screen';
        if (track.contentHint === 'motion') return 'camera';
        const label = String(track.label || '').toLowerCase();
        if (/screen|window|tab|display|monitor|web page|cửa sổ|màn hình/.test(label)) return 'screen';
        return 'camera';
    }

    async function _setOutgoingVideo(track, stream) {
        for (const [peerId, pc] of _peers) {
            const videoSender = pc.getSenders().find(s => s.track && s.track.kind === 'video');
            if (videoSender) {
                await videoSender.replaceTrack(track);
            } else if (track) {
                pc.addTrack(track, stream);
                await _renegotiate(peerId, pc);
            }
        }
    }

    async function _clearOutgoingVideo() {
        for (const [peerId, pc] of _peers) {
            const sender = pc.getSenders().find(s => s.track && s.track.kind === 'video');
            if (sender) {
                pc.removeTrack(sender);
                await _renegotiate(peerId, pc);
            }
        }
    }

    async function _restoreCameraOrClear() {
        const cam = _localVideoStream?.getVideoTracks()?.[0];
        if (_isCamOn && cam && cam.readyState === 'live') {
            cam.contentHint = 'motion';
            await _setOutgoingVideo(cam, _localVideoStream);
            return;
        }
        await _clearOutgoingVideo();
    }

    /** Renegotiate sau khi thêm/xóa track */
    async function _renegotiate(peerId, pc) {
        try {
            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);
            _send({ type: 'webrtc_offer', target_user_id: peerId, sdp: offer });
            _log(`Renegotiate offer → ${peerId}`);
        } catch (e) { _warn(`renegotiate ${peerId}:`, e.message); }
    }

    // ── Public API ────────────────────────────────────────────────────────

    async function init() { await _loadIceConfig(); }

    function setWebSocket(ws) { _ws = ws; }

    async function joinCall(userId, existingPeers = []) {
        if (_isInCall) { _warn('Already in call'); return _localStream; }
        _localUserId = userId;
        try {
            _localStream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
                video: false,
            });
        } catch (err) {
            const reason = _getMicError(err);
            _emit({ type: 'mic_error', reason, error: err });
            throw new Error(reason);
        }
        _isInCall = true;
        _isMicOn = true;
        _emit({ type: 'call_joined', userId, stream: _localStream });
        _send({ type: 'webrtc_ready', user_id: userId });
        for (const peerId of existingPeers) {
            if (peerId !== userId) await _initiateOffer(peerId);
        }
        return _localStream;
    }

    async function _initiateOffer(peerId) {
        if (_hasPeer(peerId)) { _warn(`Peer ${peerId} already exists`); return; }
        try {
            const pc = _createPeer(peerId);
            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);
            _send({ type: 'webrtc_offer', target_user_id: peerId, sdp: offer });
            _log(`Offer → ${peerId}`);
        } catch (e) { _warn(`createOffer ${peerId}:`, e.message); _closePeer(peerId); }
    }

    async function handleOffer(fromId, sdp) {
        try {
            let pc = _peers.get(fromId);
            if (!pc) pc = _createPeer(fromId);
            await pc.setRemoteDescription(new RTCSessionDescription(sdp));
            _remoteDescSet.set(fromId, true);
            await _flushIceCandidates(fromId);
            const answer = await pc.createAnswer();
            await pc.setLocalDescription(answer);
            _send({ type: 'webrtc_answer', target_user_id: fromId, sdp: answer });
            _log(`Answer → ${fromId}`);
        } catch (e) { _warn(`handleOffer ${fromId}:`, e.message); _closePeer(fromId); }
    }

    async function handleAnswer(fromId, sdp) {
        const pc = _peers.get(fromId);
        if (!pc) { _warn(`No peer ${fromId}`); return; }
        try {
            await pc.setRemoteDescription(new RTCSessionDescription(sdp));
            _remoteDescSet.set(fromId, true);
            await _flushIceCandidates(fromId);
            _log(`Answer from ${fromId}`);
        } catch (e) { _warn(`handleAnswer ${fromId}:`, e.message); }
    }

    async function handleIceCandidate(fromId, candidate) {
        if (!_peers.has(fromId)) {
            if (!_iceCandidateBuffer.has(fromId)) _iceCandidateBuffer.set(fromId, []);
            _iceCandidateBuffer.get(fromId).push(candidate);
            return;
        }
        const pc = _peers.get(fromId);
        if (!_remoteDescSet.get(fromId)) {
            _iceCandidateBuffer.get(fromId).push(candidate);
            return;
        }
        try { await pc.addIceCandidate(new RTCIceCandidate(candidate)); }
        catch (e) { _warn('addIceCandidate:', e.message); }
    }

    function handlePeerLeft(peerId) {
        if (_hasPeer(peerId)) { _closePeer(peerId); _log(`Peer ${peerId} left`); }
    }

    function toggleMic() {
        if (!_localStream) return _isMicOn;
        _isMicOn = !_isMicOn;
        _localStream.getAudioTracks().forEach(t => { t.enabled = _isMicOn; });
        _emit({ type: 'mic_toggled', enabled: _isMicOn });
        return _isMicOn;
    }

    async function toggleCamera() {
        if (!_isCamOn) {
            try {
                _localVideoStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
                const videoTrack = _localVideoStream.getVideoTracks()[0];
                if (videoTrack) videoTrack.contentHint = 'motion';
                // Đang chia sẻ màn hình: giữ track màn hình trên peer, camera chỉ preview local
                if (!_isScreenOn) await _setOutgoingVideo(videoTrack, _localVideoStream);
                _isCamOn = true;
                _emit({ type: 'cam_toggled', enabled: true, stream: _localVideoStream });
            } catch (err) {
                _emit({ type: 'cam_error', reason: err.name === 'NotAllowedError' ? 'Từ chối quyền camera' : err.message });
            }
        } else {
            if (_localVideoStream) {
                _localVideoStream.getVideoTracks().forEach(t => t.stop());
                _localVideoStream = null;
            }
            _isCamOn = false;
            if (!_isScreenOn) await _clearOutgoingVideo();
            _emit({ type: 'cam_toggled', enabled: false, stream: null });
        }
        return _isCamOn;
    }

    async function toggleScreen() {
        if (!_isScreenOn) {
            try {
                _localScreenStream = await navigator.mediaDevices.getDisplayMedia({ video: { cursor: 'always' }, audio: false });
                const screenTrack = _localScreenStream.getVideoTracks()[0];
                if (screenTrack) screenTrack.contentHint = 'detail';
                await _setOutgoingVideo(screenTrack, _localScreenStream);
                screenTrack.onended = async () => {
                    if (!_isScreenOn) return;
                    _isScreenOn = false;
                    _localScreenStream = null;
                    _emit({ type: 'screen_toggled', enabled: false, stream: null });
                    await _restoreCameraOrClear();
                };
                _isScreenOn = true;
                _emit({ type: 'screen_toggled', enabled: true, stream: _localScreenStream });
            } catch (err) {
                if (err.name !== 'NotAllowedError') _emit({ type: 'screen_error', reason: err.message });
            }
        } else {
            _isScreenOn = false;
            if (_localScreenStream) {
                const track = _localScreenStream.getVideoTracks()[0];
                if (track) track.onended = null;
                _localScreenStream.getTracks().forEach(t => t.stop());
                _localScreenStream = null;
            }
            _emit({ type: 'screen_toggled', enabled: false, stream: null });
            await _restoreCameraOrClear();
        }
        return _isScreenOn;
    }

    function leaveCall() {
        _send({ type: 'webrtc_leave', user_id: _localUserId });
        for (const peerId of [..._peers.keys()]) _closePeer(peerId);
        if (_localStream) { _localStream.getTracks().forEach(t => t.stop()); _localStream = null; }
        if (_localVideoStream) { _localVideoStream.getTracks().forEach(t => t.stop()); _localVideoStream = null; }
        if (_localScreenStream) { _localScreenStream.getTracks().forEach(t => t.stop()); _localScreenStream = null; }
        _isInCall = false; _isMicOn = false; _isCamOn = false; _isScreenOn = false; _localUserId = null;
        _emit({ type: 'call_left' });
    }

    function _getMicError(err) {
        const map = {
            NotAllowedError: 'Người dùng từ chối quyền microphone',
            PermissionDeniedError: 'Người dùng từ chối quyền microphone',
            NotFoundError: 'Không tìm thấy microphone',
            DevicesNotFoundError: 'Không tìm thấy microphone',
            NotReadableError: 'Microphone đang được ứng dụng khác sử dụng',
            TrackStartError: 'Microphone đang được ứng dụng khác sử dụng',
            OverconstrainedError: 'Thiết bị không đáp ứng yêu cầu âm thanh',
        };
        return map[err.name] || `Lỗi microphone: ${err.message}`;
    }

    return {
        init, setWebSocket, joinCall,
        createOffer: _initiateOffer,
        handleOffer, handleAnswer, handleIceCandidate, handlePeerLeft,
        toggleMic, toggleCamera, toggleScreen, leaveCall,
        get isMicOn() { return _isMicOn; },
        get isCamOn() { return _isCamOn; },
        get isScreenOn() { return _isScreenOn; },
        get isInCall() { return _isInCall; },
        get peerCount() { return _peers.size; },
        get localStream() { return _localStream; },
        get localVideoStream() { return _localVideoStream; },
        get localScreenStream() { return _localScreenStream; },
        set onStateChange(fn) { _onStateChange = fn; },
        get onStateChange() { return _onStateChange; },
    };
})();
