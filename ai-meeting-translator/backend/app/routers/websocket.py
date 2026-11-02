"""
WebSocket router — xử lý signaling, STT, TTS, và điều phối sự kiện phòng họp.

Phân tách trách nhiệm:
  - WebRTC  : truyền audio giữa các peer trực tiếp (P2P)
  - WebSocket: signaling (offer/answer/ICE) + sự kiện ứng dụng (STT, dịch, TTS)
  - STT/Dịch/TTS: vẫn hoạt động qua nhánh riêng, không liên quan WebRTC
"""

import json
import asyncio
import logging
import uuid
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from starlette.websockets import WebSocketState
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.services import room_service, user_service
from app.services import audio_buffer
from app.services.transcription_service import transcribe_pcm
from app.services.translation_pipeline import process_transcript
from app.websocket.connection_manager import manager
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["WebSocket"])


# ── REST: ICE server config cho frontend ─────────────────────────────────────
@router.get("/api/webrtc/ice-config")
def get_ice_config():
    """Trả về danh sách ICE server đọc từ .env — không hardcode credential."""
    return {"iceServers": settings.ice_servers}


async def _reject_websocket(websocket: WebSocket, message: str, code: int = 1008):
    """Từ chối kết nối WebSocket an toàn, gửi thông báo lỗi rồi đóng kết nối."""
    try:
        await websocket.accept()
        await websocket.send_text(json.dumps({"type": "error", "message": message}, ensure_ascii=False))
        await websocket.close(code=code)
    except Exception as e:
        logger.debug(f"[WS] Error rejecting websocket: {e}")


