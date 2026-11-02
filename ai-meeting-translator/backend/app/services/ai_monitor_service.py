"""
AI Monitor Service — Module Giám sát AI & Hạ tầng cho LinguaMeet.
Thu thập metrics thời gian thực (real metrics) tại từng bước xử lý của:
- Whisper STT (requests, latency, infer_ms, wait_ms, rejections)
- Translation (requests, latency, HTTP 429 rate limits, fallback count, cache hits)
- TTS (requests, latency, total bytes, cache hits)
- WebSocket & WebRTC connections
- Hạ tầng hệ thống: CPU, RAM, Uptime, Database latency

Đảm bảo an toàn thông tin:
- Khử sạch nội dung nhạy cảm, không lưu nội dung âm thanh, bản dịch hoặc API key.
- Không dùng số liệu giả mạo; nếu chưa có dữ liệu, trả về None hoặc chỉ báo rõ ràng.
"""

import collections
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import psutil
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.ai_metric import AIMetricSnapshot
from app.services import log_service
from app.websocket.connection_manager import manager

logger = logging.getLogger("ai_monitor")

# Thời điểm bắt đầu chạy backend
_SERVER_START_TIME = time.time()
_CURRENT_PROCESS = psutil.Process(os.getpid())


def sanitize_sensitive_data(val: Any) -> str:
    """
    Loại bỏ API key, mật khẩu, payload âm thanh hoặc chuỗi nhạy cảm khỏi log & metrics.
    """
    if val is None:
        return ""
    s = str(val)

    # 1. Che giấu API key (Google, OpenAI, Bearer tokens, etc.)
    s = re.sub(r'AIza[0-9A-Za-z-_]{20,50}', '[REDACTED_GOOGLE_KEY]', s)
    s = re.sub(r'Bearer\s+[A-Za-z0-9\-_\.]+', 'Bearer [REDACTED_TOKEN]', s, flags=re.IGNORECASE)
    s = re.sub(r'((?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*)([^\s&,;]+)', r'\1[REDACTED]', s, flags=re.IGNORECASE)

    # 2. Cắt ngắn chuỗi quá dài tránh lưu dump nội dung âm thanh/bản dịch
    if len(s) > 500:
        s = s[:497] + "..."
    return s


