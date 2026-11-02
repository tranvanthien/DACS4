"""
TTS Service - Module 08.2
Provider abstraction: gtts | edge
Cấu hình qua .env:
  TTS_PROVIDER=gtts|edge   (mặc định: gtts)
  TTS_ENABLED=True|False
  TTS_TIMEOUT=15           (giây, chỉ dùng cho edge-tts)
  EDGE_TTS_VOICE_VI=vi-VN-HoaiMyNeural  (ghi đè voice mặc định)
"""

import asyncio
import hashlib
import logging
import os
import tempfile
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.services.language_service import is_supported_language, LANGUAGES
from app.services.ai_monitor_service import ai_monitor

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts")

# ── Cache: (md5(text), lang) → bytes ─────────────────────────────────────
_cache: dict[tuple[str, str], bytes] = {}
_MAX_CACHE = 200


def _cache_key(text: str, lang: str) -> tuple[str, str]:
    return (hashlib.md5(text.encode("utf-8")).hexdigest(), lang)


def _cache_get(text: str, lang: str) -> bytes | None:
    return _cache.get(_cache_key(text, lang))


def _cache_set(text: str, lang: str, data: bytes) -> None:
    if len(_cache) >= _MAX_CACHE:
        _cache.pop(next(iter(_cache)))
    _cache[_cache_key(text, lang)] = data


# ── Language maps ─────────────────────────────────────────────────────────
_GTTS_LANG_MAP: dict[str, str] = {
    "vi": "vi", "en": "en", "ko": "ko", "ja": "ja",
    "zh": "zh-CN", "fr": "fr", "de": "de", "es": "es",
}

_EDGE_DEFAULT_VOICES: dict[str, str] = {
    "vi": "vi-VN-HoaiMyNeural",
    "en": "en-US-JennyNeural",
    "ko": "ko-KR-SunHiNeural",
    "ja": "ja-JP-NanamiNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "es": "es-ES-ElviraNeural",
}


def _get_edge_voice(lang: str) -> str:
    """Đọc voice từ .env (EDGE_TTS_VOICE_VI=...) hoặc dùng mặc định."""
    return os.environ.get(
        f"EDGE_TTS_VOICE_{lang.upper()}",
        _EDGE_DEFAULT_VOICES.get(lang, "en-US-JennyNeural"),
    )


# ── Exception ─────────────────────────────────────────────────────────────
class TTSError(Exception):
    """Lỗi TTS — caller có thể catch riêng để không crash pipeline."""


# ── Base provider ─────────────────────────────────────────────────────────
class BaseTTSProvider(ABC):
    @abstractmethod
    def synthesize_sync(self, text: str, language: str) -> bytes:
        """Blocking. Chạy trong thread pool. Trả MP3 bytes."""

    @property
    def audio_format(self) -> str:
        return "mp3"

    def supports_language(self, lang: str) -> bool:
        return True


# ── gTTS provider ─────────────────────────────────────────────────────────
class GTTSProvider(BaseTTSProvider):
    """Google TTS — miễn phí, không cần API key, cần internet."""

    def supports_language(self, lang: str) -> bool:
        return lang in _GTTS_LANG_MAP

    def synthesize_sync(self, text: str, language: str) -> bytes:
        from gtts import gTTS, gTTSError

        tts_lang = _GTTS_LANG_MAP.get(language)
        if not tts_lang:
            raise TTSError(f"[gTTS] Ngôn ngữ không hỗ trợ: {language}")

        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        try:
            gTTS(text=text, lang=tts_lang, slow=False).save(tmp.name)
            with open(tmp.name, "rb") as f:
                return f.read()
        except gTTSError as e:
            raise TTSError(f"[gTTS] API lỗi: {e}") from e
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


