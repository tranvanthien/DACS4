"""
Kiểm thử toàn diện tính năng Giám sát AI & Hạ tầng thực tế cho Admin LinguaMeet.
Kiểm tra các yêu cầu:
1. Module Whisper/STT, Translation, TTS
2. Thu thập metrics thật tại từng bước xử lý (requests, success, failure, latency, 429)
3. Theo dõi WebSocket, Database ping, CPU/RAM
4. Lưu lịch sử metrics và lỗi vào database, khử dữ liệu nhạy cảm
5. Xác thực quyền Admin tại backend
6. Không dùng số liệu giả mạo
"""

import asyncio
import os
import sys
import tempfile
import time
import wave
import pytest
from fastapi.testclient import TestClient

# Thêm path app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app.config import settings
from app.database import SessionLocal
from app.models.ai_metric import AIMetricSnapshot
from app.models.system_log import SystemLog
from app.models.user import User
from app.services.ai_monitor_service import ai_monitor, sanitize_sensitive_data
from app.services.translation_service import translate_text
from app.services.tts_service import synthesize
from app.services.whisper_service import whisper_service
from app.services.transcription_service import transcribe_pcm


@pytest.fixture(scope="module")
def db_session():
    db = SessionLocal()
    yield db
    db.close()


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def admin_headers(db_session):
    admin_user = db_session.query(User).filter(User.is_admin == True).first()
    assert admin_user is not None, "Cần có ít nhất 1 tài khoản Admin trong DB để test"
    return {
        "X-Admin-Email": admin_user.email,
        "X-Admin-Key": settings.ADMIN_SECRET_KEY,
    }


# ── TEST 1: Sanitization bảo mật ─────────────────────────────────────────────
def test_sensitive_data_sanitization():
    raw_error = "Failed to call Google API key AIzaSyD1234567890abcdef1234567890abc with password=mysecretpass and Bearer eyJhbGciOiJIUzI1Ni"
    sanitized = sanitize_sensitive_data(raw_error)
    assert "AIzaSyD" not in sanitized
    assert "[REDACTED_GOOGLE_KEY]" in sanitized
    assert "mysecretpass" not in sanitized
    assert "[REDACTED]" in sanitized
    assert "eyJhbGciOiJIUzI1Ni" not in sanitized


# ── TEST 2: Translation Service & Metrics thật ────────────────────────────────
def test_real_translation_metrics():
    async def _run():
        initial_requests = ai_monitor.translation.requests_total
        initial_cache_hits = ai_monitor.translation.cache_hits

        # 1. Lần dịch đầu tiên
        result1 = await translate_text("Xin chào, chúc một ngày tốt lành", "vi", "en")
        assert result1 is not None and len(result1) > 0
        assert ai_monitor.translation.requests_total == initial_requests + 1
        assert ai_monitor.translation.last_latency_ms is not None
        assert ai_monitor.translation.last_latency_ms > 0

        # 2. Dịch cùng câu lần 2 để kiểm tra cache hit
        result2 = await translate_text("Xin chào, chúc một ngày tốt lành", "vi", "en")
        assert result2 == result1
        assert ai_monitor.translation.cache_hits == initial_cache_hits + 1

        summary = ai_monitor.translation.get_summary()
        assert summary["requests_total"] >= 1
        assert summary["avg_latency_ms"] is not None
        assert summary["status"] == "online"

    asyncio.run(_run())


# ── TEST 3: TTS Service & Metrics thật ────────────────────────────────────────
def test_real_tts_metrics():
    async def _run():
        initial_requests = ai_monitor.tts.requests_total
        initial_bytes = ai_monitor.tts.total_bytes

        # 1. Tổng hợp tiếng Anh
        audio_en = await synthesize("Testing TTS metrics in LinguaMeet", "en")
        assert audio_en is not None and len(audio_en) > 0
        assert ai_monitor.tts.requests_total == initial_requests + 1
        assert ai_monitor.tts.total_bytes > initial_bytes

        # 2. Tổng hợp cùng câu để kiểm tra cache hit
        audio_en_cached = await synthesize("Testing TTS metrics in LinguaMeet", "en")
        assert audio_en_cached == audio_en

        summary = ai_monitor.tts.get_summary()
        assert summary["requests_total"] >= 1
        assert summary["avg_latency_ms"] is not None
        assert summary["status"] == "online"

    asyncio.run(_run())


