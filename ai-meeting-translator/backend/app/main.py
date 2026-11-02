import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Đảm bảo console Windows hỗ trợ UTF-8
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.config import settings
from app.database import Base, engine
from app.routers import auth, users, rooms, websocket, admin
import app.models  # noqa: F401
from app.services.ai_monitor_service import ai_monitor


async def _periodic_snapshot_loop():
    """Tự động ghi snapshot metrics định kỳ mỗi 60 giây."""
    while True:
        try:
            await asyncio.sleep(60)
            ai_monitor.save_snapshot()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[AIMonitor] Lỗi khi lưu snapshot định kỳ: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Tạo các bảng database nếu chưa tồn tại
    Base.metadata.create_all(bind=engine)

    # Khởi động tác vụ nền lưu snapshot định kỳ
    snapshot_task = asyncio.create_task(_periodic_snapshot_loop())

    # Lưu 1 snapshot khởi động ban đầu
    try:
        ai_monitor.save_snapshot()
    except Exception as e:
        print(f"[AIMonitor] Lỗi lưu snapshot khởi động: {e}")

    yield

    # Hủy tác vụ nền khi dừng server
    snapshot_task.cancel()
    try:
        await snapshot_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title=settings.APP_NAME,
    description="Backend API cho hệ thống họp trực tuyến đa ngôn ngữ",
    version="0.6.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Đăng ký routers
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(rooms.router)
app.include_router(websocket.router)
app.include_router(admin.router)

# Serve frontend tĩnh tại /ui
_frontend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend"))
if os.path.isdir(_frontend_path):
    app.mount("/ui", StaticFiles(directory=_frontend_path, html=True), name="frontend")
else:
    print(f"[WARNING] Frontend path not found: {_frontend_path}")


@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "message": "AI Meeting Translator API is running",
    }


@app.get("/api/network-info")
def network_info():
    """Trả thông tin địa chỉ mạng (LAN IP) để hỗ trợ quét QR trên thiết bị di động."""
    import socket
    candidates = []

    # 1. Lấy danh sách IP từ hostname
    try:
        hostname = socket.gethostname()
        for addr in socket.getaddrinfo(hostname, None):
            ip = addr[4][0]
            if ':' not in ip and not ip.startswith('127.'):
                if ip not in candidates:
                    candidates.append(ip)
    except Exception:
        pass

    # 2. Lấy IP outbound socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(('8.8.8.8', 80))
        outbound = s.getsockname()[0]
        s.close()
        if outbound and not outbound.startswith('127.') and outbound not in candidates:
            candidates.append(outbound)
    except Exception:
        pass

    # 3. Sắp xếp ưu tiên: 192.168.x.x > 10.x.x.x > 172.16-31.x.x
    def ip_priority(ip: str):
        parts = ip.split('.')
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            p0, p1 = int(parts[0]), int(parts[1])
            if p0 == 192 and p1 == 168:
                return 1
            if p0 == 10:
                return 2
            if p0 == 172 and 16 <= p1 <= 31:
                return 3
        return 4

    candidates.sort(key=ip_priority)
    primary_ip = candidates[0] if candidates else "127.0.0.1"

    return {
        "primary_ip": primary_ip,
        "all_ips": candidates,
        "port": 8000,
        "base_url": f"http://{primary_ip}:8000"
    }


@app.get("/api/health/ai")
def ai_health_check():
    """Public endpoint — trả trạng thái AI services cho room.html widget."""
    from app.services.whisper_service import whisper_service
    from app.services.translation_service import translate_text

    services = {}

    # Whisper
    whisper_ok = hasattr(whisper_service, "model") and whisper_service.model is not None
    services["whisper"] = {
        "status": "ok" if whisper_ok else "offline",
        "latency_ms": None,
    }

    # Translation ping (nhẹ)
    try:
        t0 = time.time()
        result = translate_text("hi", "en", "vi")
        latency = round((time.time() - t0) * 1000, 1)
        services["translation"] = {"status": "ok", "latency_ms": latency}
    except Exception:
        services["translation"] = {"status": "error", "latency_ms": None}

    # TTS — chỉ check import
    try:
        from app.services.tts_service import synthesize  # noqa
        services["tts"] = {"status": "ok", "latency_ms": None}
    except Exception:
        services["tts"] = {"status": "error", "latency_ms": None}

    return {"services": services}