# ── Edge TTS provider ─────────────────────────────────────────────────────
class EdgeTTSProvider(BaseTTSProvider):
    """Microsoft Edge TTS — miễn phí, chất lượng cao, cần internet."""

    def supports_language(self, lang: str) -> bool:
        return lang in _EDGE_DEFAULT_VOICES

    def synthesize_sync(self, text: str, language: str) -> bytes:
        try:
            import edge_tts
        except ImportError as e:
            raise TTSError("[EdgeTTS] Chưa cài edge-tts. Chạy: pip install edge-tts") from e

        voice = _get_edge_voice(language)
        timeout = int(getattr(settings, "TTS_TIMEOUT", 15))

        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()

        async def _run() -> None:
            communicate = edge_tts.Communicate(text, voice)
            await asyncio.wait_for(communicate.save(tmp.name), timeout=timeout)

        # Tạo event loop riêng — an toàn khi chạy trong thread pool
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_run())
        except asyncio.TimeoutError as e:
            raise TTSError(f"[EdgeTTS] Timeout sau {timeout}s") from e
        finally:
            loop.close()

        try:
            with open(tmp.name, "rb") as f:
                return f.read()
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


# ── Factory ───────────────────────────────────────────────────────────────
def _make_provider() -> BaseTTSProvider:
    name = getattr(settings, "TTS_PROVIDER", "gtts").lower().strip()
    if name == "edge":
        logger.info("[TTS] Provider: Edge TTS")
        return EdgeTTSProvider()
    logger.info("[TTS] Provider: gTTS")
    return GTTSProvider()


_provider: BaseTTSProvider = _make_provider()


# ── Internal cached synthesizer ───────────────────────────────────────────
def _synthesize_sync_cached(text: str, language: str) -> bytes:
    cached = _cache_get(text, language)
    if cached:
        logger.debug(f"[TTS] Cache hit lang={language}")
        ai_monitor.record_tts_cache_hit()
        return cached
    t0 = time.perf_counter()
    data = _provider.synthesize_sync(text, language)
    logger.info(f"[TTS] lang={language} size={len(data)}B time={int((time.perf_counter()-t0)*1000)}ms")
    _cache_set(text, language, data)
    return data


# ── Public async API ──────────────────────────────────────────────────────
async def synthesize(text: str, language: str) -> bytes:
    """
    Tổng hợp giọng nói bất đồng bộ, không block event loop.

    Args:
        text:     Văn bản cần đọc.
        language: Mã ngôn ngữ 2 ký tự (vi, en, ko, ja, zh, fr, de, es).

    Returns:
        MP3 bytes. Trả b"" nếu text rỗng hoặc TTS_ENABLED=False.

    Raises:
        TTSError: Ngôn ngữ không hỗ trợ hoặc provider thất bại.
    """
    if not getattr(settings, "TTS_ENABLED", True):
        return b""

    text = text.strip()
    if not text:
        return b""

    if not is_supported_language(language):
        raise TTSError(f"Ngôn ngữ không hỗ trợ: {language}")

    if not _provider.supports_language(language):
        raise TTSError(f"Provider '{settings.TTS_PROVIDER}' không hỗ trợ: {language}")

    loop = asyncio.get_event_loop()
    t0 = time.perf_counter()
    try:
        data = await loop.run_in_executor(_executor, _synthesize_sync_cached, text, language)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        ai_monitor.record_tts_result(
            latency_ms=elapsed_ms,
            language=language,
            success=True,
            bytes_count=len(data) if data else 0,
        )
        return data
    except TTSError as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        ai_monitor.record_tts_result(
            latency_ms=elapsed_ms,
            language=language,
            success=False,
            error=str(exc),
        )
        raise
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        ai_monitor.record_tts_result(
            latency_ms=elapsed_ms,
            language=language,
            success=False,
            error=str(exc),
        )
        raise TTSError(f"TTS thất bại ({language}): {exc}") from exc


def get_audio_format() -> str:
    return _provider.audio_format


def get_supported_languages() -> list[str]:
    return [lang for lang in LANGUAGES if _provider.supports_language(lang)]


def clear_cache() -> int:
    count = len(_cache)
    _cache.clear()
    return count
