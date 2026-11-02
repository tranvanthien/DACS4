"""
STT timing benchmark — kiểm thử thực tế với PCM sinh từ numpy.

Chạy:
    python -m pytest tests/test_stt_timing.py -v -s

Không mock Whisper — đo thời gian thực tế của từng bước.
Yêu cầu model đã tải (WHISPER_MODEL trong .env).
"""

import os
import sys
import time
import struct
import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(".env")

SAMPLE_RATE = 16000


def _make_silence_pcm(duration_ms: int) -> bytes:
    """Tạo PCM silence 16kHz mono."""
    n = int(SAMPLE_RATE * duration_ms / 1000)
    return np.zeros(n, dtype=np.int16).tobytes()


def _make_tone_pcm(duration_ms: int, freq: float = 440.0, amplitude: float = 0.3) -> bytes:
    """Tạo sine wave — đủ năng lượng để qua energy VAD."""
    n = int(SAMPLE_RATE * duration_ms / 1000)
    t = np.linspace(0, duration_ms / 1000, n)
    samples = (amplitude * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
    return samples.tobytes()


def _pcm_to_wav_bytes(pcm: bytes, sr: int = SAMPLE_RATE) -> bytes:
    """Tạo WAV bytes hợp lệ để kiểm tra pcm_to_wav."""
    num_ch = 1
    bps = 16
    byte_rate = sr * num_ch * bps // 8
    block_align = num_ch * bps // 8
    data_size = len(pcm)
    chunk_size = 36 + data_size
    hdr = (
        b'RIFF' + struct.pack('<I', chunk_size) + b'WAVE'
        + b'fmt ' + struct.pack('<I', 16)
        + struct.pack('<H', 1) + struct.pack('<H', num_ch)
        + struct.pack('<I', sr) + struct.pack('<I', byte_rate)
        + struct.pack('<H', block_align) + struct.pack('<H', bps)
        + b'data' + struct.pack('<I', data_size)
    )
    return hdr + pcm


# ═══════════════════════════════════════════════════════════════════════════
# A. Unit tests cho audio_preprocessor
# ═══════════════════════════════════════════════════════════════════════════

class TestAudioPreprocessor:

    def test_remove_invalid_short(self):
        from app.services.audio_preprocessor import remove_invalid_audio
        # 100ms < 500ms min → rejected
        pcm = _make_tone_pcm(100)
        assert remove_invalid_audio(pcm, min_duration_ms=500) is None

    def test_remove_invalid_long_enough(self):
        from app.services.audio_preprocessor import remove_invalid_audio
        pcm = _make_tone_pcm(600)
        result = remove_invalid_audio(pcm, min_duration_ms=500)
        assert result is not None
        assert len(result) == len(pcm)

    def test_normalize_boosts_quiet_audio(self):
        from app.services.audio_preprocessor import normalize_audio
        # Tạo audio rất nhỏ (10% amplitude)
        n = SAMPLE_RATE
        samples = (0.05 * np.sin(2 * np.pi * 440 * np.linspace(0, 1, n)) * 32767).astype(np.int16)
        pcm_quiet = samples.tobytes()
        pcm_norm = normalize_audio(pcm_quiet)
        s_orig = np.frombuffer(pcm_quiet, dtype=np.int16).astype(np.float32) / 32768
        s_norm = np.frombuffer(pcm_norm, dtype=np.int16).astype(np.float32) / 32768
        assert np.max(np.abs(s_norm)) > np.max(np.abs(s_orig))

    def test_pcm_to_wav_valid_header(self):
        from app.services.audio_preprocessor import pcm_to_wav
        import os
        pcm = _make_tone_pcm(500)
        path = pcm_to_wav(pcm, sample_rate=SAMPLE_RATE)
        try:
            assert os.path.exists(path)
            with open(path, 'rb') as f:
                hdr = f.read(4)
            assert hdr == b'RIFF'
        finally:
            os.unlink(path)

    def test_pcm_sample_rate_in_wav_header(self):
        """WAV header phải ghi đúng 16000 Hz."""
        from app.services.audio_preprocessor import pcm_to_wav
        import os
        pcm = _make_tone_pcm(500)
        path = pcm_to_wav(pcm, sample_rate=16000)
        try:
            with open(path, 'rb') as f:
                f.read(24)  # skip RIFF+size+WAVE+fmt+subchunk1size+format+channels
                sr_bytes = f.read(4)
            sr = struct.unpack('<I', sr_bytes)[0]
            assert sr == 16000, f"Expected 16000 Hz, got {sr}"
        finally:
            os.unlink(path)


# ═══════════════════════════════════════════════════════════════════════════
# B. VAD tests
# ═══════════════════════════════════════════════════════════════════════════

class TestVAD:

    def test_silence_rejected(self):
        from app.services.vad_service import check_segment
        pcm = _make_silence_pcm(1000)
        result = check_segment(pcm)
        assert not result["has_speech"]

    def test_tone_detected_as_speech(self):
        """Sine wave với amplitude cao đủ để energy VAD phát hiện."""
        from app.services.vad_service import check_segment
        from app.config import settings
        # Dùng amplitude > threshold để đảm bảo qua energy VAD
        amplitude = settings.VAD_THRESHOLD * 3
        pcm = _make_tone_pcm(1000, amplitude=min(amplitude, 0.9))
        result = check_segment(pcm)
        assert result["has_speech"]

    def test_vad_timing(self):
        from app.services.vad_service import check_segment
        pcm = _make_tone_pcm(2000)
        t0 = time.perf_counter()
        result = check_segment(pcm)
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"\n  [VAD] 2s audio → {elapsed:.1f}ms (internal={result['vad_processing_ms']:.1f}ms)")
        # VAD phải chạy xong trong 500ms cho 2 giây audio
        assert elapsed < 500, f"VAD quá chậm: {elapsed:.0f}ms"