# ── WebSocket endpoint ────────────────────────────────────────────────────────
@router.websocket("/ws/rooms/{room_code}/{user_id}")
async def websocket_endpoint(
    room_code: str,
    user_id: int,
    websocket: WebSocket,
    db: Session = Depends(get_db),
):
    # --- Validation: xác minh room, user, membership trước khi accept ---
    try:
        room = room_service.get_room_by_code(db, room_code)
    except Exception as e:
        logger.error(f"[WS] DB error fetching room {room_code}: {e}")
        await _reject_websocket(websocket, "Database error", code=1011)
        return

    if not room:
        await _reject_websocket(websocket, "Room not found")
        return

    if not room.is_active:
        await _reject_websocket(websocket, "Room is closed")
        return

    try:
        user = user_service.get_user_by_id(db, user_id)
    except Exception as e:
        logger.error(f"[WS] DB error fetching user {user_id}: {e}")
        await _reject_websocket(websocket, "Database error", code=1011)
        return

    if not user:
        await _reject_websocket(websocket, "User not found")
        return

    try:
        member = room_service.get_room_member(db, room.id, user_id)
        if not member:
            # Tự động ghi nhận thành viên nếu phòng đang mở
            member = room_service.join_room(db, room, user_id)
        elif not member.is_active:
            # Kích hoạt lại trạng thái active khi thành viên quay lại phòng
            member.is_active = True
            db.commit()
    except Exception as e:
        logger.error(f"[WS] DB error updating room member {user_id} in {room.id}: {e}")
        await _reject_websocket(websocket, "Database error", code=1011)
        return

    # Lưu lại thông tin cơ bản để không phụ thuộc vào session db trong loop
    room_id = room.id
    room_host_id = room.host_id          # dùng để kiểm tra host auto-close
    default_speaking_lang = user.speaking_language or "en"

    # Kết nối vào ConnectionManager
    await manager.connect(room_code, user_id, websocket)
    logger.info(f"[WS] User {user_id} connected to room {room_code}")

    # Gửi danh sách user đang online cho user mới để họ initiate WebRTC
    online_peers = manager.get_room_users(room_code, exclude=user_id)
    await manager.send_personal_message(websocket, {
        "type": "room_peers",
        "peers": online_peers,
    })

    # Thông báo user mới cho tất cả peer trong phòng
    await manager.broadcast(room_code, {"type": "user_joined", "user_id": user_id}, exclude=websocket)

    is_streaming = False

    try:
        while True:
            # Ngắt nếu socket không còn ở trạng thái CONNECTED
            if getattr(websocket, "client_state", None) == WebSocketState.DISCONNECTED:
                break

            msg = await websocket.receive()

            # ── ASGI Disconnect message: xử lý dọn dẹp và thoát loop ngay lập tức ──
            # Nếu không break ở đây, vòng lặp kế tiếp gọi websocket.receive() sẽ gây:
            # RuntimeError: Cannot call "receive" once a disconnect message has been received.
            if msg.get("type") == "websocket.disconnect":
                logger.info(f"[WS] User {user_id} disconnected (code={msg.get('code', 1000)})")
                break

            # ── BINARY: PCM audio chunk (STT pipeline) ───────────────────
            if msg.get("type") == "websocket.receive" and msg.get("bytes"):
                if is_streaming:
                    audio_buffer.append(room_code, user_id, msg["bytes"])
                continue

            # ── TEXT: JSON control messages ──────────────────────────────
            raw = msg.get("text", "")
            if not raw:
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await manager.send_personal_message(websocket, {
                    "type": "error", "message": "Invalid JSON"
                })
                continue

            t = payload.get("type")

            # ── Ping/pong ─────────────────────────────────────────────────
            if t == "ping":
                await manager.send_personal_message(websocket, {"type": "pong"})

            # ── Chat message ──────────────────────────────────────────────
            elif t == "message":
                await manager.broadcast(room_code, {
                    "type": "message",
                    "user_id": user_id,
                    "message": payload.get("message", ""),
                })

            # ── TTS preference ────────────────────────────────────────────
            elif t == "tts_toggle":
                enabled = bool(payload.get("enabled", True))
                manager.set_tts_enabled(room_code, user_id, enabled)
                await manager.send_personal_message(websocket, {
                    "type": "tts_toggled", "enabled": enabled,
                })

            # ── Presenter layout (screen / camera) — chỉ relay ────────────
            elif t == "presenter_state":
                mode = payload.get("mode") or "none"
                if mode not in ("screen", "camera", "none"):
                    mode = "none"
                await manager.broadcast(room_code, {
                    "type": "presenter_state",
                    "user_id": user_id,
                    "mode": mode,
                })

            # ── WebRTC Signaling ──────────────────────────────────────────
            elif t in ("webrtc_offer", "webrtc_answer", "webrtc_ice_candidate", "webrtc_ready", "webrtc_leave"):
                await _handle_webrtc_signaling(
                    t, payload, room_code, room_id, user_id, websocket, db
                )

            # ── STT: audio_start ──────────────────────────────────────────
            elif t == "audio_start":
                is_streaming = True
                lang = payload.get("speaking_language") or default_speaking_lang
                sr   = payload.get("sample_rate", 16000)
                mime = payload.get("mime_type", "audio/pcm")

                if not lang:
                    await manager.send_personal_message(websocket, {
                        "type": "error", "message": "SPEAKING_LANGUAGE_REQUIRED"
                    })
                    is_streaming = False
                    continue

                audio_buffer.start(room_code, user_id, mime_type=mime,
                                   sample_rate=sr, speaking_language=lang)
                logger.info(f"[STT] User {user_id} audio_start lang={lang}")
                await manager.broadcast(room_code, {
                    "type": "audio_start", "user_id": user_id,
                }, exclude=websocket)

            # ── STT: audio_stop ───────────────────────────────────────────
            elif t == "audio_stop":
                is_streaming = False
                meta   = audio_buffer.get_meta(room_code, user_id)
                pcm    = audio_buffer.get_audio(room_code, user_id)
                lang   = meta.get("speaking_language", "") or default_speaking_lang
                dur_ms = audio_buffer.get_duration_ms(room_code, user_id)
                audio_buffer.clear(room_code, user_id)

                if not lang:
                    await manager.send_personal_message(websocket, {
                        "type": "error", "message": "SPEAKING_LANGUAGE_REQUIRED"
                    })
                elif dur_ms < settings.VAD_MIN_SPEECH_MS:
                    logger.info(f"[STT] User {user_id} audio too short {dur_ms:.0f}ms")
                    await manager.send_personal_message(websocket, {
                        "type": "transcript_rejected", "reason": "AUDIO_TOO_SHORT"
                    })
                else:
                    logger.info(f"[STT] User {user_id} {dur_ms:.0f}ms lang={lang}")
                    asyncio.create_task(
                        _do_stt(room_code, user_id, pcm, lang, websocket,
                                meta.get("mime_type", "audio/pcm"))
                    )

                await manager.broadcast(room_code, {
                    "type": "audio_stop", "user_id": user_id,
                }, exclude=websocket)

            else:
                await manager.send_personal_message(websocket, {
                    "type": "error", "message": f"Unknown message type: {t}"
                })

    except WebSocketDisconnect as e:
        logger.info(f"[WS] WebSocketDisconnect for user {user_id} in room {room_code} (code={e.code})")
    except Exception as e:
        logger.error(f"[WS] Exception in WebSocket for user {user_id} in room {room_code}: {e}", exc_info=True)
    finally:
        # ── Safe Cleanup: Đảm bảo luôn dọn dẹp kết nối và tài nguyên khi kết nối đóng ──
        manager.disconnect(room_code, user_id, websocket)
        audio_buffer.remove(room_code, user_id)
        logger.info(f"[WS] Cleaned up user {user_id} from room {room_code}")

        # Cập nhật trạng thái thành viên offline trong database
        # Nếu là host → tự động đóng phòng và thông báo tất cả thành viên
        host_closed = False
        try:
            with SessionLocal() as session:
                r_member = room_service.get_room_member(session, room_id, user_id)
                if r_member:
                    r_member.is_active = False
                    session.commit()

                # Host rời phòng → đóng phòng tự động
                if user_id == room_host_id:
                    r_room = room_service.get_room_by_code(session, room_code)
                    if r_room and r_room.is_active:
                        room_service.close_room(session, r_room)
                        host_closed = True
                        logger.info(f"[WS] Host {user_id} disconnected — auto-closed room {room_code}")
        except Exception as e:
            logger.warning(f"[WS] Failed to update DB on user {user_id} disconnect: {e}")

        try:
            if host_closed:
                # Thông báo tất cả thành viên còn lại rằng phòng đã đóng
                await manager.broadcast(room_code, {
                    "type": "room_closed",
                    "message": "Chủ phòng đã rời — phòng họp kết thúc",
                })
            else:
                # Thông báo để các peer đóng RTCPeerConnection tương ứng
                await manager.broadcast(room_code, {
                    "type": "user_left",
                    "user_id": user_id,
                })
        except Exception as e:
            logger.warning(f"[WS] Failed to broadcast disconnect event for user {user_id}: {e}")