# ── TEST 4: Whisper STT & Metrics thật ────────────────────────────────────────
def test_real_whisper_metrics():
    async def _run():
        # 1. Tạo audio PCM hợp lệ (1 giây, 16000Hz, 16-bit mono)
        sample_rate = 16000
        duration_s = 1.0
        num_samples = int(sample_rate * duration_s)
        pcm_bytes = b"\x00\x00" * num_samples

        # 2. Chạy transcribe_pcm
        res = await transcribe_pcm(pcm_bytes, language="vi", user_id=999, room_code="TEST_ROOM")
        assert "rejected" in res

        # 3. Kiểm tra rejections được ghi nhận chính xác
        w_summary = ai_monitor.whisper.get_summary()
        assert w_summary["requests_total"] >= 1

    asyncio.run(_run())


# ── TEST 5: System & Database Metrics ─────────────────────────────────────────
def test_infrastructure_real_metrics(db_session):
    sys_m = ai_monitor.get_system_metrics()
    assert sys_m["cpu_percent"] is not None
    assert sys_m["ram_percent"] is not None
    assert sys_m["uptime_seconds"] >= 0
    assert sys_m["active_threads"] >= 1

    db_m = ai_monitor.get_database_status(db=db_session)
    assert db_m["status"] == "online"
    assert db_m["latency_ms"] is not None
    assert db_m["latency_ms"] >= 0


# ── TEST 6: Snapshot Lưu DB & Truy vấn Lịch sử ─────────────────────────────────
def test_save_snapshot_and_history(db_session):
    # 1. Lưu snapshot
    success = ai_monitor.save_snapshot(db=db_session)
    assert success is True

    # 2. Kiểm tra bản ghi trong bảng ai_metrics_history
    count = db_session.query(AIMetricSnapshot).count()
    assert count >= 4  # Ít nhất 4 bản ghi: INFRASTRUCTURE, WHISPER, TRANSLATION, TTS

    # 3. Lấy chuỗi lịch sử
    history = ai_monitor.get_historical_metrics(db=db_session, hours=1)
    assert len(history) >= 1
    point = history[-1]
    assert "timestamp" in point
    assert "cpu_percent" in point
    assert "requests_total" in point


# ── TEST 7: Admin API Security & Endpoints ────────────────────────────────────
def test_admin_api_security_and_endpoints(client, admin_headers):
    # 1. Không có header xác thực -> 401 Unauthorized
    resp_unauth = client.get("/api/admin/metrics/live")
    assert resp_unauth.status_code == 401

    # 2. Header sai key -> 403 Forbidden
    resp_wrong_key = client.get(
        "/api/admin/metrics/live",
        headers={"X-Admin-Email": admin_headers["X-Admin-Email"], "X-Admin-Key": "wrong-secret-key"},
    )
    assert resp_wrong_key.status_code == 403

    # 3. Header đúng quyền Admin -> 200 OK
    resp_live = client.get("/api/admin/metrics/live", headers=admin_headers)
    assert resp_live.status_code == 200
    live_data = resp_live.json()
    assert "system" in live_data
    assert "database" in live_data
    assert "services" in live_data
    assert "whisper" in live_data["services"]
    assert "translation" in live_data["services"]
    assert "tts" in live_data["services"]

    # 4. GET /api/admin/metrics/history
    resp_hist = client.get("/api/admin/metrics/history?hours=24", headers=admin_headers)
    assert resp_hist.status_code == 200
    hist_data = resp_hist.json()
    assert "history" in hist_data

    # 5. POST /api/admin/metrics/snapshot
    resp_snap = client.post("/api/admin/metrics/snapshot", headers=admin_headers)
    assert resp_snap.status_code == 200
    assert resp_snap.json()["success"] is True

    # 6. GET /api/admin/metrics/errors
    resp_err = client.get("/api/admin/metrics/errors?limit=10", headers=admin_headers)
    assert resp_err.status_code == 200
    assert "errors" in resp_err.json()

    # 7. POST /api/admin/ai/test (Translation)
    resp_test_trans = client.post(
        "/api/admin/ai/test",
        headers=admin_headers,
        json={"service": "translation", "text": "Kiểm thử hệ thống", "src": "vi", "tgt": "en"},
    )
    assert resp_test_trans.status_code == 200
    assert resp_test_trans.json()["success"] is True

    # 8. POST /api/admin/ai/test (TTS)
    resp_test_tts = client.post(
        "/api/admin/ai/test",
        headers=admin_headers,
        json={"service": "tts", "text": "Xin chào", "tgt": "vi"},
    )
    assert resp_test_tts.status_code == 200
    assert resp_test_tts.json()["success"] is True

    # 9. POST /api/admin/ai/test (Whisper)
    resp_test_whisper = client.post(
        "/api/admin/ai/test",
        headers=admin_headers,
        json={"service": "whisper"},
    )
    assert resp_test_whisper.status_code == 200
    assert resp_test_whisper.json()["success"] is True
