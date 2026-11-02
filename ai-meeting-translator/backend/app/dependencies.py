"""
Shared FastAPI dependencies — xác thực quyền Admin.
Dùng API Key qua header X-Admin-Email và X-Admin-Key.
Key được đặt trong .env (ADMIN_SECRET_KEY).
"""
from typing import Optional
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.services import user_service
from app.config import settings


async def require_admin(
    x_admin_email: Optional[str] = Header(None, description="Email admin"),
    x_admin_key: Optional[str] = Header(None, description="Admin secret key"),
    db: Session = Depends(get_db),
):
    """
    Dependency kiểm tra quyền Admin ở Backend:
    1. Kiểm tra có header X-Admin-Email và X-Admin-Key không.
    2. Kiểm tra X-Admin-Key có khớp với ADMIN_SECRET_KEY không.
    3. Kiểm tra user có tồn tại trong database và is_admin == True không.
    """
    if not x_admin_email or not x_admin_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Thiếu thông tin xác thực Admin (yêu cầu X-Admin-Email và X-Admin-Key)",
        )

    # Kiểm tra secret key
    if not settings.ADMIN_SECRET_KEY or x_admin_key != settings.ADMIN_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin secret key không chính xác hoặc không có quyền truy cập",
        )

    # Kiểm tra user tồn tại và là admin
    user = user_service.get_user_by_email(db, x_admin_email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Tài khoản {x_admin_email} không tồn tại trong hệ thống",
        )

    if not getattr(user, "is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản không được cấp quyền Quản trị viên (is_admin = False)",
        )

    return user
