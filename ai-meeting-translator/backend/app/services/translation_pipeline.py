"""
Pipeline: transcript → dịch song song → TTS song song → gửi đúng người nghe.

Module 08.4 — WebSocket event protocol:
  JSON  {"type": "translation",  message_id, ...}   gửi text dịch ngay
  JSON  {"type": "tts_start",    message_id, language, size}  báo trước khi gửi audio
  BIN   b"TTS\\x00" + MP3 bytes                      audio frame (4-byte magic prefix)
  JSON  {"type": "tts_error",    message_id, language, error}  nếu TTS thất bại

Đảm bảo:
- Chỉ gửi đúng ngôn ngữ đích cho từng user (listening_language).
- Không gửi lại cho người nói.
- Không gửi cho user ngoài phòng.
- Nếu user rời phòng trong lúc TTS đang xử lý → get_user_websocket trả None → bỏ qua.
- TTS lỗi → gửi tts_error JSON, text đã có rồi, meeting không bị ảnh hưởng.
- TTS chạy trong thread pool → không block event loop.
"""

import asyncio
import logging
import uuid
from sqlalchemy.orm import Session

from app.services.translation_service import translate_text
from app.services.tts_service import TTSError, synthesize as tts_synthesize
from app.services.room_service import get_active_members, get_room_by_code
from app.services.user_service import get_user_by_id
from app.services.language_service import is_supported_language
from app.websocket.connection_manager import manager
from app.config import settings

logger = logging.getLogger(__name__)

_TRANSLATION_SEMAPHORE = asyncio.Semaphore(6)
_TTS_SEMAPHORE = asyncio.Semaphore(4)
_processed_ids: set[str] = set()

# 4-byte magic prefix phân biệt TTS audio frame với raw PCM
TTS_FRAME_MAGIC = b"TTS\x00"


def pack_tts_frame(audio_bytes: bytes) -> bytes:
    """Đóng gói TTS audio: magic(4B) + MP3 bytes."""
    return TTS_FRAME_MAGIC + audio_bytes


