from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from app.database import Base

class SystemLog(Base):
    __tablename__ = "system_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    level = Column(String(20), default="INFO", nullable=False, index=True)      # INFO, WARNING, ERROR
    service = Column(String(50), nullable=False, index=True)                   # WHISPER, TRANSLATION, TTS, WEBRTC, WEBSOCKET, AUTH, SYSTEM
    message = Column(String(500), nullable=False)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)
