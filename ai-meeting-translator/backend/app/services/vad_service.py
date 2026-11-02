"""
Silero VAD wrapper.
Chạy trên PCM 16-bit / 16kHz / mono.
Nếu Silero chưa cài, fallback sang energy-based VAD.
"""

import threading
import numpy as np
import time
from app.config import settings

# Thử load Silero VAD
_silero_model = None
_silero_utils = None
_silero_lock = threading.Lock()  # Silero model không thread-safe


def _load_silero():
    global _silero_model, _silero_utils
    if _silero_model is not None:
        return True
    try:
        import torch
        model, utils = torch.hub.load(
            repo_or_dir='snakers4/silero-vad',
            model='silero_vad',
            force_reload=False,
            trust_repo=True,
        )
        _silero_model = model
        _silero_utils = utils
        print("[VAD] Silero VAD loaded.")
        return True
    except Exception as e:
        print(f"[VAD] Silero not available ({e}), using energy-based fallback.")
        return False


def _energy_vad(pcm_bytes: bytes, threshold: float = None) -> bool:
    """Trả True nếu audio có năng lượng đủ lớn (có giọng nói)."""
    if threshold is None:
        threshold = settings.VAD_THRESHOLD
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    rms = np.sqrt(np.mean(samples ** 2))
    return float(rms) > threshold


def has_speech(pcm_bytes: bytes, sample_rate: int = 16000) -> tuple[bool, float]:
    """
    Trả (has_speech, confidence).
    Dùng Silero nếu có, ngược lại dùng energy VAD.
    """
    if len(pcm_bytes) < 512:
        return False, 0.0

    # Thử Silero
    silero_ok = _load_silero()
    if silero_ok and _silero_model is not None:
        try:
            import torch
            samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

            # Dùng chunk_size=512 — Silero chỉ chấp nhận đúng 512 samples @ 16kHz
            chunk_size = 512
            confidences = []

            with _silero_lock:
                _silero_model.reset_states()
                for i in range(0, len(samples) - chunk_size + 1, chunk_size):
                    chunk = samples[i:i + chunk_size]
                    tensor = torch.from_numpy(chunk).unsqueeze(0)
                    conf = _silero_model(tensor, sample_rate).item()
                    confidences.append(conf)
                    # Early exit khi detect speech rõ ràng
                    if conf >= 0.7:
                        break

            if not confidences:
                raise ValueError("No chunks")

            max_conf = max(confidences)
            avg_conf = sum(confidences) / len(confidences)

            if max_conf >= settings.VAD_THRESHOLD:
                print(f"[VAD] Silero speech detected: max={max_conf:.3f} avg={avg_conf:.3f} "
                      f"chunks={len(confidences)} threshold={settings.VAD_THRESHOLD}")
            else:
                print(f"[VAD] Silero no speech: max={max_conf:.3f} avg={avg_conf:.3f} "
                      f"chunks={len(confidences)} threshold={settings.VAD_THRESHOLD}")

            return max_conf >= settings.VAD_THRESHOLD, max_conf

        except Exception as e:
            print(f"[VAD] Silero error: {e}, fallback to energy")

    # Energy fallback
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(samples ** 2)))
    detected = rms > settings.VAD_THRESHOLD
    print(f"[VAD] Energy fallback: rms={rms:.4f} threshold={settings.VAD_THRESHOLD} detected={detected}")
    return detected, rms


def check_segment(pcm_bytes: bytes, sample_rate: int = 16000) -> dict:
    """
    Kiểm tra toàn bộ segment.
    Trả dict với: has_speech, confidence, duration_ms
    """
    t0 = time.time()
    detected, conf = has_speech(pcm_bytes, sample_rate)
    duration_ms = (len(pcm_bytes) / 2 / sample_rate) * 1000
    elapsed_ms = (time.time() - t0) * 1000
    return {
        "has_speech": detected,
        "confidence": conf,
        "duration_ms": duration_ms,
        "vad_processing_ms": elapsed_ms,
    }
