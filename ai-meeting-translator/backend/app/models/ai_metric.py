from sqlalchemy import Column, Integer, String, Float, DateTime, Text
from sqlalchemy.sql import func
from app.database import Base


class AIMetricSnapshot(Base):
    """
    Bảng lưu trữ lịch sử metrics của các dịch vụ AI và hệ thống theo chuỗi thời gian.
    Dữ liệu được tổng hợp định kỳ hoặc khi có sự kiện, không lưu nội dung âm thanh hoặc bản dịch nhạy cảm.
    """
    __tablename__ = "ai_metrics_history"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    timestamp = Column(DateTime, server_default=func.now(), index=True, nullable=False)
    service = Column(String(50), nullable=False, index=True)  # WHISPER, TRANSLATION, TTS, INFRASTRUCTURE

    requests_count = Column(Integer, default=0, nullable=False)
    success_count = Column(Integer, default=0, nullable=False)
    error_count = Column(Integer, default=0, nullable=False)
    rate_limit_count = Column(Integer, default=0, nullable=False)  # Ví dụ: mã lỗi HTTP 429

    avg_latency_ms = Column(Float, nullable=True)
    min_latency_ms = Column(Float, nullable=True)
    max_latency_ms = Column(Float, nullable=True)

    cpu_percent = Column(Float, nullable=True)
    ram_percent = Column(Float, nullable=True)
    active_connections = Column(Integer, nullable=True)

    details_json = Column(Text, nullable=True)  # JSON lưu các chỉ số bổ sung: cache_hits, fallback_count, v.v.
