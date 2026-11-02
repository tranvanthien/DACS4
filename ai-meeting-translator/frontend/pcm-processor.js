/**
 * AudioWorklet Processor
 * - Nhận Float32 từ microphone
 * - Resample về 16000 Hz nếu cần
 * - Convert sang Int16 PCM
 * - Gửi chunk về main thread mỗi ~100ms
 */
class PCMProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        this._targetSampleRate = 16000;
        this._sourceSampleRate = options.processorOptions?.sampleRate || 48000;
        this._buffer = [];
        // Số sample Float32 tích lũy trước khi gửi (~100ms ở 16kHz = 1600 samples)
        this._chunkSamples = 1600;
        this._accumulated = [];
    }

    // Resample đơn giản bằng linear interpolation
    _resample(input, srcRate, dstRate) {
        if (srcRate === dstRate) return input;
        const ratio = srcRate / dstRate;
        const outputLength = Math.round(input.length / ratio);
        const output = new Float32Array(outputLength);
        for (let i = 0; i < outputLength; i++) {
            const pos = i * ratio;
            const idx = Math.floor(pos);
            const frac = pos - idx;
            const a = input[idx] || 0;
            const b = input[idx + 1] || 0;
            output[i] = a + frac * (b - a);
        }
        return output;
    }

    // Float32 [-1,1] → Int16
    _toInt16(float32) {
        const int16 = new Int16Array(float32.length);
        for (let i = 0; i < float32.length; i++) {
            const s = Math.max(-1, Math.min(1, float32[i]));
            int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }
        return int16;
    }

    process(inputs) {
        const input = inputs[0];
        if (!input || !input[0]) return true;

        // Lấy channel đầu tiên (mono)
        const channelData = input[0];

        // Resample về 16kHz
        const resampled = this._resample(channelData, this._sourceSampleRate, this._targetSampleRate);

        // Tích lũy
        for (let i = 0; i < resampled.length; i++) {
            this._accumulated.push(resampled[i]);
        }

        // Khi đủ chunk thì gửi
        while (this._accumulated.length >= this._chunkSamples) {
            const chunk = this._accumulated.splice(0, this._chunkSamples);
            const float32 = new Float32Array(chunk);
            const int16 = this._toInt16(float32);
            // Gửi ArrayBuffer về main thread (transferable)
            this.port.postMessage({ pcm: int16.buffer }, [int16.buffer]);
        }

        return true;
    }
}

registerProcessor('pcm-processor', PCMProcessor);