class ServiceMetricsTracker:
    """
    Lưu trữ số liệu thống kê thời gian thực của một dịch vụ AI.
    Thread-safe, bảo vệ bằng threading.Lock.
    """

    def __init__(self, service_name: str, max_recent: int = 150):
        self.service_name = service_name
        self.lock = threading.Lock()

        self.requests_total: int = 0
        self.requests_success: int = 0
        self.requests_failed: int = 0
        self.rate_limit_429: int = 0
        self.fallback_count: int = 0
        self.cache_hits: int = 0
        self.total_bytes: int = 0

        self.rejections: Dict[str, int] = collections.defaultdict(int)

        self.recent_latencies: collections.deque = collections.deque(maxlen=max_recent)
        self.recent_infer_times: collections.deque = collections.deque(maxlen=max_recent)
        self.recent_wait_times: collections.deque = collections.deque(maxlen=max_recent)

        self.last_processed_at: Optional[datetime] = None
        self.last_latency_ms: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_error_at: Optional[datetime] = None

    def record_success(
        self,
        latency_ms: float,
        infer_ms: Optional[float] = None,
        wait_ms: Optional[float] = None,
        bytes_count: int = 0,
    ):
        with self.lock:
            self.requests_total += 1
            self.requests_success += 1
            self.last_processed_at = datetime.now()
            self.last_latency_ms = round(latency_ms, 1)
            self.recent_latencies.append(latency_ms)

            if infer_ms is not None:
                self.recent_infer_times.append(infer_ms)
            if wait_ms is not None:
                self.recent_wait_times.append(wait_ms)
            if bytes_count > 0:
                self.total_bytes += bytes_count

    def record_failure(self, error_msg: str, latency_ms: Optional[float] = None, is_429: bool = False):
        with self.lock:
            self.requests_total += 1
            self.requests_failed += 1
            if is_429:
                self.rate_limit_429 += 1

            now = datetime.now()
            self.last_processed_at = now
            self.last_error_at = now
            self.last_error = sanitize_sensitive_data(error_msg)

            if latency_ms is not None:
                self.last_latency_ms = round(latency_ms, 1)
                self.recent_latencies.append(latency_ms)

    def record_rejection(self, reason: str, latency_ms: Optional[float] = None):
        with self.lock:
            self.requests_total += 1
            self.rejections[reason] += 1
            self.last_processed_at = datetime.now()
            if latency_ms is not None:
                self.last_latency_ms = round(latency_ms, 1)
                self.recent_latencies.append(latency_ms)

    def record_cache_hit(self):
        with self.lock:
            self.cache_hits += 1

    def record_fallback(self):
        with self.lock:
            self.fallback_count += 1

    def get_summary(self) -> Dict[str, Any]:
        with self.lock:
            latencies = list(self.recent_latencies)
            avg_lat = round(sum(latencies) / len(latencies), 1) if latencies else None
            min_lat = round(min(latencies), 1) if latencies else None
            max_lat = round(max(latencies), 1) if latencies else None

            infer_times = list(self.recent_infer_times)
            avg_infer = round(sum(infer_times) / len(infer_times), 1) if infer_times else None

            wait_times = list(self.recent_wait_times)
            avg_wait = round(sum(wait_times) / len(wait_times), 1) if wait_times else None

            success_rate = (
                round((self.requests_success / self.requests_total) * 100, 1)
                if self.requests_total > 0
                else None
            )

            total_cacheable = self.requests_total + self.cache_hits
            cache_hit_rate = (
                round((self.cache_hits / total_cacheable) * 100, 1)
                if total_cacheable > 0
                else None
            )

            # Đánh giá trạng thái thực tế dựa trên lỗi và lần hoạt động gần nhất
            if self.requests_total == 0:
                calc_status = "idle"  # Chưa có request nào
            elif self.rate_limit_429 > 0 or (self.requests_failed > 0 and self.requests_failed >= self.requests_success):
                calc_status = "degraded"
            else:
                calc_status = "online"

            return {
                "service": self.service_name,
                "status": calc_status,
                "requests_total": self.requests_total,
                "requests_success": self.requests_success,
                "requests_failed": self.requests_failed,
                "rate_limit_429": self.rate_limit_429,
                "fallback_count": self.fallback_count,
                "cache_hits": self.cache_hits,
                "cache_hit_rate": cache_hit_rate,
                "total_bytes": self.total_bytes,
                "rejections": dict(self.rejections),
                "avg_latency_ms": avg_lat,
                "min_latency_ms": min_lat,
                "max_latency_ms": max_lat,
                "avg_infer_ms": avg_infer,
                "avg_wait_ms": avg_wait,
                "last_latency_ms": self.last_latency_ms,
                "success_rate": success_rate,
                "last_processed_at": self.last_processed_at.isoformat() if self.last_processed_at else None,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            }


