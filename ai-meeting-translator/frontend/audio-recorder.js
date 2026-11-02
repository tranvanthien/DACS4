/**
 * AudioRecorder
 * - Dùng AudioWorklet để lấy PCM 16-bit / 16kHz / mono
 * - Gửi binary PCM qua WebSocket
 * - Không dùng MediaRecorder
 */
class AudioRecorder {
    constructor({ ws, onStatusChange, onChunkSent }) {
        this.ws = ws;
        this.onStatusChange = onStatusChange || (() => { });
        this.onChunkSent = onChunkSent || (() => { });

        this._context = null;
        this._worklet = null;
        this._source = null;
        this._stream = null;
        this._recording = false;
    }

    async start(speakingLanguage) {
        if (this._recording) return;
        if (!speakingLanguage) throw new Error('SPEAKING_LANGUAGE_REQUIRED');

        // Xin quyền microphone
        try {
            this._stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    channelCount: 1,
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                },
            });
        } catch {
            throw new Error('MICROPHONE_PERMISSION_DENIED');
        }

        // Tạo AudioContext
        this._context = new AudioContext();
        const srcRate = this._context.sampleRate;

        // Load AudioWorklet
        await this._context.audioWorklet.addModule('pcm-processor.js');

        this._source = this._context.createMediaStreamSource(this._stream);
        this._worklet = new AudioWorkletNode(this._context, 'pcm-processor', {
            processorOptions: { sampleRate: srcRate },
        });

        // Nhận PCM chunk từ worklet
        this._worklet.port.onmessage = (e) => {
            if (!this._recording) return;
            const pcmBuffer = e.data.pcm;
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
            if (this.ws.bufferedAmount > 128 * 1024) return; // tránh tràn buffer
            this.ws.send(pcmBuffer);
            this.onChunkSent(pcmBuffer.byteLength);
        };

        this._source.connect(this._worklet);
        this._worklet.connect(this._context.destination);

        // Báo server bắt đầu
        this.ws.send(JSON.stringify({
            type: 'audio_start',
            mime_type: 'audio/pcm',
            sample_rate: 16000,
            speaking_language: speakingLanguage,
        }));

        this._recording = true;
        this.onStatusChange('recording');
    }

    stop() {
        if (!this._recording) return;
        this._recording = false;

        // Ngắt worklet
        if (this._worklet) { this._worklet.disconnect(); this._worklet = null; }
        if (this._source) { this._source.disconnect(); this._source = null; }
        if (this._context) { this._context.close(); this._context = null; }
        if (this._stream) { this._stream.getTracks().forEach(t => t.stop()); this._stream = null; }

        // Báo server dừng
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ type: 'audio_stop' }));
        }

        this.onStatusChange('stopped');
    }

    get isRecording() { return this._recording; }
}
