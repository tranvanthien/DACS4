"""
Translation service với provider abstraction.
TRANSLATION_PROVIDER=nllb|marian|google|deep_google
"""

import asyncio
import hashlib
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.services.ai_monitor_service import ai_monitor

_executor = ThreadPoolExecutor(max_workers=1)  # tuần tự để tránh 429

# ── Global rate limiter cho gtx (tránh 429) ───────────────────────────────
import threading as _threading
_gtx_lock = _threading.Lock()
_gtx_last_call: float = 0.0
_GTX_MIN_INTERVAL = 0.4  # giây tối thiểu giữa 2 lần gọi gtx

# ── In-memory cache ────────────────────────────────────────────────────────
_cache: dict[str, str] = {}


def _cache_key(src: str, tgt: str, text: str) -> str:
    return hashlib.md5(f"{src}:{tgt}:{text}".encode()).hexdigest()


def _cache_get(src: str, tgt: str, text: str):
    return _cache.get(_cache_key(src, tgt, text))


def _cache_set(src: str, tgt: str, text: str, result: str):
    _cache[_cache_key(src, tgt, text)] = result


# ── Base ───────────────────────────────────────────────────────────────────
class BaseTranslationProvider(ABC):
    @abstractmethod
    def translate_sync(self, text: str, src: str, tgt: str) -> str:
        pass


# ── NLLB (offline, 200 languages) ─────────────────────────────────────────
class NLLBProvider(BaseTranslationProvider):
    LANG_MAP = {
        "vi": "vie_Latn", "en": "eng_Latn", "ko": "kor_Hang",
        "ja": "jpn_Jpan", "zh": "zho_Hans", "fr": "fra_Latn",
        "de": "deu_Latn", "es": "spa_Latn",
    }

    def __init__(self):
        self._model = None
        self._tokenizer = None

    def _load(self):
        if self._model is None:
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
            name = "facebook/nllb-200-distilled-600M"
            print(f"[Translation] Loading NLLB {name}...")
            self._tokenizer = AutoTokenizer.from_pretrained(name)
            self._model = AutoModelForSeq2SeqLM.from_pretrained(name)
            print("[Translation] NLLB loaded.")

    def translate_sync(self, text: str, src: str, tgt: str) -> str:
        self._load()
        src_tag = self.LANG_MAP.get(src)
        tgt_tag = self.LANG_MAP.get(tgt)
        if not src_tag or not tgt_tag:
            raise ValueError(f"Unsupported pair: {src}→{tgt}")
        self._tokenizer.src_lang = src_tag
        inputs = self._tokenizer(text, return_tensors="pt", padding=True, truncation=True, max_length=512)
        forced_bos = self._tokenizer.convert_tokens_to_ids(tgt_tag)
        outputs = self._model.generate(
            **inputs,
            forced_bos_token_id=forced_bos,
            max_new_tokens=512,
        )
        return self._tokenizer.batch_decode(outputs, skip_special_tokens=True)[0]


# ── MarianMT (offline, per language pair) ─────────────────────────────────
class MarianProvider(BaseTranslationProvider):
    def __init__(self):
        self._models: dict[str, object] = {}
        self._tokenizers: dict[str, object] = {}

    def _load(self, src: str, tgt: str):
        key = f"{src}-{tgt}"
        if key not in self._models:
            from transformers import MarianMTModel, MarianTokenizer
            name = f"Helsinki-NLP/opus-mt-{src}-{tgt}"
            print(f"[Translation] Loading Marian {name}...")
            self._tokenizers[key] = MarianTokenizer.from_pretrained(name)
            self._models[key] = MarianMTModel.from_pretrained(name)
        return self._tokenizers[key], self._models[key]

    def translate_sync(self, text: str, src: str, tgt: str) -> str:
        tokenizer, model = self._load(src, tgt)
        inputs = tokenizer([text], return_tensors="pt", padding=True,
                           truncation=True, max_length=512)
        out = model.generate(**inputs)
        return tokenizer.batch_decode(out, skip_special_tokens=True)[0]


# ── Google Translate (official API, cần API key) ──────────────────────────
class GoogleProvider(BaseTranslationProvider):
    def translate_sync(self, text: str, src: str, tgt: str) -> str:
        import requests
        url = "https://translation.googleapis.com/language/translate/v2"
        resp = requests.post(url, params={
            "key": settings.GOOGLE_TRANSLATE_API_KEY,
            "q": text, "source": src, "target": tgt, "format": "text",
        }, timeout=10)
        resp.raise_for_status()
        return resp.json()["data"]["translations"][0]["translatedText"]


