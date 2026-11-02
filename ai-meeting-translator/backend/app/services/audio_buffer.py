"""
Audio buffer theo (room_code, user_id).
Lưu PCM 16-bit raw bytes.
"""

import time

_buffers:    dict[tuple, bytearray] = {}
_meta:       dict[tuple, dict]      = {}  # mime_type, sample_rate, speaking_language
_start_time: dict[tuple, float]     = {}

SAMPLE_RATE   = 16000
BYTES_PER_SAMPLE = 2  # Int16


def _key(room_code: str, user_id: int) -> tuple:
    return (room_code, user_id)


def start(room_code: str, user_id: int, mime_type: str = "audio/pcm",
          sample_rate: int = 16000, speaking_language: str = "vi"):
    k = _key(room_code, user_id)
    _buffers[k]    = bytearray()
    _meta[k]       = {"mime_type": mime_type, "sample_rate": sample_rate,
                      "speaking_language": speaking_language}
    _start_time[k] = time.time()


def append(room_code: str, user_id: int, data: bytes):
    k = _key(room_code, user_id)
    if k not in _buffers:
        _buffers[k] = bytearray()
    _buffers[k].extend(data)


def get_audio(room_code: str, user_id: int) -> bytes:
    return bytes(_buffers.get(_key(room_code, user_id), bytearray()))


def get_duration_ms(room_code: str, user_id: int) -> float:
    k = _key(room_code, user_id)
    buf = _buffers.get(k, bytearray())
    meta = _meta.get(k, {})
    sr = meta.get("sample_rate", SAMPLE_RATE)
    samples = len(buf) / BYTES_PER_SAMPLE
    return (samples / sr) * 1000


def get_meta(room_code: str, user_id: int) -> dict:
    return _meta.get(_key(room_code, user_id), {})


def clear(room_code: str, user_id: int):
    k = _key(room_code, user_id)
    _buffers[k] = bytearray()


def remove(room_code: str, user_id: int):
    k = _key(room_code, user_id)
    _buffers.pop(k, None)
    _meta.pop(k, None)
    _start_time.pop(k, None)