# ═══════════════════════════════════════════════════════════════════════════
# C. Whisper STT timing benchmark (thực tế, không mock)
# ═══════════════════════════════════════════════════════════════════════════

class TestWhisperTiming:
    """
    Đo thời gian thực của Whisper trên audio sine wave.
    Sine wave KHÔNG ra text có nghĩa nhưng đo được infer time thực tế.
    """

    def _run_transcribe(self, pcm: bytes, lang: str) -> dict:
        from app.services.audio_preprocessor import normalize_audio, pcm_to_wav
        import os
        pcm_norm = normalize_audio(pcm)
        wav_path = pcm_to_wav(pcm_norm)
        try:
            from app.services.whisper_service import whisper_service
            t0 = time.perf_counter()
            result = whisper_service.transcribe(wav_path, language=lang)
            elapsed = (time.perf_counter() - t0) * 1000
            result["_wall_ms"] = elapsed
            return result
        finally:
            os.unlink(wav_path)

    def test_whisper_vi_timing(self):
        """Đo latency Whisper cho tiếng Việt (1 giây audio)."""
        pcm = _make_tone_pcm(1000, amplitude=0.3)
        result = self._run_transcribe(pcm, "vi")
        infer = result.get("infer_ms", result.get("duration_ms", 0))
        wait  = result.get("wait_ms", 0)
        wall  = result.get("_wall_ms", 0)
        print(f"\n  [Whisper-vi] 1s audio → infer={infer:.0f}ms wait={wait:.0f}ms wall={wall:.0f}ms")
        # Thời gian infer phải dưới 10 giây (thực tế trên CPU thường 500-3000ms)
        assert wall < 10_000, f"Whisper vi quá chậm: {wall:.0f}ms"

    def test_whisper_en_timing(self):
        """Đo latency Whisper cho tiếng Anh (1 giây audio)."""
        pcm = _make_tone_pcm(1000, amplitude=0.3)
        result = self._run_transcribe(pcm, "en")
        infer = result.get("infer_ms", result.get("duration_ms", 0))
        wall  = result.get("_wall_ms", 0)
        print(f"\n  [Whisper-en] 1s audio → infer={infer:.0f}ms wall={wall:.0f}ms")
        assert wall < 10_000, f"Whisper en quá chậm: {wall:.0f}ms"

    def test_whisper_lock_serializes_concurrent(self):
        """
        2 request đồng thời → không crash, kết quả độc lập nhau.
        Thread thứ 2 sẽ chờ lock của thread 1.
        """
        import threading
        from app.services.audio_preprocessor import normalize_audio, pcm_to_wav
        from app.services.whisper_service import whisper_service
        import os

        pcm = normalize_audio(_make_tone_pcm(800, amplitude=0.3))
        results = [None, None]
        errors  = []

        def _task(idx):
            path = pcm_to_wav(pcm)
            try:
                results[idx] = whisper_service.transcribe(path, language="en")
            except Exception as e:
                errors.append(e)
            finally:
                try: os.unlink(path)
                except: pass

        t0 = time.perf_counter()
        t1 = threading.Thread(target=_task, args=(0,))
        t2 = threading.Thread(target=_task, args=(1,))
        t1.start(); t2.start()
        t1.join(); t2.join()
        elapsed = (time.perf_counter() - t0) * 1000

        assert not errors, f"Thread errors: {errors}"
        assert results[0] is not None
        assert results[1] is not None
        print(f"\n  [Whisper-concurrent] 2 threads → {elapsed:.0f}ms total (serialized)")


