"""
FastAPI Dependencies — Xác thực và phân quyền người dùng (JWT) và quản trị viên (Admin).
"""
from typing import Optional
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.services import user_service
from app.core.security import decode_access_token
from app.config import settings

# HTTPBearer scheme cho OpenAPI docs và trích xuất header Authorization: Bearer <token>
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    Xác thực người dùng hiện tại qua JWT access token:
    1. Trích xuất Bearer token từ header Authorization.
    2. Giải mã và kiểm tra hạn sử dụng (exp), chữ ký hợp lệ.
    3. Lấy thông tin user từ database theo `sub` (user_id).
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Yêu cầu xác thực tài khoản. Vui lòng đăng nhập.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    payload = decode_access_token(token)
    user_id_raw = payload.get("sub")
    if not user_id_raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token không chứa thông tin định danh người dùng",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = int(user_id_raw)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Thông tin định danh trong token không hợp lệ",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = user_service.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tài khoản không tồn tại hoặc đã bị xóa khỏi hệ thống",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Dependency tùy chọn: trả về User nếu có token hợp lệ, ngược lại trả về None."""
    if not credentials or not credentials.credentials:
        return None
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload.get("sub", 0))
        return user_service.get_user_by_id(db, user_id)
    except Exception:
        return None


async def require_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    x_admin_email: Optional[str] = Header(None, description="Legacy email admin"),
    x_admin_key: Optional[str] = Header(None, description="Legacy admin secret key"),
    db: Session = Depends(get_db),
) -> User:
    """
    Dependency kiểm tra quyền Quản trị viên (Admin):
    1. Ưu tiên: Xác thực qua JWT Bearer Token, kiểm tra is_admin == True.
    2. Dự phòng (legacy test scripts): So khớp X-Admin-Key & X-Admin-Email nếu ADMIN_SECRET_KEY được cấu hình.
    """
    # 1. Thử xác thực qua JWT Bearer Token
    if credentials and credentials.credentials:
        try:
            payload = decode_access_token(credentials.credentials)
            user_id = int(payload.get("sub", 0))
            user = user_service.get_user_by_id(db, user_id)
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Tài khoản Admin không tồn tại trong hệ thống",
                )
            if not getattr(user, "is_admin", False):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Tài khoản không được cấp quyền Quản trị viên (is_admin = False)",
                )
            return user
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token Admin không hợp lệ",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # 2. Dự phòng legacy header: X-Admin-Email & X-Admin-Key
    if x_admin_email and x_admin_key:
        if settings.ADMIN_SECRET_KEY and x_admin_key == settings.ADMIN_SECRET_KEY:
            user = user_service.get_user_by_email(db, x_admin_email)
            if user and getattr(user, "is_admin", False):
                return user
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tài khoản không tồn tại hoặc không có quyền Quản trị viên",
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin secret key không chính xác",
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Yêu cầu xác thực quyền Quản trị viên (Bearer token hoặc thông tin hợp lệ)",
        headers={"WWW-Authenticate": "Bearer"},
    )
