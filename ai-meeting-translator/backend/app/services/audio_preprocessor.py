"""
Audio preprocessing trước khi đưa vào Whisper.
Input: PCM 16-bit / 16kHz / mono bytes
Output: WAV file path (temp) hoặc normalized bytes
"""

import os
import tempfile
import struct
import numpy as np


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000) -> str:
    """
    Ghi PCM 16-bit thành file WAV tạm.
    Trả đường dẫn file. Caller có trách nhiệm xóa file.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()

    num_samples = len(pcm_bytes) // 2
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = len(pcm_bytes)
    chunk_size = 36 + data_size

    with open(tmp.name, 'wb') as f:
        # WAV header
        f.write(b'RIFF')
        f.write(struct.pack('<I', chunk_size))
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write(struct.pack('<I', 16))           # subchunk1 size
        f.write(struct.pack('<H', 1))            # PCM format
        f.write(struct.pack('<H', num_channels))
        f.write(struct.pack('<I', sample_rate))
        f.write(struct.pack('<I', byte_rate))
        f.write(struct.pack('<H', block_align))
        f.write(struct.pack('<H', bits_per_sample))
        f.write(b'data')
        f.write(struct.pack('<I', data_size))
        f.write(pcm_bytes)

    return tmp.name


def normalize_audio(pcm_bytes: bytes) -> bytes:
    """
    Chuẩn hóa biên độ audio về ~70% max.
    Không clip, không distortion.
    """
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    max_val = np.max(np.abs(samples))
    if max_val < 1.0:
        return pcm_bytes  # silent, không làm gì
    target = 0.7 * 32767
    gain = min(target / max_val, 3.0)  # giới hạn gain tối đa 3x
    samples = np.clip(samples * gain, -32768, 32767).astype(np.int16)
    return samples.tobytes()


def remove_invalid_audio(pcm_bytes: bytes, min_duration_ms: int = 300,
                          sample_rate: int = 16000) -> bytes | None:
    """Trả None nếu audio quá ngắn hoặc rỗng."""
    min_samples = int(sample_rate * min_duration_ms / 1000)
    num_samples = len(pcm_bytes) // 2
    if num_samples < min_samples:
        return None
    return pcm_bytes