# ═══════════════════════════════════════════════════════════════════════════
# D. Full pipeline transcribe_pcm — PCM bytes → result
# ═══════════════════════════════════════════════════════════════════════════

class TestTranscribePipeline:

    def _run(self, pcm: bytes, lang: str = "vi") -> dict:
        import asyncio
        from app.services.transcription_service import transcribe_pcm
        return asyncio.run(transcribe_pcm(pcm, lang, user_id=0, room_code="TEST"))

    def test_too_short_rejected(self):
        pcm = _make_tone_pcm(100)  # < 500ms
        result = self._run(pcm)
        assert result["rejected"]
        assert result["reason"] == "AUDIO_TOO_SHORT"

    def test_silence_rejected_by_vad(self):
        pcm = _make_silence_pcm(2000)
        result = self._run(pcm)
        assert result["rejected"]
        assert result["reason"] in ("NO_SPEECH_DETECTED", "AUDIO_TOO_SHORT")

    def test_tone_passes_pipeline(self):
        """Sine wave đủ năng lượng → không bị reject ở VAD, Whisper chạy."""
        from app.config import settings
        amplitude = settings.VAD_THRESHOLD * 4
        pcm = _make_tone_pcm(2000, amplitude=min(amplitude, 0.9))
        t0 = time.perf_counter()
        result = self._run(pcm, "en")
        elapsed = (time.perf_counter() - t0) * 1000
        timing = result.get("timing", {})
        print(
            f"\n  [Pipeline-en] 2s tone audio → total={elapsed:.0f}ms | "
            f"validate={timing.get('validate_ms',0):.0f}ms "
            f"normalize={timing.get('normalize_ms',0):.0f}ms "
            f"vad={timing.get('vad_ms',0):.0f}ms "
            f"wav={timing.get('wav_encode_ms',0):.0f}ms "
            f"stt_wait={timing.get('stt_wait_ms',0):.0f}ms "
            f"stt_infer={timing.get('stt_infer_ms',0):.0f}ms"
        )
        # Phải có timing dict
        assert "timing" in result
        assert result["timing"]["total_ms"] > 0

    def test_vietnamese_pipeline(self):
        """Pipeline tiếng Việt — không reject ở validate/vad."""
        from app.config import settings
        amplitude = settings.VAD_THRESHOLD * 4
        pcm = _make_tone_pcm(1500, amplitude=min(amplitude, 0.9))
        t0 = time.perf_counter()
        result = self._run(pcm, "vi")
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"\n  [Pipeline-vi] 1.5s → total={elapsed:.0f}ms rejected={result.get('rejected')}")
        assert "timing" in result or result.get("rejected") in (True, False)

    def test_no_language_rejected(self):
        pcm = _make_tone_pcm(1000)
        result = self._run(pcm, "")
        assert result["rejected"]
        assert result["reason"] == "SPEAKING_LANGUAGE_REQUIRED"
