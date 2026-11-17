/**
 * WebRTC Service — LinguaMeet (Module 09.3 Robust Multi-Stream Mesh)
 * Quản lý RTCPeerConnection mesh cho âm thanh, camera và chia sẻ màn hình độc lập.
 *
 * Tính năng chính:
 * 1. Hỗ trợ đồng thời cả Camera và Screen Share mà không ghi đè lẫn nhau.
 * 2. Dedicated RTCRtpSender cho Camera và Screen Share per peer.
 * 3. Hỗ trợ Perfect Negotiation (Polite Peer Pattern) xử lý Glare hoàn toàn triệt để.
 * 4. Renegotiation an toàn, tránh InvalidStateError khi signalingState chưa stable.
 * 5. ICE Candidate buffering trước khi remote description sẵn sàng.
 * 6. Tự động phục hồi ICE restart khi gặp lỗi mạng.
 * 7. Phát hiện loại video track (camera / screen) qua hint, settings, meta signaling.
 * 8. API & ICE servers dynamic theo host truy cập (hỗ trợ cross-network LAN / Domain / STUN / TURN).
 */

const WebRTCService = (() => {
    // Dynamic API URL dựa trên window.location để hỗ trợ truy cập qua IP LAN / Domain / Proxy
    function getApiBase() {
        if (typeof window !== 'undefined' && window.location && window.location.protocol.startsWith('http')) {
            const loc = window.location;
            if (loc.port === '8000' || loc.port === '80' || loc.port === '443' || loc.port === '') {
                return loc.protocol + '//' + loc.host;
            }
            return loc.protocol + '//' + (loc.hostname || '127.0.0.1') + ':8000';
        }
        return 'http://127.0.0.1:8000';
    }

    let _iceServers = [
        { urls: 'stun:stun.l.google.com:19302' },
        { urls: 'stun:stun1.l.google.com:19302' },
        { urls: 'stun:stun2.l.google.com:19302' },
        { urls: 'stun:stun.cloudflare.com:3478' },
    ];

    let _localUserId = null;
    let _localStream = null;          // audio (mic)
    let _localVideoStream = null;     // camera MediaStream
    let _localScreenStream = null;    // screen share MediaStream

    let _isMicOn = true;
    let _isCamOn = false;
    let _isScreenOn = false;
    let _isInCall = false;
    let _onStateChange = null;
    let _ws = null;

    // Quản lý Mesh Peers (key luôn được chuẩn hóa thành Number)
    const _peers = new Map();              // peerId (num) -> RTCPeerConnection
    const _remoteDescSet = new Map();      // peerId (num) -> bool
    const _iceCandidateBuffer = new Map(); // peerId (num) -> Array<RTCIceCandidateInit>
    const _audioElements = new Map();      // peerId (num) -> <audio> element

    // Dedicated senders per peer để Camera và Screen không bao giờ ghi đè lên nhau
    const _camSenders = new Map();         // peerId (num) -> RTCRtpSender
    const _screenSenders = new Map();      // peerId (num) -> RTCRtpSender

    // Trạng thái renegotiation & glare
    const _renegotiating = new Map();      // peerId (num) -> bool
    const _needsRenegotiate = new Map();   // peerId (num) -> bool
    const _makingOffer = new Map();        // peerId (num) -> bool

    // Remote peer streams lưu độc lập
    const _peerCameraStreams = new Map();  // peerId (num) -> MediaStream
    const _peerScreenStreams = new Map();  // peerId (num) -> MediaStream
    const _peerTrackState = new Map();     // peerId (num) -> { camera: bool, screen: bool }
    const _peerTrackMeta = new Map();      // trackId (str) -> 'camera' | 'screen'

    // ── Logging & Events Helpers ───────────────────────────────────────────
    function _log(...a) { console.log('[WebRTC]', ...a); }
    function _warn(...a) { console.warn('[WebRTC]', ...a); }
    function _error(...a) { console.error('[WebRTC]', ...a); }

    function _emit(event) {
        if (typeof _onStateChange === 'function') {
            try { _onStateChange(event); } catch (e) { _error('onStateChange callback error:', e); }
        }
    }

    function _send(msg) {
        if (_ws && _ws.readyState === WebSocket.OPEN) {
            _ws.send(JSON.stringify(msg));
        } else {
            _warn('Cannot send WS message (not open):', msg.type);
        }
    }

    function _num(id) {
        const n = Number(id);
        return isNaN(n) ? id : n;
    }

    // ── ICE Configuration ──────────────────────────────────────────────────
    async function _loadIceConfig() {
        try {
            const api = getApiBase();
            const res = await fetch(`${api}/api/webrtc/ice-config`);
            if (res.ok) {
                const data = await res.json();
                if (Array.isArray(data.iceServers) && data.iceServers.length > 0) {
                    _iceServers = data.iceServers;
                    _log('Loaded ICE servers configuration from backend:', _iceServers);
                }
            }
        } catch (e) {
            _warn('ICE config fallback to default STUN list:', e.message);
        }
    }

    // ── Peer Management ────────────────────────────────────────────────────
    function _hasPeer(id) {
        return _peers.has(_num(id));
    }

    function _createPeer(peerId) {
        const id = _num(peerId);
        if (_peers.has(id)) return _peers.get(id);

        _log(`Creating RTCPeerConnection for peer ${id}`);
        const pc = new RTCPeerConnection({ iceServers: _iceServers });
        _peers.set(id, pc);
        _remoteDescSet.set(id, false);
        _iceCandidateBuffer.set(id, []);
        _renegotiating.set(id, false);
        _needsRenegotiate.set(id, false);
        _makingOffer.set(id, false);

        // 1. Thêm Local Audio Track (nếu đã tham gia cuộc gọi)
        if (_localStream) {
            _localStream.getAudioTracks().forEach(t => {
                try {
                    pc.addTrack(t, _localStream);
                    _log(`Added local audio track to peer ${id}`);
                } catch (err) {
                    _warn(`Failed to add audio track to peer ${id}:`, err.message);
                }
            });
        }

        // 2. Thêm Local Camera Video Track (nếu camera đang bật)
        if (_isCamOn && _localVideoStream) {
            const camTrack = _localVideoStream.getVideoTracks()[0];
            if (camTrack && camTrack.readyState === 'live') {
                try {
                    const sender = pc.addTrack(camTrack, _localVideoStream);
                    _camSenders.set(id, sender);
                    _log(`Added local camera track to new peer ${id}`);
                } catch (err) {
                    _warn(`Failed to add camera track to peer ${id}:`, err.message);
                }
            }
        }

        // 3. Thêm Local Screen Share Video Track (nếu đang chia sẻ màn hình)
        if (_isScreenOn && _localScreenStream) {
            const scrTrack = _localScreenStream.getVideoTracks()[0];
            if (scrTrack && scrTrack.readyState === 'live') {
                try {
                    const sender = pc.addTrack(scrTrack, _localScreenStream);
                    _screenSenders.set(id, sender);
                    _log(`Added local screen share track to new peer ${id}`);
                } catch (err) {
                    _warn(`Failed to add screen share track to peer ${id}:`, err.message);
                }
            }
        }

        // onnegotiationneeded: Trình duyệt kích hoạt khi có track thêm/bớt
        pc.onnegotiationneeded = async () => {
            _log(`onnegotiationneeded fired for peer ${id}, state=${pc.signalingState}`);
            if (pc.signalingState !== 'stable' || !_remoteDescSet.get(id)) {
                _needsRenegotiate.set(id, true);
                return;
            }
            await _renegotiate(id, pc);
        };

        // ontrack: Xử lý track âm thanh / video nhận được từ peer
        pc.ontrack = (e) => {
            _log(`Track received from peer ${id}: kind=${e.track.kind} id=${e.track.id} label="${e.track.label}" streams=${e.streams.length}`);

            if (e.track.kind === 'audio') {
                let stream = e.streams && e.streams[0];
                if (!stream) stream = new MediaStream([e.track]);
                _attachAudio(id, stream);
                _emit({ type: 'peer_stream', peerId: id, stream });
                return;
            }

            if (e.track.kind === 'video') {
                const kind = _determineVideoKind(id, e.track, e.streams);
                _log(`Video track from peer ${id}: inferred kind=${kind} (trackId: ${e.track.id})`);

                // Tạo MediaStream độc lập cho video track này
                const videoStream = new MediaStream([e.track]);

                if (kind === 'screen') {
                    _peerScreenStreams.set(id, videoStream);
                } else {
                    _peerCameraStreams.set(id, videoStream);
                }

                _emit({ type: 'peer_video', peerId: id, stream: videoStream, kind, track: e.track });

                e.track.onmute = () => {
                    _log(`Video track muted from peer ${id} (${kind})`);
                };

                e.track.onunmute = () => {
                    _log(`Video track unmuted from peer ${id} (${kind})`);
                    _emit({ type: 'peer_video', peerId: id, stream: videoStream, kind, track: e.track });
                };

                e.track.onended = () => {
                    _log(`Video track ended from peer ${id} (${kind})`);
                    if (kind === 'screen') _peerScreenStreams.delete(id);
                    else _peerCameraStreams.delete(id);
                    _emit({ type: 'peer_video', peerId: id, stream: null, kind });
                };
            }
        };

        // ICE Candidate dispatch
        pc.onicecandidate = (e) => {
            if (e.candidate) {
                _send({
                    type: 'webrtc_ice_candidate',
                    target_user_id: id,
                    candidate: e.candidate.toJSON()
                });
            }
        };

        // Giám sát ICE State
        pc.oniceconnectionstatechange = () => {
            const state = pc.iceConnectionState;
            _log(`ICE state for peer ${id}: ${state}`);
            _emit({ type: 'ice_state', peerId: id, state });

            if (state === 'failed') {
                _warn(`ICE failed for peer ${id}. If devices are on different networks behind symmetric NAT, TURN server is required in backend .env. Attempting ICE restart...`);
                try {
                    pc.restartIce();
                } catch (err) {
                    _warn(`ICE restart error for peer ${id}:`, err.message);
                }
            } else if (state === 'closed') {
                _closePeer(id);
            }
        };

        // Giám sát Connection State
        pc.onconnectionstatechange = () => {
            const state = pc.connectionState;
            _log(`Connection state for peer ${id}: ${state}`);
            _emit({ type: 'connection_state', peerId: id, state });

            if (state === 'failed') {
                _warn(`Connection failed for peer ${id}, attempting ICE restart`);
                try {
                    pc.restartIce();
                } catch (_) { }
            }
        };

        return pc;
    }

    function _determineVideoKind(peerId, track, streams) {
        // 1. Kiểm tra metadata gửi từ WebSocket signaling
        if (_peerTrackMeta.has(track.id)) {
            return _peerTrackMeta.get(track.id);
        }

        // 2. contentHint
        if (track.contentHint === 'detail') return 'screen';
        if (track.contentHint === 'motion') return 'camera';

        // 3. getSettings displaySurface
        const s = (typeof track.getSettings === 'function' && track.getSettings()) || {};
        if (s.displaySurface) return 'screen';

        // 4. Stream ID hint
        if (streams && streams[0] && streams[0].id) {
            const sid = streams[0].id.toLowerCase();
            if (sid.includes('screen')) return 'screen';
            if (sid.includes('cam')) return 'camera';
        }

        // 5. Track label keywords
        const label = String(track.label || '').toLowerCase();
        if (/screen|window|tab|display|monitor|cửa sổ|màn hình/.test(label)) return 'screen';

        // 6. Kiểm tra theo trạng thái hiện thời của peer
        const st = _peerTrackState.get(peerId);
        if (st) {
            if (st.screen && !st.camera) return 'screen';
            if (st.camera && !st.screen) return 'camera';
            if (st.screen && !_peerScreenStreams.has(peerId)) return 'screen';
        }

        return 'camera';
    }

    function _attachAudio(peerId, stream) {
        let audio = _audioElements.get(peerId);
        if (!audio) {
            audio = document.createElement('audio');
            audio.id = `webrtc-peer-audio-${peerId}`;
            audio.autoplay = true;
            audio.playsinline = true;
            audio.volume = 1.0;
            audio.muted = false;
            // Dùng fixed ẩn ngoài màn hình thay vì display:none để trình duyệt không đình chỉ audio playback
            audio.style.cssText = 'position:fixed;top:-9999px;left:-9999px;width:1px;height:1px;opacity:0.001;pointer-events:none;';
            document.body.appendChild(audio);
            _audioElements.set(peerId, audio);
        }
        audio.srcObject = stream;
        audio.volume = 1.0;
        audio.muted = false;
        const p = audio.play();
        if (p !== undefined) {
            p.then(() => {
                _log(`[Audio] Playing audio from peer ${peerId}`);
            }).catch(err => {
                _warn(`Autoplay blocked for peer ${peerId}:`, err.message);
                _emit({ type: 'autoplay_blocked', peerId });
                const retryPlay = () => {
                    audio.play().then(() => {
                        _log(`[Audio] Autoplay unblocked for peer ${peerId}`);
                    }).catch(() => { });
                };
                document.addEventListener('click', retryPlay, { once: true });
                document.addEventListener('keydown', retryPlay, { once: true });
            });
        }
    }

    function unlockAudio() {
        _log('[Audio] Unlocking all peer audio elements...');
        _audioElements.forEach((audio, peerId) => {
            if (audio) {
                audio.muted = false;
                audio.volume = 1.0;
                audio.play().catch(err => {
                    _warn(`Retry audio play failed for peer ${peerId}:`, err.message);
                });
            }
        });
    }

    function _closePeer(peerId) {
        const id = _num(peerId);
        const pc = _peers.get(id);
        if (pc) {
            pc.onnegotiationneeded = null;
            pc.ontrack = null;
            pc.onicecandidate = null;
            pc.oniceconnectionstatechange = null;
            pc.onconnectionstatechange = null;
            try { pc.close(); } catch (_) { }
            _peers.delete(id);
        }
        _remoteDescSet.delete(id);
        _iceCandidateBuffer.delete(id);
        _camSenders.delete(id);
        _screenSenders.delete(id);
        _renegotiating.delete(id);
        _needsRenegotiate.delete(id);
        _makingOffer.delete(id);
        _peerCameraStreams.delete(id);
        _peerScreenStreams.delete(id);
        _peerTrackState.delete(id);

        const audio = _audioElements.get(id);
        if (audio) {
            audio.srcObject = null;
            audio.remove();
            _audioElements.delete(id);
        }
        _emit({ type: 'peer_closed', peerId: id });
        _log(`Closed connection with peer ${id}`);
    }

    async function _flushIceCandidates(peerId) {
        const id = _num(peerId);
        const pc = _peers.get(id);
        const buf = _iceCandidateBuffer.get(id) || [];
        if (!pc || !buf.length) return;

        const toFlush = buf.splice(0);
        for (const c of toFlush) {
            try {
                await pc.addIceCandidate(new RTCIceCandidate(c));
            } catch (e) {
                _warn(`addIceCandidate flush error for peer ${id}:`, e.message);
            }
        }
    }

    // ── Renegotiation Engine ───────────────────────────────────────────────
    async function _renegotiate(peerId, pc) {
        const id = _num(peerId);
        if (!pc || pc.signalingState === 'closed') return;

        if (pc.signalingState !== 'stable') {
            _log(`Cannot renegotiate peer ${id} immediately: signalingState=${pc.signalingState}, queuing.`);
            _needsRenegotiate.set(id, true);
            return;
        }

        if (_renegotiating.get(id) || _makingOffer.get(id)) {
            _needsRenegotiate.set(id, true);
            return;
        }

        _renegotiating.set(id, true);
        _makingOffer.set(id, true);

        try {
            const offer = await pc.createOffer();
            if (pc.signalingState !== 'stable') {
                _log(`Signaling state changed while creating offer for ${id}: ${pc.signalingState}. Aborting.`);
                return;
            }
            await pc.setLocalDescription(offer);
            _send({
                type: 'webrtc_offer',
                target_user_id: id,
                sdp: pc.localDescription
            });
            _log(`Renegotiation offer sent to peer ${id}`);
        } catch (e) {
            _warn(`Renegotiation error with peer ${id}:`, e.message);
        } finally {
            _renegotiating.set(id, false);
            _makingOffer.set(id, false);
        }
    }

    function _broadcastTrackState() {
        if (!_localUserId) return;
        _send({
            type: 'webrtc_track_state',
            user_id: _localUserId,
            camera: _isCamOn,
            screen: _isScreenOn
        });
    }

    function _broadcastTrackMeta(trackId, kind) {
        if (!_localUserId || !trackId) return;
        _send({
            type: 'webrtc_track_meta',
            user_id: _localUserId,
            track_id: trackId,
            kind: kind
        });
    }

    // ── Public API ─────────────────────────────────────────────────────────

    async function init() {
        await _loadIceConfig();
    }

    function setWebSocket(ws) {
        _ws = ws;
    }

    async function joinCall(userId, existingPeers = [], options = {}) {
        if (_isInCall) {
            _warn('Already in call');
            return _localStream;
        }
        _localUserId = _num(userId);

        const initialMic = (options && options.initialMic !== undefined) ? !!options.initialMic : true;
        const initialCam = (options && options.initialCam !== undefined) ? !!options.initialCam : false;

        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            const reason = (window.isSecureContext === false)
                ? 'Trình duyệt yêu cầu kết nối HTTPS hoặc localhost để cấp quyền microphone'
                : 'Trình duyệt không hỗ trợ getUserMedia';
            _emit({ type: 'mic_error', reason, error: new Error(reason) });
            _isInCall = true;
            _isMicOn = false;
            _isCamOn = false;
            _emit({ type: 'call_joined', userId: _localUserId, stream: null, micOn: false, camOn: false });
            return null;
        }

        try {
            _localStream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
                video: false
            });
            _isMicOn = initialMic;
            // Áp dụng trạng thái mic thực tế vào track: nếu initialMic=false thì mute track ngay
            _localStream.getAudioTracks().forEach(t => {
                t.enabled = _isMicOn;
            });
        } catch (err) {
            const reason = _getMediaError(err, 'microphone');
            _warn('Microphone access failed on joinCall:', reason);
            _emit({ type: 'mic_error', reason, error: err });
            _localStream = null;
            _isMicOn = false;
            // Không chặn cuộc gọi để người dùng vẫn có thể xem video/chat
        }

        _isInCall = true;

        // Đảm bảo audio track được gán vào mọi peer connection đã khởi tạo trước đó
        if (_localStream) {
            _peers.forEach((pc, id) => {
                const senders = pc.getSenders();
                _localStream.getAudioTracks().forEach(t => {
                    const hasTrack = senders.some(s => s.track && s.track.kind === 'audio');
                    if (!hasTrack) {
                        try {
                            pc.addTrack(t, _localStream);
                            _log(`Attached local audio track to existing peer ${id}`);
                        } catch (e) {
                            _warn(`Failed attaching audio track to peer ${id}:`, e.message);
                        }
                    }
                });
            });
        }

        // Khởi tạo camera nếu người dùng đã bật từ màn hình chờ
        if (initialCam) {
            try {
                await toggleCamera(true);
            } catch (camErr) {
                _warn('Initial camera activation failed:', camErr);
                _isCamOn = false;
            }
        } else {
            _isCamOn = false;
        }

        _emit({ type: 'call_joined', userId: _localUserId, stream: _localStream, micOn: _isMicOn, camOn: _isCamOn });

        // Thông báo trạng thái track cho các thành viên trong phòng
        _broadcastTrackState();

        // Thông báo cho phòng họp rằng ta đã sẵn sàng
        _send({ type: 'webrtc_ready', user_id: _localUserId });

        // Khởi tạo Offer đến các peer đã có mặt trong phòng
        for (const peerId of existingPeers) {
            const id = _num(peerId);
            if (id !== _localUserId) {
                await _initiateOffer(id);
            }
        }

        return _localStream;
    }

    async function _initiateOffer(peerId) {
        const id = _num(peerId);
        if (_hasPeer(id)) {
            _warn(`Peer ${id} already exists`);
            return;
        }
        try {
            const pc = _createPeer(id);
            _makingOffer.set(id, true);
            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);
            _send({
                type: 'webrtc_offer',
                target_user_id: id,
                sdp: pc.localDescription
            });
            _log(`Initial offer sent to peer ${id}`);
        } catch (e) {
            _warn(`_initiateOffer failed for ${id}:`, e.message);
            _closePeer(id);
        } finally {
            _makingOffer.set(id, false);
        }
    }

    async function handleOffer(fromId, sdp) {
        const id = _num(fromId);
        try {
            let pc = _peers.get(id);
            if (!pc) pc = _createPeer(id);

            // Perfect negotiation / Glare handling:
            // Peer có id nhỏ hơn là Polite Peer (chấp nhận rollback), id lớn hơn là Impolite Peer
            const isPolite = Number(_localUserId) < id;
            const offerCollision = (_makingOffer.get(id) || pc.signalingState !== 'stable');

            if (offerCollision) {
                if (!isPolite) {
                    _log(`Glare collision with peer ${id} (impolite): ignoring offer`);
                    return;
                }
                _log(`Glare collision with peer ${id} (polite): rolling back local offer`);
                await pc.setLocalDescription({ type: 'rollback' });
            }

            await pc.setRemoteDescription(new RTCSessionDescription(sdp));
            _remoteDescSet.set(id, true);
            await _flushIceCandidates(id);

            // Đảm bảo Local Audio Track được add vào pc trước khi tạo answer
            if (_localStream) {
                const senders = pc.getSenders();
                _localStream.getAudioTracks().forEach(t => {
                    const hasTrack = senders.some(s => s.track && s.track.kind === 'audio');
                    if (!hasTrack) {
                        try {
                            pc.addTrack(t, _localStream);
                            _log(`Added local audio track before answer to peer ${id}`);
                        } catch (err) {
                            _warn(`Failed to add audio track before answer to peer ${id}:`, err.message);
                        }
                    }
                });
            }

            const answer = await pc.createAnswer();
            await pc.setLocalDescription(answer);
            _send({
                type: 'webrtc_answer',
                target_user_id: id,
                sdp: pc.localDescription
            });
            _log(`Answer sent to peer ${id}`);
        } catch (e) {
            _warn(`handleOffer error from peer ${id}:`, e.message);
        }
    }

    async function handleAnswer(fromId, sdp) {
        const id = _num(fromId);
        const pc = _peers.get(id);
        if (!pc) {
            _warn(`handleAnswer: no peer ${id}`);
            return;
        }
        if (pc.signalingState !== 'have-local-offer') {
            _warn(`handleAnswer for peer ${id}: unexpected signalingState=${pc.signalingState}`);
            return;
        }
        try {
            await pc.setRemoteDescription(new RTCSessionDescription(sdp));
            _remoteDescSet.set(id, true);
            await _flushIceCandidates(id);
            _log(`Answer accepted from peer ${id}`);

            // Nếu có renegotiation đang chờ, kích hoạt ngay khi state trở về stable
            if (_needsRenegotiate.get(id)) {
                _needsRenegotiate.set(id, false);
                await _renegotiate(id, pc);
            }
        } catch (e) {
            _warn(`handleAnswer error from peer ${id}:`, e.message);
        }
    }

    async function handleIceCandidate(fromId, candidate) {
        const id = _num(fromId);
        if (!candidate) return;

        const pc = _peers.get(id);
        if (!pc || !_remoteDescSet.get(id) || !pc.remoteDescription) {
            if (!_iceCandidateBuffer.has(id)) _iceCandidateBuffer.set(id, []);
            _iceCandidateBuffer.get(id).push(candidate);
            return;
        }

        try {
            await pc.addIceCandidate(new RTCIceCandidate(candidate));
        } catch (e) {
            _warn(`addIceCandidate error from peer ${id}:`, e.message);
        }
    }

    function handlePeerLeft(peerId) {
        const id = _num(peerId);
        if (_hasPeer(id)) {
            _closePeer(id);
            _log(`Peer ${id} left call`);
        }
    }

    function handleTrackState(fromId, state) {
        const id = _num(fromId);
        _peerTrackState.set(id, state);
        _log(`Peer ${id} updated track state:`, state);

        if (!state.camera && _peerCameraStreams.has(id)) {
            _peerCameraStreams.delete(id);
            _emit({ type: 'peer_video', peerId: id, stream: null, kind: 'camera' });
        }
        if (!state.screen && _peerScreenStreams.has(id)) {
            _peerScreenStreams.delete(id);
            _emit({ type: 'peer_video', peerId: id, stream: null, kind: 'screen' });
        }
    }

    function handleTrackMeta(fromId, meta) {
        if (meta && meta.track_id && meta.kind) {
            _peerTrackMeta.set(meta.track_id, meta.kind);
            _log(`Recorded track meta: ${meta.track_id} -> ${meta.kind}`);
        }
    }

    function toggleMic(forceState) {
        if (!_localStream) {
            // Nếu chưa có stream (do lúc đầu lỗi quyền hoặc không có thiết bị), thử xin lại nếu muốn bật
            if (forceState === true || (forceState === undefined && !_isMicOn)) {
                if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
                    navigator.mediaDevices.getUserMedia({
                        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
                        video: false
                    }).then(stream => {
                        _localStream = stream;
                        _isMicOn = true;
                        _localStream.getAudioTracks().forEach(t => { t.enabled = true; });
                        _peers.forEach((pc, id) => {
                            _localStream.getAudioTracks().forEach(t => {
                                try { pc.addTrack(t, _localStream); } catch (_) {}
                            });
                        });
                        _broadcastTrackState();
                        _emit({ type: 'mic_toggled', enabled: true, stream: _localStream });
                    }).catch(err => {
                        _isMicOn = false;
                        _emit({ type: 'mic_error', reason: _getMediaError(err, 'microphone'), error: err });
                    });
                }
            }
            return _isMicOn;
        }

        _isMicOn = (forceState !== undefined) ? !!forceState : !_isMicOn;
        _localStream.getAudioTracks().forEach(t => { t.enabled = _isMicOn; });
        _broadcastTrackState();
        _emit({ type: 'mic_toggled', enabled: _isMicOn });
        return _isMicOn;
    }

    async function toggleCamera(forceState) {
        if (forceState !== undefined) {
            if (forceState === _isCamOn) return _isCamOn;
        }
        if (!_isCamOn) {
            try {
                if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                    throw Object.assign(new Error('Yêu cầu HTTPS hoặc localhost để dùng camera'), { name: 'InsecureContext' });
                }

                _localVideoStream = await navigator.mediaDevices.getUserMedia({
                    video: { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } },
                    audio: false
                });

                const camTrack = _localVideoStream.getVideoTracks()[0];
                if (!camTrack) throw new Error('Không lấy được video track từ camera');
                camTrack.contentHint = 'motion';
                _isCamOn = true;

                // Gửi camera track tới tất cả peers qua dedicated Camera Sender
                for (const [peerId, pc] of _peers) {
                    if (pc.signalingState === 'closed') continue;
                    let sender = _camSenders.get(peerId);
                    if (sender) {
                        try {
                            await sender.replaceTrack(camTrack);
                            _log(`Replaced camera track on existing sender for peer ${peerId}`);
                        } catch (err) {
                            _warn(`replaceTrack failed for peer ${peerId}:`, err.message);
                        }
                    } else {
                        try {
                            sender = pc.addTrack(camTrack, _localVideoStream);
                            _camSenders.set(peerId, sender);
                            _log(`Added camera track (new sender) to peer ${peerId}`);
                            if (_remoteDescSet.get(peerId)) {
                                await _renegotiate(peerId, pc);
                            }
                        } catch (err) {
                            _warn(`addTrack failed for peer ${peerId}:`, err.message);
                        }
                    }
                }

                _broadcastTrackMeta(camTrack.id, 'camera');
                _broadcastTrackState();
                _emit({ type: 'cam_toggled', enabled: true, stream: _localVideoStream });
            } catch (err) {
                const reason = _getMediaError(err, 'camera');
                _warn('Camera error:', reason);
                _emit({ type: 'cam_error', reason, error: err });
            }
        } else {
            // Tắt camera
            _isCamOn = false;
            for (const [peerId, pc] of _peers) {
                if (pc.signalingState === 'closed') continue;
                const sender = _camSenders.get(peerId);
                if (sender) {
                    try {
                        await sender.replaceTrack(null);
                        _log(`Silenced camera sender for peer ${peerId}`);
                    } catch (err) {
                        _warn(`replaceTrack(null) failed for peer ${peerId}:`, err.message);
                    }
                }
            }

            if (_localVideoStream) {
                _localVideoStream.getTracks().forEach(t => t.stop());
                _localVideoStream = null;
            }

            _broadcastTrackState();
            _emit({ type: 'cam_toggled', enabled: false, stream: null });
        }
        return _isCamOn;
    }

    async function toggleScreen() {
        if (!_isScreenOn) {
            try {
                if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
                    throw Object.assign(new Error('Trình duyệt không hỗ trợ chia sẻ màn hình'), { name: 'NotSupportedError' });
                }

                _localScreenStream = await navigator.mediaDevices.getDisplayMedia({
                    video: { cursor: 'always' },
                    audio: false
                });

                const screenTrack = _localScreenStream.getVideoTracks()[0];
                if (!screenTrack) throw new Error('Không lấy được video track từ màn hình chia sẻ');
                screenTrack.contentHint = 'detail';
                _isScreenOn = true;

                // Xử lý khi người dùng ấn nút "Stop sharing" mặc định của trình duyệt
                screenTrack.onended = async () => {
                    _log('Screen share ended by browser native stop UI');
                    if (!_isScreenOn) return;
                    _isScreenOn = false;

                    for (const [peerId, pc] of _peers) {
                        if (pc.signalingState === 'closed') continue;
                        const sender = _screenSenders.get(peerId);
                        if (sender) {
                            try {
                                await sender.replaceTrack(null);
                            } catch (_) { }
                        }
                    }

                    if (_localScreenStream) {
                        _localScreenStream.getTracks().forEach(t => t.stop());
                        _localScreenStream = null;
                    }

                    _broadcastTrackState();
                    _emit({ type: 'screen_toggled', enabled: false, stream: null });
                };

                // Gửi screen track tới tất cả peers qua dedicated Screen Sender
                for (const [peerId, pc] of _peers) {
                    if (pc.signalingState === 'closed') continue;
                    let sender = _screenSenders.get(peerId);
                    if (sender) {
                        try {
                            await sender.replaceTrack(screenTrack);
                            _log(`Replaced screen track on existing sender for peer ${peerId}`);
                        } catch (err) {
                            _warn(`replaceTrack failed for peer ${peerId}:`, err.message);
                        }
                    } else {
                        try {
                            sender = pc.addTrack(screenTrack, _localScreenStream);
                            _screenSenders.set(peerId, sender);
                            _log(`Added screen track (new sender) to peer ${peerId}`);
                            if (_remoteDescSet.get(peerId)) {
                                await _renegotiate(peerId, pc);
                            }
                        } catch (err) {
                            _warn(`addTrack failed for peer ${peerId}:`, err.message);
                        }
                    }
                }

                _broadcastTrackMeta(screenTrack.id, 'screen');
                _broadcastTrackState();
                _emit({ type: 'screen_toggled', enabled: true, stream: _localScreenStream });
            } catch (err) {
                if (err.name !== 'NotAllowedError') {
                    const reason = _getMediaError(err, 'screen');
                    _warn('Screen share error:', reason);
                    _emit({ type: 'screen_error', reason, error: err });
                }
            }
        } else {
            // Tắt chia sẻ màn hình qua nút bấm UI
            _isScreenOn = false;
            for (const [peerId, pc] of _peers) {
                if (pc.signalingState === 'closed') continue;
                const sender = _screenSenders.get(peerId);
                if (sender) {
                    try {
                        await sender.replaceTrack(null);
                        _log(`Silenced screen sender for peer ${peerId}`);
                    } catch (err) {
                        _warn(`replaceTrack(null) failed for peer ${peerId}:`, err.message);
                    }
                }
            }

            if (_localScreenStream) {
                const track = _localScreenStream.getVideoTracks()[0];
                if (track) track.onended = null;
                _localScreenStream.getTracks().forEach(t => t.stop());
                _localScreenStream = null;
            }

            _broadcastTrackState();
            _emit({ type: 'screen_toggled', enabled: false, stream: null });
        }
        return _isScreenOn;
    }

    function leaveCall() {
        if (_localUserId) {
            _send({ type: 'webrtc_leave', user_id: _localUserId });
        }

        for (const peerId of [..._peers.keys()]) {
            _closePeer(peerId);
        }

        if (_localStream) {
            _localStream.getTracks().forEach(t => t.stop());
            _localStream = null;
        }
        if (_localVideoStream) {
            _localVideoStream.getTracks().forEach(t => t.stop());
            _localVideoStream = null;
        }
        if (_localScreenStream) {
            _localScreenStream.getTracks().forEach(t => t.stop());
            _localScreenStream = null;
        }

        _isInCall = false;
        _isMicOn = false;
        _isCamOn = false;
        _isScreenOn = false;
        _localUserId = null;

        _peerCameraStreams.clear();
        _peerScreenStreams.clear();
        _peerTrackState.clear();
        _peerTrackMeta.clear();

        _emit({ type: 'call_left' });
        _log('Call left and resources released');
    }

    function _getMediaError(err, type = 'thiết bị') {
        const name = err.name || '';
        const msg = err.message || '';
        if (name === 'InsecureContext') return 'Yêu cầu HTTPS hoặc localhost để truy cập ' + type;
        if (name === 'NotAllowedError' || name === 'PermissionDeniedError') return 'Người dùng từ chối cấp quyền ' + type;
        if (name === 'NotFoundError' || name === 'DevicesNotFoundError') return 'Không tìm thấy ' + type + ' phù hợp';
        if (name === 'NotReadableError' || name === 'TrackStartError') return type + ' đang bị ứng dụng khác chiếm dụng';
        if (name === 'OverconstrainedError') return type + ' không đáp ứng thông số kỹ thuật yêu cầu';
        return `Lỗi ${type}: ${msg || name}`;
    }

    return {
        init,
        setWebSocket,
        joinCall,
        createOffer: _initiateOffer,
        handleOffer,
        handleAnswer,
        handleIceCandidate,
        handlePeerLeft,
        handleTrackState,
        handleTrackMeta,
        toggleMic,
        toggleCamera,
        toggleScreen,
        leaveCall,
        unlockAudio,
        hasPeer: _hasPeer,
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