async def process_transcript(
    db: Session,
    room_code: str,
    speaker_user_id: int,
    source_language: str,
    text: str,
    message_id: str = "",
):
    if not text or not text.strip():
        return
    if not is_supported_language(source_language):
        return

    if not message_id:
        message_id = str(uuid.uuid4())
    if message_id in _processed_ids:
        return
    _processed_ids.add(message_id)
    if len(_processed_ids) > 10_000:
        _processed_ids.clear()

    room = get_room_by_code(db, room_code)
    if not room:
        return

    members = get_active_members(db, room.id)

    # ── Group listeners: target_language → [user_id, ...] ─────────────────
    # Lấy thành viên active, bao gồm cả người nói (để họ thấy bản dịch của mình)
    groups: dict[str, list[int]] = {}
    for member in members:
        user = get_user_by_id(db, member.user_id)
        if not user or not user.listening_language:
            continue
        # Bỏ qua người nói nếu ngôn ngữ nghe = ngôn ngữ nói (không cần dịch)
        if member.user_id == speaker_user_id and user.listening_language == source_language:
            continue
        groups.setdefault(user.listening_language, []).append(member.user_id)

    if not groups:
        return

    # ── Bước 1: Dịch song song, 1 lần mỗi ngôn ngữ đích ──────────────────
    async def _translate_one(tgt: str) -> tuple[str, str | None]:
        async with _TRANSLATION_SEMAPHORE:
            try:
                result = await translate_text(text, source_language, tgt)
                return tgt, result
            except Exception as exc:
                logger.error(f"[Pipeline] translate {source_language}→{tgt}: {type(exc).__name__}: {exc}")
                return tgt, None

    translated: dict[str, str | None] = dict(
        await asyncio.gather(*[_translate_one(tgt) for tgt in groups])
    )

    # ── Bước 2: Gửi text dịch ngay — không chờ TTS ────────────────────────
    global_tts_on = settings.TTS_ENABLED

    for tgt, user_ids in groups.items():
        result_text = translated.get(tgt)
        for uid in user_ids:
            ws = manager.get_user_websocket(room_code, uid)
            if not ws:
                continue  # user đã rời phòng

            if result_text is None:
                msg = {
                    "type": "translation_error",
                    "message_id": message_id,
                    "user_id": speaker_user_id,
                    "source_language": source_language,
                    "target_language": tgt,
                    "error": "Translation service unavailable",
                }
            else:
                msg = {
                    "type": "translation",
                    "message_id": message_id,
                    "user_id": speaker_user_id,
                    "source_language": source_language,
                    "target_language": tgt,
                    "original_text": text,
                    "translated_text": result_text,
                    # Cho frontend biết có TTS sắp đến hay không
                    "tts_pending": global_tts_on and manager.is_tts_enabled(room_code, uid),
                }
            try:
                await manager.send_personal_message(ws, msg)
            except Exception as e:
                logger.warning(f"[Pipeline] send text uid={uid}: {e}")

    if not global_tts_on:
        logger.info(f"[Pipeline] TTS_ENABLED=False, skipping TTS for room={room_code}")
        return

    # ── Bước 3: TTS song song, shared per language ─────────────────────────
    # Chỉ tổng hợp khi có ít nhất 1 user đang connected và đã bật TTS
    tts_needed = {
        tgt for tgt, user_ids in groups.items()
        if translated.get(tgt)
        and any(
            uid != speaker_user_id  # không TTS cho người nói
            and manager.get_user_websocket(room_code, uid) is not None
            and manager.is_tts_enabled(room_code, uid)
            for uid in user_ids
        )
    }

    logger.info(f"[Pipeline] TTS needed for langs={tts_needed} room={room_code}")

    if not tts_needed:
        return

    async def _tts_one(tgt: str) -> tuple[str, bytes | None]:
        async with _TTS_SEMAPHORE:
            try:
                audio = await tts_synthesize(translated[tgt], tgt)
                return tgt, audio or None
            except TTSError as exc:
                logger.warning(f"[Pipeline] TTS {tgt}: {exc}")
                return tgt, None
            except Exception as exc:
                logger.error(f"[Pipeline] TTS unexpected {tgt}: {exc}")
                return tgt, None

    tts_results: dict[str, bytes | None] = dict(
        await asyncio.gather(*[_tts_one(tgt) for tgt in tts_needed])
    )

    # ── Bước 4: Gửi TTS events + audio tới từng user ──────────────────────
    for tgt, user_ids in groups.items():
        audio = tts_results.get(tgt)

        for uid in user_ids:
            if uid == speaker_user_id:
                continue  # không phát TTS lại cho người nói
            if not manager.is_tts_enabled(room_code, uid):
                continue

            # Kiểm tra lại — user có thể đã rời phòng trong lúc TTS đang xử lý
            ws = manager.get_user_websocket(room_code, uid)
            if not ws:
                logger.debug(f"[Pipeline] uid={uid} left room during TTS, skip")
                continue

            if audio is None:
                # Gửi tts_error để frontend biết không có audio
                try:
                    await manager.send_personal_message(ws, {
                        "type": "tts_error",
                        "message_id": message_id,
                        "language": tgt,
                        "error": "TTS synthesis failed",
                    })
                except Exception:
                    pass
                continue

            try:
                # tts_start: báo kích thước để frontend chuẩn bị
                await manager.send_personal_message(ws, {
                    "type": "tts_start",
                    "message_id": message_id,
                    "language": tgt,
                    "size": len(audio),
                })
                # tts_audio: binary frame với magic prefix
                await manager.send_personal_bytes(ws, pack_tts_frame(audio))
                logger.info(f"[Pipeline] TTS sent uid={uid} lang={tgt} {len(audio)}B")
            except Exception as e:
                logger.warning(f"[Pipeline] send TTS uid={uid}: {e}")
