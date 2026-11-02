"""
Router Admin cho LinguaMeet — Quản trị hệ thống, Dashboard, Giám sát AI, Quản lý Người dùng, Phòng họp & Ngôn ngữ.
Toàn bộ API quản trị đều yêu cầu xác thực qua dependency require_admin.
"""

import time
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import require_admin
from app.models.user import User
from app.models.room import Room, RoomMember
from app.models.system_log import SystemLog
from app.schemas.user import UserResponse
from app.services import user_service, room_service, log_service
from app.services.language_service import LANGUAGES, get_all_languages, is_supported_language
from app.services.whisper_service import whisper_service
from app.services.translation_service import translate_text
from app.services.tts_service import synthesize as tts_synthesize
from app.websocket.connection_manager import manager
from app.services.ai_monitor_service import ai_monitor

router = APIRouter(prefix="/api/admin", tags=["Admin Portal"])


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Schemas cho Admin
# ─────────────────────────────────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str
    admin_key: Optional[str] = None


class AdminUserCreate(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    speaking_language: str = "vi"
    listening_language: str = "vi"
    is_admin: bool = False


class AdminUserUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    speaking_language: Optional[str] = None
    listening_language: Optional[str] = None
    is_admin: Optional[bool] = None


class AdminPasswordUpdate(BaseModel):
    new_password: str


class AITestRequest(BaseModel):
    service: str  # whisper | translation | tts
    text: Optional[str] = None
    src: Optional[str] = "en"
    tgt: Optional[str] = "vi"


class SimulateErrorRequest(BaseModel):
    service: str = "SYSTEM"
    message: str
    details: Optional[str] = None
    level: str = "ERROR"


# ─────────────────────────────────────────────────────────────────────────────
# Authentication
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/login")
def admin_login(creds: AdminLoginRequest, db: Session = Depends(get_db)):
    """
    Đăng nhập quản trị viên:
    - Kiểm tra email, mật khẩu.
    - Kiểm tra is_admin == True.
    - Nếu admin_key được gửi, kiểm tra khớp ADMIN_SECRET_KEY.
    - Trả về thông tin user và admin_key để lưu vào session header.
    """
    user = user_service.get_user_by_email(db, creds.email)
    if not user or not user_service.verify_password(creds.password, user.password_hash):
        log_service.record_log("WARNING", "AUTH", f"Đăng nhập Admin thất bại cho email {creds.email}: Sai thông tin", db=db)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email hoặc mật khẩu không chính xác",
        )

    if not getattr(user, "is_admin", False):
        log_service.record_log("WARNING", "AUTH", f"Từ chối truy cập Admin cho {creds.email}: Không có quyền is_admin", db=db)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản này không có quyền Quản trị viên (is_admin = False)",
        )

    if creds.admin_key and creds.admin_key != settings.ADMIN_SECRET_KEY:
        log_service.record_log("WARNING", "AUTH", f"Đăng nhập Admin thất bại cho {creds.email}: Sai Admin Secret Key", db=db)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Mã Admin Secret Key không đúng",
        )

    log_service.record_log("INFO", "AUTH", f"Quản trị viên {user.email} đăng nhập thành công", db=db)

    return {
        "message": "Admin login successful",
        "user": UserResponse.model_validate(user),
        "admin_key": settings.ADMIN_SECRET_KEY,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Dashboard: Tổng quan thống kê & Biểu đồ
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/stats/overview")
def get_dashboard_overview(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Thống kê tổng quan hệ thống dựa trên cơ sở dữ liệu thật và bộ nhớ runtime:
    - Tổng người dùng & người dùng mới trong 7 ngày
    - Tổng phòng họp, phòng đang mở, phòng đã đóng
    - Số cuộc họp và người tham gia đang trực tuyến (realtime WebSocket)
    - Số ngôn ngữ hỗ trợ
    - Số lỗi trong 24 giờ qua từ system_logs
    """
    now = datetime.now()
    seven_days_ago = now - timedelta(days=7)

    total_users = db.query(User).count()
    new_users_7d = db.query(User).filter(User.created_at >= seven_days_ago).count()
    admin_users = db.query(User).filter(User.is_admin == True).count()

    total_rooms = db.query(Room).count()
    active_rooms = db.query(Room).filter(Room.is_active == True).count()
    closed_rooms = total_rooms - active_rooms

    total_langs = len(LANGUAGES)

    # Realtime connections từ ConnectionManager
    live_active_rooms = len(manager.room_connections)
    live_participants = len(manager.user_map)

    # Lỗi 24h
    errors_24h = log_service.get_error_count(db, hours=24)

    return {
        "users": {
            "total": total_users,
            "new_7d": new_users_7d,
            "admins": admin_users,
        },
        "rooms": {
            "total": total_rooms,
            "active": active_rooms,
            "closed": closed_rooms,
        },
        "realtime": {
            "live_rooms": live_active_rooms,
            "live_participants": live_participants,
        },
        "languages": {
            "total": total_langs,
        },
        "system": {
            "errors_24h": errors_24h,
            "app_name": settings.APP_NAME,
            "whisper_model": settings.WHISPER_MODEL,
            "translation_provider": settings.TRANSLATION_PROVIDER,
            "tts_provider": settings.TTS_PROVIDER,
        }
    }


@router.get("/stats/charts")
def get_dashboard_charts(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Dữ liệu biểu đồ thống kê theo chuỗi thời gian thực:
    - Số cuộc họp tạo theo ngày (14 ngày gần nhất)
    - Số người dùng đăng ký theo ngày (14 ngày gần nhất)
    - Phân bố ngôn ngữ nói và nghe của người dùng
    - Phân loại lỗi theo service trong nhật ký hệ thống
    """
    now = datetime.now()
    fourteen_days_ago = now - timedelta(days=14)

    # 1. Meetings per day (last 14 days)
    # Generate continuous date map
    date_map_rooms = {}
    date_map_users = {}
    for i in range(14, -1, -1):
        d_str = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        date_map_rooms[d_str] = 0
        date_map_users[d_str] = 0

    rooms_by_day = db.query(
        func.date(Room.created_at).label("day"),
        func.count(Room.id).label("count")
    ).filter(Room.created_at >= fourteen_days_ago).group_by("day").all()

    for r in rooms_by_day:
        d_str = str(r.day)
        if d_str in date_map_rooms:
            date_map_rooms[d_str] = int(r.count)

    # 2. Users per day (last 14 days)
    users_by_day = db.query(
        func.date(User.created_at).label("day"),
        func.count(User.id).label("count")
    ).filter(User.created_at >= fourteen_days_ago).group_by("day").all()

    for u in users_by_day:
        d_str = str(u.day)
        if d_str in date_map_users:
            date_map_users[d_str] = int(u.count)

    # 3. Language distribution
    lang_counts = {}
    for code, info in LANGUAGES.items():
        speaking = db.query(User).filter(User.speaking_language == code).count()
        listening = db.query(User).filter(User.listening_language == code).count()
        lang_counts[code] = {
            "name": info["name"],
            "flag": info["flag"],
            "speaking": speaking,
            "listening": listening,
            "total": speaking + listening,
        }

    # 4. Error distribution by service (all-time / recent 30d)
    error_stats = db.query(
        SystemLog.service,
        func.count(SystemLog.id).label("count")
    ).filter(SystemLog.level == "ERROR").group_by(SystemLog.service).all()

    error_by_service = {s.service: s.count for s in error_stats}

    return {
        "meetings_timeline": [
            {"date": d, "count": count} for d, count in date_map_rooms.items()
        ],
        "users_timeline": [
            {"date": d, "count": count} for d, count in date_map_users.items()
        ],
        "languages": lang_counts,
        "errors_by_service": error_by_service,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. AI Services Status & Monitoring (Whisper, Translation, TTS) & Real Metrics
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/metrics/live")
def get_live_metrics(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Trả toàn bộ metrics thời gian thực đo được tại backend:
    - CPU, RAM, Uptime, Threads
    - Database ping latency & status
    - WebSocket connections (live & lifetime)
    - Whisper STT (requests, avg latency, infer_ms, wait_ms, rejections, last used)
    - Translation (requests, avg latency, 429 rate limit count, fallback count, cache hits)
    - TTS (requests, avg latency, cache hits, bytes, last used)
    """
    sys_m = ai_monitor.get_system_metrics()
    db_m = ai_monitor.get_database_status(db=db)
    ws_m = ai_monitor.get_websocket_status()

    w_summary = ai_monitor.whisper.get_summary()
    w_summary.update({
        "model": settings.WHISPER_MODEL,
        "device": settings.WHISPER_DEVICE,
        "compute_type": settings.WHISPER_COMPUTE_TYPE,
        "beam_size": settings.WHISPER_BEAM_SIZE,
        "is_model_loaded": hasattr(whisper_service, "model") and whisper_service.model is not None,
    })

    t_summary = ai_monitor.translation.get_summary()
    t_summary.update({
        "provider": settings.TRANSLATION_PROVIDER,
    })

    tts_summary = ai_monitor.tts.get_summary()
    tts_summary.update({
        "provider": settings.TTS_PROVIDER,
        "enabled": settings.TTS_ENABLED,
    })

    # Overall calculation
    statuses = [w_summary["status"], t_summary["status"], tts_summary["status"], db_m["status"]]
    if "offline" in statuses or "error" in statuses:
        overall = "error"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "timestamp": datetime.now().isoformat(),
        "overall_status": overall,
        "system": sys_m,
        "database": db_m,
        "websocket": ws_m,
        "services": {
            "whisper": w_summary,
            "translation": t_summary,
            "tts": tts_summary,
        }
    }


@router.get("/metrics/history")
def get_historical_metrics(
    hours: int = Query(24, ge=1, le=168, description="Số giờ cần lấy dữ liệu lịch sử (1 - 168h)"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Lấy chuỗi dữ liệu lịch sử từ database (ai_metrics_history) cho biểu đồ thời gian.
    Trả về [] nếu chưa có bản ghi, tuyệt đối không tạo số liệu giả.
    """
    history = ai_monitor.get_historical_metrics(db=db, hours=hours)
    return {
        "hours": hours,
        "total_points": len(history),
        "history": history,
    }


@router.post("/metrics/snapshot")
def trigger_metrics_snapshot(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Kích hoạt lưu snapshot metrics hiện tại vào cơ sở dữ liệu."""
    saved = ai_monitor.save_snapshot(db=db)
    if not saved:
        raise HTTPException(status_code=500, detail="Không thể lưu snapshot metrics vào cơ sở dữ liệu")
    return {
        "success": True,
        "message": "Đã lưu snapshot metrics vào cơ sở dữ liệu thành công",
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/metrics/errors")
def get_recent_ai_errors(
    limit: int = Query(25, ge=1, le=100),
    service: Optional[str] = Query(None, description="Lọc theo dịch vụ: WHISPER, TRANSLATION, TTS, SYSTEM"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Lấy danh sách lỗi AI & hệ thống gần nhất, đã khử sạch dữ liệu nhạy cảm.
    """
    query = db.query(SystemLog).filter(SystemLog.level.in_(["ERROR", "WARNING"]))
    if service and service.upper() != "ALL":
        query = query.filter(SystemLog.service == service.upper())
    logs = query.order_by(SystemLog.created_at.desc()).limit(limit).all()

    return {
        "total": len(logs),
        "errors": [
            {
                "id": l.id,
                "level": l.level,
                "service": l.service,
                "message": l.message,
                "details": l.details,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ]
    }


@router.get("/ai/status")
async def get_ai_services_status(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Kiểm tra thực tế trạng thái hoạt động của các dịch vụ AI kết hợp metrics runtime thật:
    - Whisper STT: Model status và latency đo được từ các lượt transcribe thật
    - Translation: Provider status, số lần dính 429 rate limit, latency thực
    - TTS: Provider status, latency thực tế, bytes generated
    - WebRTC / WebSocket: Trạng thái kết nối live
    """
    results = {}

    # 1. Whisper STT
    whisper_loaded = hasattr(whisper_service, "model") and whisper_service.model is not None
    w_stats = ai_monitor.whisper.get_summary()
    results["whisper"] = {
        "name": "Speech-to-Text (Whisper)",
        "service_key": "WHISPER",
        "provider": "faster-whisper",
        "status": "online" if whisper_loaded else "offline",
        "model": settings.WHISPER_MODEL,
        "device": settings.WHISPER_DEVICE,
        "compute_type": settings.WHISPER_COMPUTE_TYPE,
        "beam_size": settings.WHISPER_BEAM_SIZE,
        "vad_threshold": settings.VAD_THRESHOLD,
        "min_speech_ms": settings.VAD_MIN_SPEECH_MS,
        "requests_total": w_stats["requests_total"],
        "requests_success": w_stats["requests_success"],
        "requests_failed": w_stats["requests_failed"],
        "avg_latency_ms": w_stats["avg_latency_ms"],
        "avg_infer_ms": w_stats["avg_infer_ms"],
        "last_latency_ms": w_stats["last_latency_ms"],
        "description": f"Model {settings.WHISPER_MODEL} trên {settings.WHISPER_DEVICE} ({settings.WHISPER_COMPUTE_TYPE})",
    }

    # 2. Translation Service
    t_stats = ai_monitor.translation.get_summary()
    results["translation"] = {
        "name": "Dịch thuật AI",
        "service_key": "TRANSLATION",
        "provider": settings.TRANSLATION_PROVIDER,
        "status": t_stats["status"],
        "requests_total": t_stats["requests_total"],
        "requests_success": t_stats["requests_success"],
        "requests_failed": t_stats["requests_failed"],
        "rate_limit_429": t_stats["rate_limit_429"],
        "fallback_count": t_stats["fallback_count"],
        "cache_hits": t_stats["cache_hits"],
        "avg_latency_ms": t_stats["avg_latency_ms"],
        "last_latency_ms": t_stats["last_latency_ms"],
        "last_error": t_stats["last_error"],
        "description": f"Provider {settings.TRANSLATION_PROVIDER} (Google gtx / MyMemory fallback)",
    }

    # 3. TTS Service
    tts_stats = ai_monitor.tts.get_summary()
    results["tts"] = {
        "name": "Text-to-Speech (TTS)",
        "service_key": "TTS",
        "provider": settings.TTS_PROVIDER,
        "enabled": settings.TTS_ENABLED,
        "status": tts_stats["status"] if settings.TTS_ENABLED else "disabled",
        "requests_total": tts_stats["requests_total"],
        "requests_success": tts_stats["requests_success"],
        "requests_failed": tts_stats["requests_failed"],
        "cache_hits": tts_stats["cache_hits"],
        "avg_latency_ms": tts_stats["avg_latency_ms"],
        "last_latency_ms": tts_stats["last_latency_ms"],
        "total_bytes": tts_stats["total_bytes"],
        "description": f"Provider {settings.TTS_PROVIDER}",
    }

    # 4. WebSocket & WebRTC ICE
    ws_stats = ai_monitor.get_websocket_status()
    results["webrtc"] = {
        "name": "WebRTC & WebSocket Signaling",
        "service_key": "WEBRTC",
        "status": "online",
        "stun_servers": [s["urls"] for s in settings.ice_servers],
        "has_turn": bool(settings.TURN_URL),
        "active_rooms": ws_stats["active_rooms"],
        "active_peers": ws_stats["active_connections"],
        "lifetime_connections": ws_stats["lifetime_connections"],
        "description": f"{ws_stats['active_rooms']} phòng trực tuyến, {ws_stats['active_connections']} kết nối WebSocket",
    }

    # Đánh giá tổng thể
    statuses = [results["whisper"]["status"], results["translation"]["status"], results["tts"]["status"]]
    if "offline" in statuses or "error" in statuses:
        overall = "error"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "overall_status": overall,
        "timestamp": datetime.now().isoformat(),
        "services": results,
    }


@router.post("/ai/test")
async def test_ai_service(
    req: AITestRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Thực hiện kiểm tra on-demand một dịch vụ AI cụ thể và trả về kết quả chi tiết.
    """
    service = req.service.lower()
    t0 = time.time()

    if service == "translation":
        src = req.src or "vi"
        tgt = req.tgt or "en"
        text_input = req.text or "Xin chào, đây là cuộc họp trực tuyến đa ngôn ngữ."
        try:
            translated = await translate_text(text_input, src, tgt)
            elapsed_ms = round((time.time() - t0) * 1000)
            log_service.record_log("INFO", "TRANSLATION", f"Admin test dịch {src}→{tgt} thành công ({elapsed_ms}ms)", db=db)
            return {
                "success": True,
                "service": "translation",
                "input": text_input,
                "output": translated,
                "src": src,
                "tgt": tgt,
                "elapsed_ms": elapsed_ms,
            }
        except Exception as e:
            elapsed_ms = round((time.time() - t0) * 1000)
            log_service.record_log("ERROR", "TRANSLATION", f"Admin test dịch {src}→{tgt} lỗi: {e}", details=str(e), db=db)
            return {
                "success": False,
                "service": "translation",
                "error": str(e),
                "elapsed_ms": elapsed_ms,
            }

    elif service == "tts":
        tgt = req.tgt or "vi"
        text_input = req.text or "Xin chào, hệ thống nhận diện giọng nói đang hoạt động tốt."
        try:
            audio_bytes = await tts_synthesize(text_input, tgt)
            elapsed_ms = round((time.time() - t0) * 1000)
            size = len(audio_bytes) if audio_bytes else 0
            log_service.record_log("INFO", "TTS", f"Admin test TTS lang={tgt} thành công: {size} bytes ({elapsed_ms}ms)", db=db)
            return {
                "success": True,
                "service": "tts",
                "input": text_input,
                "audio_bytes_length": size,
                "format": "mp3",
                "elapsed_ms": elapsed_ms,
            }
        except Exception as e:
            elapsed_ms = round((time.time() - t0) * 1000)
            log_service.record_log("ERROR", "TTS", f"Admin test TTS lang={tgt} lỗi: {e}", details=str(e), db=db)
            return {
                "success": False,
                "service": "tts",
                "error": str(e),
                "elapsed_ms": elapsed_ms,
            }

    elif service == "whisper":
        is_ready = hasattr(whisper_service, "model") and whisper_service.model is not None
        if not is_ready:
            return {
                "success": False,
                "service": "whisper",
                "error": "Whisper model chưa sẵn sàng hoặc chưa được nạp",
                "elapsed_ms": 0,
            }

        # Tạo file WAV mẫu 16kHz mono ngắn để đo suy luận thực tế
        import tempfile, wave
        tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_wav.close()
        try:
            with wave.open(tmp_wav.name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"\x00" * 16000)  # 0.5 giây âm thanh
            t_whisper = time.time()
            trans_res = whisper_service.transcribe(tmp_wav.name, language="vi")
            elapsed_ms = round((time.time() - t_whisper) * 1000)
            return {
                "success": True,
                "service": "whisper",
                "model": settings.WHISPER_MODEL,
                "device": settings.WHISPER_DEVICE,
                "compute_type": settings.WHISPER_COMPUTE_TYPE,
                "status": "ready",
                "elapsed_ms": elapsed_ms,
                "infer_ms": round(trans_res.get("infer_ms", 0), 1),
                "wait_ms": round(trans_res.get("wait_ms", 0), 1),
            }
        except Exception as e:
            elapsed_ms = round((time.time() - t0) * 1000)
            return {
                "success": False,
                "service": "whisper",
                "error": str(e),
                "elapsed_ms": elapsed_ms,
            }
        finally:
            try:
                import os
                os.unlink(tmp_wav.name)
            except Exception:
                pass

    else:
        raise HTTPException(status_code=400, detail=f"Dịch vụ không hợp lệ: {service}")


# 3. Quản lý Lỗi & Nhật ký Hệ thống (System Logs)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/logs")
def get_system_logs(
    level: Optional[str] = Query(None, description="Lọc theo level: ERROR, WARNING, INFO"),
    service: Optional[str] = Query(None, description="Lọc theo service: WHISPER, TRANSLATION, TTS, WEBRTC, AUTH, SYSTEM"),
    search: Optional[str] = Query(None, description="Tìm kiếm nội dung log"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Lấy danh sách nhật ký hệ thống và lỗi với phân trang, tìm kiếm."""
    offset = (page - 1) * page_size
    logs, total = log_service.get_logs(
        db, level=level, service=service, search=search, limit=page_size, offset=offset
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "logs": [
            {
                "id": l.id,
                "level": l.level,
                "service": l.service,
                "message": l.message,
                "details": l.details,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ]
    }


@router.delete("/logs")
def clear_all_system_logs(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Xóa tất cả nhật ký hệ thống."""
    count = log_service.clear_logs(db)
    log_service.record_log("INFO", "SYSTEM", f"Quản trị viên {admin.email} đã xóa {count} nhật ký hệ thống", db=db)
    return {"message": "Logs cleared successfully", "deleted_count": count}


@router.post("/logs/simulate-error")
def simulate_system_error(
    req: SimulateErrorRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Tạo một bản ghi lỗi hoặc thông báo thử nghiệm vào cơ sở dữ liệu."""
    log_entry = log_service.record_log(
        level=req.level,
        service=req.service,
        message=req.message,
        details=req.details,
        db=db
    )
    return {
        "message": "Error simulated and recorded",
        "id": log_entry.id if log_entry else None
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. Quản lý Người dùng (User Management)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/users")
def get_admin_users(
    search: Optional[str] = None,
    language: Optional[str] = None,
    role: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Danh sách người dùng có phân trang, tìm kiếm, thống kê phòng họp."""
    offset = (page - 1) * page_size
    users, total = user_service.get_users_paged(
        db, search=search, language=language, role=role, limit=page_size, offset=offset
    )

    user_list = []
    for u in users:
        # Thống kê phòng
        hosted_count = db.query(Room).filter(Room.host_id == u.id).count()
        joined_count = db.query(RoomMember).filter(RoomMember.user_id == u.id).count()

        user_list.append({
            "id": u.id,
            "full_name": u.full_name,
            "email": u.email,
            "speaking_language": u.speaking_language,
            "listening_language": u.listening_language,
            "is_admin": getattr(u, "is_admin", False),
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "hosted_rooms_count": hosted_count,
            "joined_rooms_count": joined_count,
        })

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "users": user_list,
    }


@router.post("/users", status_code=status.HTTP_201_CREATED)
def admin_create_user(
    data: AdminUserCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Quản trị viên tạo tài khoản người dùng hoặc admin mới."""
    existing = user_service.get_user_by_email(db, data.email)
    if existing:
        raise HTTPException(status_code=400, detail="Email này đã được sử dụng")

    if not is_supported_language(data.speaking_language):
        raise HTTPException(status_code=400, detail=f"Ngôn ngữ nói không hợp lệ: {data.speaking_language}")
    if not is_supported_language(data.listening_language):
        raise HTTPException(status_code=400, detail=f"Ngôn ngữ nghe không hợp lệ: {data.listening_language}")

    hashed = user_service.hash_password(data.password)
    new_user = User(
        full_name=data.full_name,
        email=data.email,
        password_hash=hashed,
        speaking_language=data.speaking_language,
        listening_language=data.listening_language,
        is_admin=data.is_admin,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    log_service.record_log("INFO", "USERS", f"Admin {admin.email} đã tạo tài khoản {new_user.email} (is_admin={data.is_admin})", db=db)
    return {
        "message": "Tạo người dùng thành công",
        "user": UserResponse.model_validate(new_user),
    }


@router.get("/users/{user_id}")
def admin_get_user_detail(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Xem chi tiết đầy đủ của một người dùng kèm danh sách phòng đã tạo và đã tham gia."""
    user = user_service.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng")

    # Phòng đã tạo
    hosted_rooms = db.query(Room).filter(Room.host_id == user_id).order_by(Room.created_at.desc()).all()
    # Lịch sử tham gia
    user_rooms = room_service.get_user_rooms(db, user_id)

    return {
        "user": {
            "id": user.id,
            "full_name": user.full_name,
            "email": user.email,
            "speaking_language": user.speaking_language,
            "listening_language": user.listening_language,
            "is_admin": getattr(user, "is_admin", False),
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        "hosted_rooms": [
            {
                "id": r.id,
                "room_code": r.room_code,
                "room_name": r.room_name,
                "is_active": r.is_active,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in hosted_rooms
        ],
        "room_history": user_rooms,
    }


@router.put("/users/{user_id}")
def admin_update_user(
    user_id: int,
    data: AdminUserUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Cập nhật thông tin người dùng (tên, email, ngôn ngữ, quyền admin)."""
    user = user_service.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng")

    if data.email and data.email != user.email:
        existing = user_service.get_user_by_email(db, data.email)
        if existing and existing.id != user_id:
            raise HTTPException(status_code=400, detail="Email này đã được sử dụng bởi tài khoản khác")

    if data.speaking_language and not is_supported_language(data.speaking_language):
        raise HTTPException(status_code=400, detail=f"Ngôn ngữ nói không hợp lệ: {data.speaking_language}")
    if data.listening_language and not is_supported_language(data.listening_language):
        raise HTTPException(status_code=400, detail=f"Ngôn ngữ nghe không hợp lệ: {data.listening_language}")

    # Không cho phép tự gỡ quyền admin của chính mình
    if user.id == admin.id and data.is_admin is False:
        raise HTTPException(status_code=400, detail="Không thể tự gỡ quyền Quản trị viên của chính tài khoản đang đăng nhập")

    updated = user_service.update_user(
        db, user,
        full_name=data.full_name,
        email=data.email,
        speaking_language=data.speaking_language,
        listening_language=data.listening_language,
        is_admin=data.is_admin,
    )

    log_service.record_log("INFO", "USERS", f"Admin {admin.email} đã cập nhật tài khoản ID {user_id} ({updated.email})", db=db)
    return {
        "message": "Cập nhật người dùng thành công",
        "user": UserResponse.model_validate(updated),
    }


@router.put("/users/{user_id}/password")
def admin_change_user_password(
    user_id: int,
    data: AdminPasswordUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Đặt lại mật khẩu cho người dùng."""
    user = user_service.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng")

    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="Mật khẩu phải có ít nhất 6 ký tự")

    user_service.change_user_password(db, user, data.new_password)
    log_service.record_log("INFO", "USERS", f"Admin {admin.email} đã đổi mật khẩu cho user ID {user_id} ({user.email})", db=db)
    return {"message": "Đổi mật khẩu thành công"}


@router.delete("/users/{user_id}")
def admin_delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Xóa tài khoản người dùng."""
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Không thể xóa tài khoản của chính mình")

    user = user_service.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng")

    email = user.email
    user_service.delete_user(db, user)
    log_service.record_log("INFO", "USERS", f"Admin {admin.email} đã xóa tài khoản ID {user_id} ({email})", db=db)
    return {"message": f"Đã xóa tài khoản {email} thành công"}


# ─────────────────────────────────────────────────────────────────────────────
# 5. Quản lý Cuộc họp (Room Management)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/rooms")
def get_admin_rooms(
    search: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Danh sách tất cả cuộc họp từ database kèm số lượng thành viên và trạng thái realtime."""
    offset = (page - 1) * page_size
    rooms, total = room_service.get_all_rooms(
        db, search=search, status=status, limit=page_size, offset=offset
    )

    room_list = []
    for r in rooms:
        host = user_service.get_user_by_id(db, r.host_id)
        active_members_count = len(room_service.get_active_members(db, r.id))
        live_peers_count = manager.get_room_connection_count(r.room_code)

        room_list.append({
            "id": r.id,
            "room_code": r.room_code,
            "room_name": r.room_name,
            "host_id": r.host_id,
            "host_name": host.full_name if host else f"User #{r.host_id}",
            "host_email": host.email if host else "N/A",
            "is_active": r.is_active,
            "active_members_count": active_members_count,
            "live_peers_count": live_peers_count,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "rooms": room_list,
    }


@router.get("/rooms/{room_code}")
def get_admin_room_detail(
    room_code: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Xem chi tiết cuộc họp, danh sách thành viên và socket đang kết nối."""
    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Không tìm thấy phòng họp")

    host = user_service.get_user_by_id(db, room.host_id)

    # Lấy tất cả thành viên (kể cả đã rời phòng)
    memberships = db.query(RoomMember).filter(RoomMember.room_id == room.id).all()
    member_details = []
    for m in memberships:
        u = user_service.get_user_by_id(db, m.user_id)
        is_online = (manager.get_user_websocket(room_code, m.user_id) is not None)
        member_details.append({
            "user_id": m.user_id,
            "full_name": u.full_name if u else f"User #{m.user_id}",
            "email": u.email if u else "N/A",
            "speaking_language": u.speaking_language if u else "vi",
            "listening_language": u.listening_language if u else "vi",
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            "is_active_member": m.is_active,
            "is_online_now": is_online,
        })

    live_peers = manager.get_room_users(room_code)

    return {
        "room": {
            "id": room.id,
            "room_code": room.room_code,
            "room_name": room.room_name,
            "host_id": room.host_id,
            "host_name": host.full_name if host else f"User #{room.host_id}",
            "host_email": host.email if host else "N/A",
            "is_active": room.is_active,
            "created_at": room.created_at.isoformat() if room.created_at else None,
        },
        "members": member_details,
        "live_online_users": live_peers,
    }


@router.post("/rooms/{room_code}/close")
async def admin_close_room(
    room_code: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Quản trị viên đóng phòng họp cưỡng chế và thông báo cho người tham gia qua WebSocket."""
    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Không tìm thấy phòng họp")

    if not room.is_active:
        raise HTTPException(status_code=400, detail="Phòng họp này đã đóng trước đó")

    # Đóng phòng trong database
    room_service.close_room(db, room)

    # Thông báo cho toàn bộ kết nối WebSocket nếu phòng đang có người online
    try:
        await manager.broadcast(room_code, {
            "type": "room_closed",
            "message": "Phòng họp đã được đóng bởi Quản trị viên hệ thống",
        })
    except Exception as e:
        pass

    log_service.record_log("INFO", "ROOMS", f"Admin {admin.email} đã đóng phòng {room.room_name} ({room_code})", db=db)
    return {"message": "Đã đóng phòng họp thành công"}


@router.delete("/rooms/{room_code}")
def admin_delete_room(
    room_code: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Xóa phòng họp và toàn bộ lịch sử thành viên."""
    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Không tìm thấy phòng họp")

    room_name = room.room_name
    room_service.delete_room(db, room)
    log_service.record_log("INFO", "ROOMS", f"Admin {admin.email} đã xóa phòng {room_name} ({room_code})", db=db)
    return {"message": f"Đã xóa phòng {room_code} thành công"}


# ─────────────────────────────────────────────────────────────────────────────
# 6. Quản lý Ngôn ngữ (Language Management)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/languages")
def get_admin_languages(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Danh sách ngôn ngữ hỗ trợ kèm theo thống kê sử dụng thực tế:
    - Số người dùng chọn làm tiếng nói
    - Số người dùng chọn làm tiếng nghe
    - Trạng thái hỗ trợ STT (Whisper), Dịch thuật, TTS
    """
    languages_data = []

    for code, info in LANGUAGES.items():
        speaking_users = db.query(User).filter(User.speaking_language == code).count()
        listening_users = db.query(User).filter(User.listening_language == code).count()

        languages_data.append({
            "code": code,
            "name": info["name"],
            "flag": info["flag"],
            "speaking_users_count": speaking_users,
            "listening_users_count": listening_users,
            "total_users_count": speaking_users + listening_users,
            "stt_supported": True,  # Whisper hỗ trợ đa ngôn ngữ
            "translation_supported": True,
            "tts_supported": True,
        })

    return {
        "total": len(languages_data),
        "languages": languages_data,
    }