# ── DeepGoogle — Google gtx + MyMemory fallback ───────────────────────────
class DeepGoogleProvider(BaseTranslationProvider):
    """
    Primary: Google gtx (chất lượng tốt nhất).
    Fallback: MyMemory API (khi gtx bị 429).
    """

    _LANG_MAP = {"zh": "zh-CN"}
    _session = None
    # Khi gtx bị 429, tạm dừng dùng gtx trong N giây
    _gtx_blocked_until: float = 0.0

    @classmethod
    def _get_session(cls):
        import requests as _req
        if cls._session is None:
            cls._session = _req.Session()
            cls._session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://translate.google.com/",
            })
        return cls._session

    def _map(self, lang: str) -> str:
        return self._LANG_MAP.get(lang, lang)

    def _gtx(self, text: str, src: str, tgt: str) -> str | None:
        """Gọi Google gtx. Trả None nếu bị 429."""
        import time as _time
        global _gtx_last_call

        t_start = _time.time()

        # Nếu đang trong thời gian block, bỏ qua gtx
        if _time.time() < DeepGoogleProvider._gtx_blocked_until:
            remaining = DeepGoogleProvider._gtx_blocked_until - _time.time()
            print(f"[Translation] gtx blocked thêm {remaining:.0f}s, dùng fallback")
            return None

        with _gtx_lock:
            now = _time.time()
            gap = now - _gtx_last_call
            if gap < _GTX_MIN_INTERVAL:
                _time.sleep(_GTX_MIN_INTERVAL - gap)
            _gtx_last_call = _time.time()

        try:
            resp = self._get_session().get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": self._map(src), "tl": self._map(tgt), "dt": "t", "q": text},
                timeout=10,
            )
            if resp.status_code == 429:
                DeepGoogleProvider._gtx_blocked_until = _time.time() + 60
                print(f"[Translation] gtx 429 — block 60s, chuyển sang MyMemory")
                elapsed_ms = (_time.time() - t_start) * 1000
                ai_monitor.record_translation_result(
                    latency_ms=elapsed_ms, src=src, tgt=tgt, success=False, is_429=True
                )
                return None
            resp.raise_for_status()
            data = resp.json()
            result = "".join(part[0] for part in data[0] if part[0])
            return result or None
        except Exception as e:
            elapsed_ms = (_time.time() - t_start) * 1000
            if "429" in str(e):
                DeepGoogleProvider._gtx_blocked_until = _time.time() + 60
                print(f"[Translation] gtx 429 — block 60s, chuyển sang MyMemory")
                ai_monitor.record_translation_result(
                    latency_ms=elapsed_ms, src=src, tgt=tgt, success=False, is_429=True
                )
                return None
            print(f"[Translation] gtx error {src}→{tgt}: {e}")
            return None

    def _mymemory(self, text: str, src: str, tgt: str) -> str | None:
        """Gọi MyMemory API. Miễn phí, không cần key."""
        import requests as _req
        try:
            resp = _req.get(
                "https://api.mymemory.translated.net/get",
                params={"q": text, "langpair": f"{src}|{tgt}"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("responseData", {}).get("translatedText", "")
            if result and result.upper() not in ("INVALID LANGUAGE PAIR", "NO TRANSLATION FOUND"):
                return result
        except Exception as e:
            print(f"[Translation] MyMemory error {src}→{tgt}: {e}")
        return None

    def translate_sync(self, text: str, src: str, tgt: str) -> str:
        # Thử gtx trước
        result = self._gtx(text, src, tgt)
        if result:
            return result

        # Fallback MyMemory
        print(f"[Translation] Dùng MyMemory fallback {src}→{tgt}")
        ai_monitor.record_translation_fallback()
        result = self._mymemory(text, src, tgt)
        if result:
            return result

        raise RuntimeError(f"Tất cả provider thất bại ({src}→{tgt})")


# ── Factory ────────────────────────────────────────────────────────────────
def _make_provider() -> BaseTranslationProvider:
    p = getattr(settings, "TRANSLATION_PROVIDER", "nllb").lower().replace("-", "_")
    if p == "marian":
        return MarianProvider()
    if p == "google":
        return GoogleProvider()
    if p == "deep_google":
        return DeepGoogleProvider()
    return NLLBProvider()


_provider: BaseTranslationProvider = _make_provider()


# ── Core sync (runs in thread pool) ───────────────────────────────────────
def _translate_sync_cached(text: str, src: str, tgt: str) -> str:
    if src == tgt:
        return text
    cached = _cache_get(src, tgt, text)
    if cached is not None:
        ai_monitor.record_translation_cache_hit()
        print(f"[Translation] Cache hit {src}→{tgt}")
        return cached
    result = _provider.translate_sync(text, src, tgt)
    _cache_set(src, tgt, text, result)
    return result


# ── Public async API ───────────────────────────────────────────────────────
async def translate_text(text: str, src: str, tgt: str, retries: int = 1) -> str:
    """Async wrapper, retry tối đa retries lần, không block event loop."""
    if not text.strip() or src == tgt:
        return text
    loop = asyncio.get_event_loop()
    last_err = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            result = await loop.run_in_executor(_executor, _translate_sync_cached, text, src, tgt)
            elapsed_ms = (time.time() - t0) * 1000
            print(f"[Translation] {src}→{tgt} ({elapsed_ms:.0f}ms): \"{result[:60]}\"")
            ai_monitor.record_translation_result(
                latency_ms=elapsed_ms, src=src, tgt=tgt, success=True
            )
            return result
        except Exception as e:
            elapsed_ms = (time.time() - t0) * 1000
            last_err = e
            is_429 = "429" in str(e)
            ai_monitor.record_translation_result(
                latency_ms=elapsed_ms, src=src, tgt=tgt, success=False, error=str(e), is_429=is_429
            )
            print(f"[Translation] Attempt {attempt+1} failed {src}→{tgt}: {e}")
            if attempt < retries:
                await asyncio.sleep(0.5)
    raise last_err
