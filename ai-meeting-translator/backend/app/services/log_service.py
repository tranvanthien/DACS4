import logging
from datetime import datetime, timedelta
from typing import Optional, List
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.system_log import SystemLog

logger = logging.getLogger("system_log")

def record_log(
    level: str,
    service: str,
    message: str,
    details: Optional[str] = None,
    db: Optional[Session] = None
) -> SystemLog:
    """
    Ghi nhật ký hệ thống vào cơ sở dữ liệu.
    Tự động mở session nếu db không được truyền vào.
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        log_entry = SystemLog(
            level=level.upper(),
            service=service.upper(),
            message=message[:500],
            details=details,
        )
        db.add(log_entry)
        db.commit()
        db.refresh(log_entry)
        return log_entry
    except Exception as e:
        logger.error(f"[LogService] Failed to record log: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return None
    finally:
        if close_db:
            db.close()


def get_logs(
    db: Session,
    level: Optional[str] = None,
    service: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> tuple[List[SystemLog], int]:
    """Lấy danh sách log hệ thống có phân trang và lọc."""
    query = db.query(SystemLog)

    if level and level.upper() != "ALL":
        query = query.filter(SystemLog.level == level.upper())

    if service and service.upper() != "ALL":
        query = query.filter(SystemLog.service == service.upper())

    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            (SystemLog.message.ilike(search_filter)) | 
            (SystemLog.details.ilike(search_filter))
        )

    total = query.count()
    logs = query.order_by(SystemLog.created_at.desc()).offset(offset).limit(limit).all()
    return logs, total


def get_error_count(db: Session, hours: int = 24) -> int:
    """Đếm số lượng lỗi trong N giờ gần nhất."""
    since = datetime.now() - timedelta(hours=hours)
    return db.query(SystemLog).filter(
        SystemLog.level == "ERROR",
        SystemLog.created_at >= since
    ).count()


def clear_logs(db: Session) -> int:
    """Xóa tất cả log (chức năng quản trị)."""
    deleted = db.query(SystemLog).delete()
    db.commit()
    return deleted
