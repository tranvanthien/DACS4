import sys
import threading
import time

from faster_whisper import WhisperModel

from app.config import settings

# Đảm bảo stdout hỗ trợ UTF-8 an toàn trên Windows
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class WhisperService:
    """
    Singleton-safe wrapper cho faster-whisper.
    Model được nạp một lần khi khởi động, tái sử dụng an toàn qua lock.
    faster-whisper không thread-safe nên serialize mọi lần transcribe.
    """

    def __init__(self):
        t_load = time.time()
        print(f"[Whisper] Loading model '{settings.WHISPER_MODEL}' "
              f"({settings.WHISPER_DEVICE}/{settings.WHISPER_COMPUTE_TYPE})...")
        self.model = WhisperModel(
            settings.WHISPER_MODEL,
            device=settings.WHISPER_DEVICE,
            compute_type=settings.WHISPER_COMPUTE_TYPE,
            # num_workers=1: tránh race condition nội bộ của CTranslate2
            num_workers=1,
        )
        self._lock = threading.Lock()
        self.load_duration_ms = (time.time() - t_load) * 1000
        print(f"[Whisper] Model loaded in {self.load_duration_ms:.0f}ms.")

    def transcribe(self, audio_path: str, language: str) -> dict:
        """
        Transcribe file WAV.
        language: 'vi' hoặc 'en' — không để auto-detect (chậm hơn và kém chính xác hơn).
        Trả: text, language, confidence, no_speech_prob, duration_ms, rejected
        """
        t0 = time.time()
        try:
            with self._lock:
                t_acquire = time.time()
                segments_gen, info = self.model.transcribe(
                    audio_path,
                    language=language,
                    # beam_size=1 nhanh hơn ~3x so với 5 trên CPU, chấp nhận được cho real-time
                    beam_size=min(settings.WHISPER_BEAM_SIZE, 2),
                    temperature=settings.WHISPER_TEMPERATURE,
                    condition_on_previous_text=False,
                    vad_filter=False,          # VAD đã xử lý trước ở transcription_service
                    no_speech_threshold=settings.WHISPER_NO_SPEECH_PROB_MAX,
                    log_prob_threshold=settings.WHISPER_AVG_LOGPROB_MIN,
                    compression_ratio_threshold=settings.WHISPER_COMPRESSION_RATIO_MAX,
                )
                # Consume generator TRONG lock để không giải phóng lock sớm
                segments = list(segments_gen)
                t_infer = time.time()

            wait_ms  = (t_acquire - t0) * 1000   # thời gian chờ lock (queue)
            infer_ms = (t_infer - t_acquire) * 1000
            total_ms = (t_infer - t0) * 1000

            if not segments:
                try:
                    print(f"[Whisper] lang={language} wait={wait_ms:.0f}ms infer={infer_ms:.0f}ms → NO_SPEECH")
                except Exception:
                    pass
                return {"text": "", "language": language, "confidence": 0.0,
                        "no_speech_prob": 1.0, "duration_ms": total_ms,
                        "infer_ms": infer_ms, "wait_ms": wait_ms,
                        "rejected": True, "reason": "NO_SPEECH_DETECTED"}

            text = " ".join(seg.text.strip() for seg in segments).strip()
            avg_logprob   = sum(seg.avg_logprob   for seg in segments) / len(segments)
            no_speech_prob = sum(seg.no_speech_prob for seg in segments) / len(segments)
            confidence     = max(0.0, min(1.0, avg_logprob + 1.0))

            try:
                print(f"[Whisper] lang={language} wait={wait_ms:.0f}ms "
                      f"infer={infer_ms:.0f}ms total={total_ms:.0f}ms "
                      f"conf={confidence:.2f} no_speech={no_speech_prob:.2f} "
                      f"text=\"{text[:60]}\"")
            except Exception:
                pass

            # Reject nếu no_speech_prob quá cao (segment chủ yếu là im lặng/noise)
            if no_speech_prob > settings.WHISPER_NO_SPEECH_PROB_MAX:
                try:
                    print(f"[Whisper] REJECTED no_speech={no_speech_prob:.2f} > {settings.WHISPER_NO_SPEECH_PROB_MAX}")
                except Exception:
                    pass
                return {"text": "", "language": language, "confidence": confidence,
                        "no_speech_prob": no_speech_prob, "duration_ms": total_ms,
                        "infer_ms": infer_ms, "wait_ms": wait_ms,
                        "rejected": True, "reason": "NO_SPEECH_HIGH_PROB"}

            # Reject nếu không có text sau khi strip
            if not text:
                try:
                    print(f"[Whisper] REJECTED empty text after strip")
                except Exception:
                    pass
                return {"text": "", "language": language, "confidence": confidence,
                        "no_speech_prob": no_speech_prob, "duration_ms": total_ms,
                        "infer_ms": infer_ms, "wait_ms": wait_ms,
                        "rejected": True, "reason": "EMPTY_TEXT"}

            return {
                "text": text,
                "language": language,
                "confidence": confidence,
                "no_speech_prob": no_speech_prob,
                "duration_ms": total_ms,
                "infer_ms": infer_ms,
                "wait_ms": wait_ms,
                "rejected": False,
            }

        except Exception as e:
            duration_ms = (time.time() - t0) * 1000
            try:
                print(f"[Whisper] Error lang={language} after {duration_ms:.0f}ms: {e}")
            except Exception:
                pass
            return {"text": "", "language": language, "confidence": 0.0,
                    "duration_ms": duration_ms, "rejected": True, "reason": "WHISPER_ERROR"}


# Nạp model một lần khi module được import lần đầu (lúc app khởi động).
# Các lần import sau dùng lại instance này — Python module cache đảm bảo điều đó.
whisper_service = WhisperService()