class AIMonitorService:
    """
    Singleton quản lý toàn bộ metrics, probe kiểm tra thực tế và snapshot lưu DB.
    """

    def __init__(self):
        self.whisper = ServiceMetricsTracker("WHISPER")
        self.translation = ServiceMetricsTracker("TRANSLATION")
        self.tts = ServiceMetricsTracker("TTS")
        self._total_ws_connections: int = 0
        self._ws_lock = threading.Lock()

    # ── Tracking hooks ─────────────────────────────────────────────────────────

    def record_ws_connected(self):
        with self._ws_lock:
            self._total_ws_connections += 1

    def record_stt_result(
        self,
        duration_ms: float,
        infer_ms: Optional[float] = None,
        wait_ms: Optional[float] = None,
        rejected: bool = False,
        reason: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """Ghi nhận metric từ transcription_service."""
        if error:
            self.whisper.record_failure(error, latency_ms=duration_ms)
            self._log_sanitized("ERROR", "WHISPER", f"Whisper STT gặp lỗi: {error}")
        elif rejected:
            r = reason or "REJECTED"
            self.whisper.record_rejection(r, latency_ms=duration_ms)
        else:
            self.whisper.record_success(
                latency_ms=duration_ms, infer_ms=infer_ms, wait_ms=wait_ms
            )

    def record_translation_result(
        self,
        latency_ms: float,
        src: str,
        tgt: str,
        success: bool,
        error: Optional[str] = None,
        is_429: bool = False,
        used_fallback: bool = False,
    ):
        """Ghi nhận metric từ translation_service."""
        if is_429:
            self.translation.record_failure("HTTP 429 Too Many Requests (Rate limit)", latency_ms=latency_ms, is_429=True)
            self._log_sanitized("WARNING", "TRANSLATION", f"Google Translation HTTP 429 (Rate limit) cho cặp {src}→{tgt}")
        elif not success:
            err_msg = error or "Translation failed"
            self.translation.record_failure(err_msg, latency_ms=latency_ms)
            self._log_sanitized("ERROR", "TRANSLATION", f"Dịch {src}→{tgt} thất bại: {err_msg}")
        else:
            self.translation.record_success(latency_ms=latency_ms)
            if used_fallback:
                self.translation.record_fallback()

    def record_translation_cache_hit(self):
        self.translation.record_cache_hit()

    def record_translation_fallback(self):
        """
        Ghi nhận khi pipeline dịch chuyển sang provider dự phòng (MyMemory fallback).
        Fallback là graceful degradation — KHÔNG phải lỗi.
        Chỉ tăng fallback_count, KHÔNG tăng requests_failed hay error_count.
        """
        self.translation.record_fallback()

    def record_tts_result(
        self,
        latency_ms: float,
        language: str,
        success: bool,
        bytes_count: int = 0,
        error: Optional[str] = None,
    ):
        """Ghi nhận metric từ tts_service."""
        if not success:
            err_msg = error or "TTS synthesis failed"
            self.tts.record_failure(err_msg, latency_ms=latency_ms)
            self._log_sanitized("ERROR", "TTS", f"TTS ({language}) thất bại: {err_msg}")
        else:
            self.tts.record_success(latency_ms=latency_ms, bytes_count=bytes_count)

    def record_tts_cache_hit(self):
        self.tts.record_cache_hit()

    def _log_sanitized(self, level: str, service: str, message: str, details: Optional[str] = None):
        """Lưu log vào DB một cách an toàn."""
        try:
            clean_msg = sanitize_sensitive_data(message)
            clean_det = sanitize_sensitive_data(details) if details else None
            log_service.record_log(level=level, service=service, message=clean_msg, details=clean_det)
        except Exception as e:
            logger.warning(f"[AIMonitor] Failed to write sanitized log: {e}")

    # ── Infrastructure Metrics ────────────────────────────────────────────────

    def get_system_metrics(self) -> Dict[str, Any]:
        """Thu thập chỉ số CPU, RAM thật từ psutil."""
        try:
            sys_cpu = psutil.cpu_percent(interval=None)
            proc_cpu = _CURRENT_PROCESS.cpu_percent(interval=None)

            mem = psutil.virtual_memory()
            proc_mem = _CURRENT_PROCESS.memory_info().rss / (1024 * 1024)  # MB

            uptime_sec = round(time.time() - _SERVER_START_TIME, 0)

            return {
                "cpu_percent": round(sys_cpu, 1),
                "process_cpu_percent": round(proc_cpu, 1),
                "cpu_cores": psutil.cpu_count(logical=True),
                "ram_percent": round(mem.percent, 1),
                "ram_used_mb": round((mem.total - mem.available) / (1024 * 1024), 1),
                "ram_total_mb": round(mem.total / (1024 * 1024), 1),
                "process_ram_mb": round(proc_mem, 1),
                "active_threads": threading.active_count(),
                "uptime_seconds": int(uptime_sec),
            }
        except Exception as e:
            logger.error(f"[AIMonitor] System metrics error: {e}")
            return {
                "cpu_percent": None,
                "ram_percent": None,
                "error": str(e),
            }

    def get_database_status(self, db: Optional[Session] = None) -> Dict[str, Any]:
        """Đo lường độ trễ và khả năng phản hồi thực tế của Database."""
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True

        t0 = time.perf_counter()
        try:
            db.execute(text("SELECT 1"))
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            db_status = "online" if latency_ms < 500 else "degraded"
            return {
                "status": db_status,
                "latency_ms": latency_ms,
                "database_engine": "MySQL" if not settings.DB_SQLITE else "SQLite",
            }
        except Exception as e:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            return {
                "status": "offline",
                "latency_ms": latency_ms,
                "error": str(e),
            }
        finally:
            if close_db:
                db.close()

    def get_websocket_status(self) -> Dict[str, Any]:
        """Lấy số lượng kết nối WebSocket thật từ ConnectionManager."""
        live_peers = len(manager.user_map)
        live_rooms = len(manager.room_connections)
        return {
            "active_connections": live_peers,
            "active_rooms": live_rooms,
            "lifetime_connections": self._total_ws_connections,
        }

    # ── Snapshot & History ────────────────────────────────────────────────────

    def save_snapshot(self, db: Optional[Session] = None) -> bool:
        """
        Lưu snapshot định kỳ của tất cả các dịch vụ vào bảng ai_metrics_history.
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True

        try:
            sys_m = self.get_system_metrics()
            db_m = self.get_database_status(db=db)
            ws_m = self.get_websocket_status()

            # Snapshot cho INFRASTRUCTURE
            infra_snap = AIMetricSnapshot(
                service="INFRASTRUCTURE",
                cpu_percent=sys_m.get("cpu_percent"),
                ram_percent=sys_m.get("ram_percent"),
                active_connections=ws_m.get("active_connections", 0),
                avg_latency_ms=db_m.get("latency_ms"),  # DB ping latency
                details_json=json.dumps({
                    "system": sys_m,
                    "database": db_m,
                    "websocket": ws_m,
                }),
            )
            db.add(infra_snap)

            # Snapshot cho từng dịch vụ AI
            for tracker, svc_name in [
                (self.whisper, "WHISPER"),
                (self.translation, "TRANSLATION"),
                (self.tts, "TTS"),
            ]:
                s = tracker.get_summary()
                ai_snap = AIMetricSnapshot(
                    service=svc_name,
                    requests_count=s["requests_total"],
                    success_count=s["requests_success"],
                    error_count=s["requests_failed"],
                    rate_limit_count=s.get("rate_limit_429", 0),
                    avg_latency_ms=s["avg_latency_ms"],
                    min_latency_ms=s["min_latency_ms"],
                    max_latency_ms=s["max_latency_ms"],
                    details_json=json.dumps(s),
                )
                db.add(ai_snap)

            db.commit()
            return True
        except Exception as e:
            logger.error(f"[AIMonitor] save_snapshot failed: {e}")
            try:
                db.rollback()
            except Exception:
                pass
            return False
        finally:
            if close_db:
                db.close()

    def get_historical_metrics(self, db: Session, hours: int = 24) -> List[Dict[str, Any]]:
        """
        Truy vấn dữ liệu lịch sử từ bảng ai_metrics_history trong N giờ gần nhất.
        Nếu không có dữ liệu thật trong DB, trả về mảng rỗng [] (không bịa số liệu).
        """
        since = datetime.now() - timedelta(hours=hours)
        snapshots = (
            db.query(AIMetricSnapshot)
            .filter(AIMetricSnapshot.timestamp >= since)
            .order_by(AIMetricSnapshot.timestamp.asc())
            .all()
        )

        if not snapshots:
            return []

        # Nhóm theo timestamp (làm tròn theo phút)
        grouped: Dict[str, Dict[str, Any]] = {}
        for snap in snapshots:
            t_key = snap.timestamp.strftime("%Y-%m-%d %H:%M")
            if t_key not in grouped:
                grouped[t_key] = {
                    "timestamp": t_key,
                    "cpu_percent": None,
                    "ram_percent": None,
                    "active_connections": 0,
                    "whisper_latency_ms": None,
                    "translation_latency_ms": None,
                    "tts_latency_ms": None,
                    "requests_total": 0,
                    "errors_total": 0,
                    "rate_limit_429": 0,
                }

            if snap.service == "INFRASTRUCTURE":
                grouped[t_key]["cpu_percent"] = snap.cpu_percent
                grouped[t_key]["ram_percent"] = snap.ram_percent
                grouped[t_key]["active_connections"] = snap.active_connections or 0
            elif snap.service == "WHISPER":
                grouped[t_key]["whisper_latency_ms"] = snap.avg_latency_ms
                grouped[t_key]["requests_total"] += snap.requests_count
                grouped[t_key]["errors_total"] += snap.error_count
            elif snap.service == "TRANSLATION":
                grouped[t_key]["translation_latency_ms"] = snap.avg_latency_ms
                grouped[t_key]["requests_total"] += snap.requests_count
                grouped[t_key]["errors_total"] += snap.error_count
                grouped[t_key]["rate_limit_429"] += snap.rate_limit_count
            elif snap.service == "TTS":
                grouped[t_key]["tts_latency_ms"] = snap.avg_latency_ms
                grouped[t_key]["requests_total"] += snap.requests_count
                grouped[t_key]["errors_total"] += snap.error_count

        return list(grouped.values())


# Singleton instance toàn cục
ai_monitor = AIMonitorService()