# ── WebRTC Signaling handler ──────────────────────────────────────────────────

async def _handle_webrtc_signaling(
    msg_type: str,
    payload: dict,
    room_code: str,
    room_id: int,
    sender_id: int,
    sender_ws: WebSocket,
    db: Session,
):
    """
    Relay WebRTC signaling message đến đúng target.
    Xác thực: target phải là thành viên active của room.
    Không tin tưởng room_id từ payload — dùng room_id từ URL (đã xác thực).
    """

    # webrtc_ready và webrtc_leave broadcast cho cả phòng
    if msg_type in ("webrtc_ready", "webrtc_leave"):
        await manager.broadcast(room_code, {
            "type": msg_type,
            "user_id": sender_id,
        }, exclude=sender_ws)
        return

    # Các message còn lại cần target_user_id
    target_id = payload.get("target_user_id")
    if not isinstance(target_id, int):
        await manager.send_personal_message(sender_ws, {
            "type": "error",
            "message": "target_user_id (int) required for WebRTC signaling",
        })
        return

    # Không tự gửi cho chính mình
    if target_id == sender_id:
        await manager.send_personal_message(sender_ws, {
            "type": "error", "message": "Cannot signal to yourself",
        })
        return

    # 1. Kiểm tra target đang online trong phòng trước
    target_ws = manager.get_user_websocket(room_code, target_id)
    if not target_ws:
        await manager.send_personal_message(sender_ws, {
            "type": "webrtc_peer_unavailable",
            "target_user_id": target_id,
            "reason": "offline",
        })
        return

    # 2. Xác thực target là thành viên active của room qua DB một cách an toàn
    try:
        target_member = room_service.get_room_member(db, room_id, target_id)
        if not target_member or not target_member.is_active:
            await manager.send_personal_message(sender_ws, {
                "type": "webrtc_peer_unavailable",
                "target_user_id": target_id,
                "reason": "not_a_member",
            })
            return
    except Exception as e:
        logger.warning(f"[Signaling] DB check target_member failed: {e}")

    # Relay — chỉ forward payload SDP/ICE, thêm from_user_id đã xác thực
    relay: dict = {}
    if msg_type == "webrtc_offer":
        relay = {
            "type": "webrtc_offer",
            "from_user_id": sender_id,
            "sdp": payload.get("sdp"),
        }
    elif msg_type == "webrtc_answer":
        relay = {
            "type": "webrtc_answer",
            "from_user_id": sender_id,
            "sdp": payload.get("sdp"),
        }
    elif msg_type == "webrtc_ice_candidate":
        relay = {
            "type": "webrtc_ice_candidate",
            "from_user_id": sender_id,
            "candidate": payload.get("candidate"),
        }

    if not relay:
        return

    sent = await manager.send_personal_message(target_ws, relay)
    if not sent:
        logger.warning(f"[Signaling] relay {msg_type} to {target_id} failed (peer connection closed)")
        await manager.send_personal_message(sender_ws, {
            "type": "webrtc_peer_unavailable",
            "target_user_id": target_id,
            "reason": "send_failed",
        })


# ── STT pipeline ──────────────────────────────────────────────────────────────

async def _do_stt(room_code: str, user_id: int, pcm: bytes, language: str,
                  ws: WebSocket, mime_type: str = "audio/pcm"):
    try:
        result = await transcribe_pcm(pcm, language, user_id, room_code, mime_type=mime_type)

        if result.get("rejected"):
            reason = result.get("reason", "UNKNOWN")
            await manager.send_personal_message(ws, {
                "type": "transcript_rejected", "reason": reason,
            })
            return

        text = result.get("text", "").strip()
        if not text:
            return

        message_id = str(uuid.uuid4())

        await manager.broadcast(room_code, {
            "type": "transcript",
            "user_id": user_id,
            "speaking_language": language,
            "text": text,
            "confidence": result.get("confidence", 0),
            "message_id": message_id,
        })

        db = SessionLocal()
        try:
            await process_transcript(
                db=db, room_code=room_code,
                speaker_user_id=user_id,
                source_language=language,
                text=text,
                message_id=message_id,
            )
        finally:
            db.close()

    except Exception as e:
        logger.error(f"[STT] User {user_id}: {e}", exc_info=True)
        await manager.send_personal_message(ws, {
            "type": "error", "message": "Speech recognition failed"
        })
