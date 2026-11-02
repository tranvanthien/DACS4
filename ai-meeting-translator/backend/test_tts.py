"""
Kiểm thử độc lập TTS Service - Module 08.2
Chạy từ thư mục backend:
    python test_tts.py              # test gtts, ngôn ngữ vi/en/ko
    python test_tts.py vi en ja zh  # test ngôn ngữ cụ thể
    TTS_PROVIDER=edge python test_tts.py  # test edge-tts
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

test_langs = sys.argv[1:] or ["vi", "en", "ko"]


async def main():
    from app.services.tts_service import (
        TTSError, clear_cache, get_audio_format,
        get_supported_languages, synthesize,
    )
    from app.config import settings

    print(f"\n{'='*50}")
    print(f"TTS Provider : {settings.TTS_PROVIDER}")
    print(f"TTS Enabled  : {settings.TTS_ENABLED}")
    print(f"Audio format : {get_audio_format()}")
    print(f"Supported    : {get_supported_languages()}")
    print(f"{'='*50}\n")

    # Test 1: text rỗng
    result = await synthesize("   ", "vi")
    print(f"[1] Text rong  : {'PASS' if result == b'' else 'FAIL'}")

    # Test 2: ngôn ngữ không hỗ trợ
    try:
        await synthesize("hello", "xx")
        print("[2] Lang xx    : FAIL (no exception)")
    except TTSError as e:
        print(f"[2] Lang xx    : PASS -> {e}")

    # Test 3: tổng hợp thực tế
    samples = {
        "vi": "Xin chào, đây là bài kiểm tra.",
        "en": "Hello, this is a synthesis test.",
        "ko": "안녕하세요, 음성 합성 테스트입니다.",
        "ja": "こんにちは、音声合成のテストです。",
        "zh": "你好，这是语音合成测试。",
        "fr": "Bonjour, ceci est un test.",
        "de": "Hallo, dies ist ein Test.",
        "es": "Hola, esta es una prueba.",
    }

    os.makedirs("tts_output", exist_ok=True)
    fmt = get_audio_format()

    for lang in test_langs:
        text = samples.get(lang)
        if not text:
            print(f"[3] {lang:<4}         : SKIP (no sample)")
            continue
        try:
            t0 = time.perf_counter()
            data = await synthesize(text, lang)
            ms = int((time.perf_counter() - t0) * 1000)
            path = f"tts_output/tts_{lang}.{fmt}"
            open(path, "wb").write(data)
            print(f"[3] {lang:<4}         : PASS {len(data)}B {ms}ms -> {path}")
        except TTSError as e:
            print(f"[3] {lang:<4}         : FAIL TTSError -> {e}")
        except Exception as e:
            print(f"[3] {lang:<4}         : FAIL {type(e).__name__} -> {e}")

    # Test 4: cache
    clear_cache()
    t0 = time.perf_counter()
    await synthesize("cache test", "en")
    t1 = int((time.perf_counter() - t0) * 1000)
    t0 = time.perf_counter()
    await synthesize("cache test", "en")
    t2 = int((time.perf_counter() - t0) * 1000)
    print(f"[4] Cache      : 1st={t1}ms cached={t2}ms -> {'PASS' if t2 < t1 else 'INFO'}")

    print(f"\nAudio files in tts_output/\n")


if __name__ == "__main__":
    asyncio.run(main())
