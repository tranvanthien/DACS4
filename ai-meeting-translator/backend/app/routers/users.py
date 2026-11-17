from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, Field
import os, uuid, shutil

from app.database import get_db
from app.schemas.user import UserResponse
from app.services import user_service
from app.services.language_service import is_supported_language, get_all_languages
from app.dependencies import get_current_user
from app.models.user import User

router = APIRouter(prefix="/api", tags=["Users"])

AVATAR_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "avatars"))
os.makedirs(AVATAR_DIR, exist_ok=True)
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_SIZE = 5 * 1024 * 1024  # 5MB


class LanguageUpdate(BaseModel):
    speaking_language: str
    listening_language: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=6, max_length=128)


@router.get("/users/{user_id}", response_model=UserResponse)
def get_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.id != user_id and not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Không có quyền xem thông tin tài khoản khác")

    user = user_service.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy user id={user_id}")
    return user


@router.put("/users/{user_id}/languages")
def update_languages(
    user_id: int,
    data: LanguageUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.id != user_id and not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Không có quyền thay đổi cài đặt của tài khoản khác")

    if not is_supported_language(data.speaking_language):
        raise HTTPException(status_code=400, detail=f"Unsupported language: {data.speaking_language}")
    if not is_supported_language(data.listening_language):
        raise HTTPException(status_code=400, detail=f"Unsupported language: {data.listening_language}")

    user = user_service.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User không tồn tại")

    user.speaking_language = data.speaking_language
    user.listening_language = data.listening_language
    db.commit()
    db.refresh(user)
    return {
        "user_id": user.id,
        "speaking_language": user.speaking_language,
        "listening_language": user.listening_language,
    }


@router.put("/users/{user_id}/password")
def change_password(
    user_id: int,
    data: PasswordChange,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Người dùng tự đổi mật khẩu — chỉ cho phép chính chủ thao tác."""
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Không có quyền đổi mật khẩu của tài khoản khác")

    user = user_service.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User không tồn tại")

    if not user_service.verify_password(data.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Mật khẩu hiện tại không đúng")

    user_service.change_user_password(db, user, data.new_password)
    return {"message": "Đổi mật khẩu thành công"}


@router.get("/languages")
def get_languages():
    return get_all_languages()


@router.post("/users/{user_id}/avatar")
async def upload_avatar(
    user_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Không có quyền đổi avatar của tài khoản khác")

    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Chỉ hỗ trợ ảnh JPG, PNG, WebP, GIF")

    contents = await file.read()
    if len(contents) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="Ảnh tối đa 5MB")

    # Xóa avatar cũ nếu có
    user = user_service.get_user_by_id(db, user_id)
    if user.avatar_url:
        old_filename = user.avatar_url.split("/avatars/")[-1]
        old_path = os.path.join(AVATAR_DIR, old_filename)
        if os.path.isfile(old_path):
            os.remove(old_path)

    # Lưu file mới
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "jpg"
    filename = f"{user_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(AVATAR_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(contents)

    avatar_url = f"/avatars/{filename}"
    user.avatar_url = avatar_url
    db.commit()

    return {"avatar_url": avatar_url}
