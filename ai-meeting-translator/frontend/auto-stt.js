/**
 * AutoSTT — Module 09.5
 * Tự động phát hiện giọng nói từ mic WebRTC và gửi STT pipeline.
 *
 * Luồng: MediaStream → AudioContext → ScriptProcessor → PCM buffer
 *        → Energy VAD → detect speech end → send WebSocket audio_start+PCM+audio_stop
 *
 * Anti-echo: chỉ xử lý mic của chính user, KHÔNG lấy audio từ remote peers.
 * Anti-loop: khi TTS đang phát, tạm ngừng ghi âm STT.
 */

var AutoSTT = (function () {
    var _ws = null;
    var _speakLang = 'vi';
    var _isRunning = false;
    var _onStatusChange = null;

    // AudioContext pipeline
    var _audioCtx = null;
    var _sourceNode = null;
    var _processorNode = null;
    var _analyserNode = null;

    // PCM buffer
    var _pcmBuffer = [];          // Float32Array chunks
    var _pcmByteBuffer = [];      // Int16 byte chunks (for sending)
    var _sampleRate = 16000;

    // VAD state
    var _isSpeaking = false;
    var _silenceFrames = 0;
    var _speechFrames = 0;

    // Tuning params
    var ENERGY_THRESHOLD = 0.008;  // RMS threshold cho speech detection
    // 15 frames × (4096/16000)s ≈ 3.84s → giảm từ 30 xuống 15 để kết thúc câu nhanh hơn
    // nhưng vẫn đủ để không cắt mất từ cuối (người nói thường dừng ~300-500ms giữa các từ)
    var SILENCE_FRAMES_END = 15;
    var MIN_SPEECH_FRAMES = 5;     // ~1.28s minimum (tăng từ 10 để lọc noise ngắn)
    var MAX_SPEECH_SECONDS = 30;    // tối đa 30 giây mỗi đoạn
    var FRAME_SIZE = 4096;

    // TTS playing flag — khi TTS đang phát thì không ghi STT (chống echo)
    var _ttsPlaying = false;
    var _ttsPlayingTimer = null;
    var _maxSpeechFrames = 0;

    function _log(msg) { console.log('[AutoSTT]', msg); }

    function _setStatus(status) {
        if (typeof _onStatusChange === 'function') _onStatusChange(status);
    }

    function _rms(float32Array) {
        var sum = 0;
        for (var i = 0; i < float32Array.length; i++) sum += float32Array[i] * float32Array[i];
        return Math.sqrt(sum / float32Array.length);
    }

    function _float32ToInt16(float32Array) {
        var int16 = new Int16Array(float32Array.length);
        for (var i = 0; i < float32Array.length; i++) {
            var s = Math.max(-1, Math.min(1, float32Array[i]));
            int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }
        return int16;
    }

    function _sendSTT() {
        if (_pcmByteBuffer.length === 0) return;

        // Gộp tất cả chunk thành 1 buffer
        var totalLen = 0;
        for (var i = 0; i < _pcmByteBuffer.length; i++) totalLen += _pcmByteBuffer[i].byteLength;
        var merged = new Uint8Array(totalLen);
        var offset = 0;
        for (var i = 0; i < _pcmByteBuffer.length; i++) {
            merged.set(new Uint8Array(_pcmByteBuffer[i].buffer), offset);
            offset += _pcmByteBuffer[i].byteLength;
        }

        if (!_ws || _ws.readyState !== WebSocket.OPEN) {
            _log('WS not open, skipping STT');
            return;
        }

        _log('Sending STT: ' + (merged.length / 2 / _sampleRate * 1000).toFixed(0) + 'ms audio');
        _setStatus('nhan-dien');

        // Giao thức hiện có: audio_start (JSON) + binary PCM + audio_stop (JSON)
        _ws.send(JSON.stringify({
            type: 'audio_start',
            mime_type: 'audio/pcm',
            sample_rate: _sampleRate,
            speaking_language: _speakLang,
        }));
        _ws.send(merged.buffer);
        _ws.send(JSON.stringify({ type: 'audio_stop' }));
    }

    function _resetBuffer() {
        _pcmByteBuffer = [];
        _speechFrames = 0;
        _silenceFrames = 0;
        _maxSpeechFrames = Math.round(MAX_SPEECH_SECONDS * (_sampleRate / FRAME_SIZE));
    }

    function _resample(float32Array, srcRate, dstRate) {
        if (srcRate === dstRate) return float32Array;
        var ratio = srcRate / dstRate;
        var outLen = Math.round(float32Array.length / ratio);
        var out = new Float32Array(outLen);
        for (var i = 0; i < outLen; i++) {
            var pos = i * ratio;
            var idx = Math.floor(pos);
            var frac = pos - idx;
            var a = float32Array[idx] || 0;
            var b = float32Array[idx + 1] || 0;
            out[i] = a + frac * (b - a);
        }
        return out;
    }

    function _processAudio(inputBuffer, nativeSampleRate) {
        if (!_isRunning || _ttsPlaying) return;

        var samples = inputBuffer.getChannelData(0);

        // Resample từ native rate (48kHz) xuống 16kHz trước khi tính energy và gửi
        var resampled = _resample(samples, nativeSampleRate || _audioCtx.sampleRate, _sampleRate);
        var energy = _rms(resampled);
        var int16Chunk = _float32ToInt16(resampled);

        if (energy > ENERGY_THRESHOLD) {
            // Có tiếng nói
            _silenceFrames = 0;
            _speechFrames++;
            _pcmByteBuffer.push(int16Chunk);

            if (!_isSpeaking) {
                _isSpeaking = true;
                _log('Speech detected (energy=' + energy.toFixed(4) + ')');
                _setStatus('nghe');
            }

            // Giới hạn độ dài tối đa
            if (_speechFrames >= _maxSpeechFrames) {
                _log('Max speech length reached, sending');
                var toSend = _pcmByteBuffer.slice();
                _resetBuffer();
                _isSpeaking = false;
                _pcmByteBuffer = toSend;
                _sendSTT();
                _resetBuffer();
            }
        } else {
            // Silence
            if (_isSpeaking) {
                _pcmByteBuffer.push(int16Chunk); // thêm trailing silence
                _silenceFrames++;

                if (_silenceFrames >= SILENCE_FRAMES_END) {
                    // Đủ silence → kết thúc đoạn nói
                    if (_speechFrames >= MIN_SPEECH_FRAMES) {
                        _log('Speech ended after ' + _speechFrames + ' frames');
                        _sendSTT();
                    } else {
                        _log('Too short, discard');
                    }
                    _resetBuffer();
                    _isSpeaking = false;
                    _setStatus('cho');
                }
            }
        }
    }

    // ── Public API ─────────────────────────────────────────────────────────

    function start(mediaStream, websocket, speakingLanguage) {
        if (_isRunning) { _log('Already running'); return; }

        // Kiểm tra track trước khi bắt đầu
        var tracks = mediaStream ? mediaStream.getAudioTracks() : [];
        if (tracks.length === 0) {
            _log('ERROR: mediaStream has no audio tracks');
            return;
        }
        var track = tracks[0];
        var settings = track.getSettings ? track.getSettings() : {};
        _log('Track state: readyState=' + track.readyState
            + ' enabled=' + track.enabled
            + ' muted=' + track.muted
            + ' sampleRate=' + (settings.sampleRate || 'unknown'));

        if (track.readyState !== 'live') {
            _log('ERROR: audio track is not live (state=' + track.readyState + ')');
            return;
        }

        _ws = websocket;
        _speakLang = speakingLanguage || 'vi';
        _isRunning = true;
        _resetBuffer();

        // Tạo AudioContext với sample rate của browser (thường 48000Hz với WebRTC).
        // KHÔNG ép 16000 vì createMediaStreamSource() sẽ fail khi mismatch.
        // ScriptProcessor sẽ nhận audio ở native rate, rồi resample xuống 16kHz trước khi gửi.
        try {
            _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        } catch (e) {
            _log('ERROR: Cannot create AudioContext: ' + e.message);
            _isRunning = false;
            return;
        }

        var nativeSampleRate = _audioCtx.sampleRate;
        _sampleRate = 16000; // sample rate gửi lên backend (sau resample)
        _log('AudioContext native sampleRate=' + nativeSampleRate + ', target=' + _sampleRate);

        _sourceNode = _audioCtx.createMediaStreamSource(mediaStream);

        // ScriptProcessorNode — nhận PCM ở native rate
        _processorNode = _audioCtx.createScriptProcessor(FRAME_SIZE, 1, 1);
        _processorNode.onaudioprocess = function (e) {
            _processAudio(e.inputBuffer, nativeSampleRate);
        };

        // source → processor → destination (phải connect destination để browser không garbage collect)
        _sourceNode.connect(_processorNode);
        _processorNode.connect(_audioCtx.destination);

        _log('Started. lang=' + _speakLang + ' native_sr=' + nativeSampleRate + ' target_sr=' + _sampleRate);
        _setStatus('cho');
    }

    function stop() {
        if (!_isRunning) return;
        _isRunning = false;
        _ttsPlaying = false; // reset ngay khi stop — không để lock
        if (_ttsPlayingTimer) { clearTimeout(_ttsPlayingTimer); _ttsPlayingTimer = null; }

        // Gửi nốt nếu đang nói dở
        if (_isSpeaking && _speechFrames >= MIN_SPEECH_FRAMES) {
            _sendSTT();
        }
        _resetBuffer();
        _isSpeaking = false;

        if (_processorNode) { _processorNode.disconnect(); _processorNode = null; }
        if (_sourceNode) { _sourceNode.disconnect(); _sourceNode = null; }
        if (_audioCtx) { _audioCtx.close(); _audioCtx = null; }

        _log('Stopped');
        _setStatus('off');
    }

    function setTTSPlaying(playing) {
        _ttsPlaying = playing;
        // Timeout fallback: nếu TTS playing không được reset sau 30s → tự reset
        // Tránh trường hợp audio error không fire, STT bị lock vĩnh viễn
        if (playing) {
            _log('TTS playing — STT paused');
            if (_ttsPlayingTimer) clearTimeout(_ttsPlayingTimer);
            _ttsPlayingTimer = setTimeout(function () {
                if (_ttsPlaying) {
                    _log('TTS playing timeout — force reset');
                    _ttsPlaying = false;
                    _ttsPlayingTimer = null;
                }
            }, 30000);
        } else {
            _log('TTS done — STT resumed');
            if (_ttsPlayingTimer) { clearTimeout(_ttsPlayingTimer); _ttsPlayingTimer = null; }
        }
    }

    function updateLanguage(lang) {
        _speakLang = lang;
    }

    function isRunning() { return _isRunning; }

    return {
        start: start,
        stop: stop,
        setTTSPlaying: setTTSPlaying,
        updateLanguage: updateLanguage,
        isRunning: isRunning,
        set onStatusChange(fn) { _onStatusChange = fn; },
    };
})();
