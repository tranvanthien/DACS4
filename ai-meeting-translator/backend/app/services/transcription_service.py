"""
STT pipeline: audio bytes (PCM hoặc WebM) → WAV → VAD → Whisper → result
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.services.audio_preprocessor import pcm_to_wav, normalize_audio, remove_invalid_audio
from app.services.vad_service import check_segment
from app.services.whisper_service import whisper_service
from app.services.ai_monitor_service import ai_monitor

# max_workers=4: cho phép decode/VAD song song; STT serialize qua lock trong WhisperService
_executor = ThreadPoolExecutor(max_workers=4)


def _find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for path in [r"D:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\bin\ffmpeg.exe"]:
        if os.path.isfile(path):
            return path
    return "ffmpeg"

FFMPEG = _find_ffmpeg()


def _webm_to_wav(audio_bytes: bytes, mime_type: str) -> str | None:
    """Convert WebM/OGG → WAV 16kHz mono dùng ffmpeg. Trả path file tạm."""
    ext = ".ogg" if "ogg" in mime_type else ".webm"
    tmp_in  = tempfile.NamedTemporaryFile(suffix=ext,   delete=False)
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_in.close(); tmp_wav.close()
    try:
        with open(tmp_in.name, "wb") as f:
            f.write(audio_bytes)
        result = subprocess.run(
            [FFMPEG, "-y", "-i", tmp_in.name,
             "-ar", "16000", "-ac", "1", "-f", "wav", tmp_wav.name],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"[ffmpeg] Error: {result.stderr.decode(errors='ignore')[:200]}")
            os.unlink(tmp_wav.name)
            return None
        return tmp_wav.name
    finally:
        try: os.unlink(tmp_in.name)
        except: pass


def _run_stt(audio_bytes: bytes, language: str, user_id: int,
             room_code: str, mime_type: str = "audio/pcm") -> dict:
    t_total = time.time()

    is_pcm = "pcm" in mime_type or mime_type == ""

    if is_pcm:
        # ── Bước 1: Validate độ dài ───────────────────────────────────────
        t_step = time.time()
        valid = remove_invalid_audio(audio_bytes, min_duration_ms=settings.VAD_MIN_SPEECH_MS)
        validate_ms = (time.time() - t_step) * 1000
        if valid is None:
            ai_monitor.record_stt_result(duration_ms=validate_ms, rejected=True, reason="AUDIO_TOO_SHORT")
            return {"rejected": True, "reason": "AUDIO_TOO_SHORT", "text": ""}

        # ── Bước 2: Normalize ─────────────────────────────────────────────
        t_step = time.time()
        pcm_norm = normalize_audio(valid)
        normalize_ms = (time.time() - t_step) * 1000

        # Kiểm tra biên độ audio để phát hiện mic quá yếu
        import numpy as np
        samples = np.frombuffer(pcm_norm, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples ** 2)))
        peak = float(np.max(np.abs(samples)))

        # ── Bước 3: VAD ───────────────────────────────────────────────────
        t_step = time.time()
        vad_result = check_segment(pcm_norm)
        vad_ms = (time.time() - t_step) * 1000

        if not vad_result["has_speech"]:
            # Double-check bằng energy VAD trước khi reject hoàn toàn
            # Silero có thể false-negative với audio WebRTC do noiseSuppression
            energy_rms = rms  # đã tính ở trên
            energy_threshold = 0.005  # ngưỡng rất thấp, chỉ lọc silence thực sự
            if energy_rms > energy_threshold:
                print(
                    f"[STT] VAD Silero rejected but energy ok (rms={rms:.4f}>{energy_threshold}), "
                    f"proceeding with Whisper"
                )
                vad_result["has_speech"] = True  # override Silero với energy check
            else:
                num_samples = len(pcm_norm) // 2
                duration_ms = (num_samples / 16000) * 1000
                print(
                    f"[STT] REJECTED user={user_id} reason=NO_SPEECH "
                    f"duration={duration_ms:.0f}ms rms={rms:.4f} peak={peak:.4f} "
                    f"vad_conf={vad_result['confidence']:.3f} vad_ms={vad_ms:.0f}ms"
                )
                total_rejected_ms = (time.time() - t_total) * 1000
                ai_monitor.record_stt_result(duration_ms=total_rejected_ms, rejected=True, reason="NO_SPEECH_DETECTED")
                return {"rejected": True, "reason": "NO_SPEECH_DETECTED", "text": "", "vad": vad_result}

        audio_duration_ms = vad_result["duration_ms"]

        # ── Bước 4: PCM → WAV ─────────────────────────────────────────────
        t_step = time.time()
        wav_path = pcm_to_wav(pcm_norm)
        wav_ms = (time.time() - t_step) * 1000

    else:
        # WebM/OGG path: ffmpeg → WAV → Whisper (bỏ qua VAD vì format không phải PCM)
        if len(audio_bytes) < 1000:
            total_rejected_ms = (time.time() - t_total) * 1000
            ai_monitor.record_stt_result(duration_ms=total_rejected_ms, rejected=True, reason="AUDIO_TOO_SHORT")
            return {"rejected": True, "reason": "AUDIO_TOO_SHORT", "text": ""}

        t_step = time.time()
        wav_path = _webm_to_wav(audio_bytes, mime_type)
        wav_ms = (time.time() - t_step) * 1000
        if not wav_path:
            total_rejected_ms = (time.time() - t_total) * 1000
            ai_monitor.record_stt_result(duration_ms=total_rejected_ms, rejected=True, reason="AUDIO_CONVERSION_FAILED")
            return {"rejected": True, "reason": "AUDIO_CONVERSION_FAILED", "text": ""}

        audio_duration_ms = len(audio_bytes) / 6000 * 1000  # ước tính
        vad_ms = 0
        validate_ms = 0
        normalize_ms = 0
        vad_result = {"has_speech": True, "confidence": 1.0,
                      "duration_ms": audio_duration_ms, "vad_processing_ms": 0}

    try:
        # ── Bước 5: Whisper STT ───────────────────────────────────────────
        t_step = time.time()
        result = whisper_service.transcribe(wav_path, language=language)
        stt_ms = (time.time() - t_step) * 1000
    except Exception as e:
        total_ms = (time.time() - t_total) * 1000
        ai_monitor.record_stt_result(duration_ms=total_ms, error=str(e))
        raise e
    finally:
        try: os.unlink(wav_path)
        except: pass

    total_ms = (time.time() - t_total) * 1000

    # Ghi nhận metric thực tế
    if result.get("rejected"):
        reason = result.get("reason", "REJECTED")
        ai_monitor.record_stt_result(
            duration_ms=total_ms,
            infer_ms=result.get("infer_ms"),
            wait_ms=result.get("wait_ms"),
            rejected=True,
            reason=reason,
            error="WHISPER_ERROR" if reason == "WHISPER_ERROR" else None,
        )
    else:
        ai_monitor.record_stt_result(
            duration_ms=total_ms,
            infer_ms=result.get("infer_ms"),
            wait_ms=result.get("wait_ms"),
            rejected=False,
        )

    # Log timing chi tiết từng bước
    print(
        f"[STT] user={user_id} room={room_code} lang={language} mime={mime_type} | "
        f"audio={audio_duration_ms:.0f}ms "
        f"validate={validate_ms:.0f}ms normalize={normalize_ms:.0f}ms "
        f"vad={vad_ms:.0f}ms wav={wav_ms:.0f}ms "
        f"stt={stt_ms:.0f}ms (wait={result.get('wait_ms',0):.0f}ms infer={result.get('infer_ms',0):.0f}ms) "
        f"total={total_ms:.0f}ms | "
        f"conf={result.get('confidence',0):.3f} text=\"{result.get('text','')}\" "
    )

    result["audio_duration_ms"] = audio_duration_ms
    result["total_latency_ms"]  = total_ms
    result["vad"]               = vad_result
    result["timing"] = {
        "validate_ms": validate_ms,
        "normalize_ms": normalize_ms,
        "vad_ms": vad_ms,
        "wav_encode_ms": wav_ms,
        "stt_total_ms": stt_ms,
        "stt_wait_ms": result.get("wait_ms", 0),
        "stt_infer_ms": result.get("infer_ms", 0),
        "total_ms": total_ms,
    }
    return result


async def transcribe_pcm(pcm_bytes: bytes, language: str,
                          user_id: int = 0, room_code: str = "",
                          mime_type: str = "audio/pcm") -> dict:
    """Async wrapper – xử lý cả PCM lẫn WebM."""
    if not language:
        return {"rejected": True, "reason": "SPEAKING_LANGUAGE_REQUIRED", "text": ""}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor, _run_stt, pcm_bytes, language, user_id, room_code, mime_type
    )
