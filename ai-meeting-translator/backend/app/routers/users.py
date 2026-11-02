from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel

from app.database import get_db
from app.schemas.user import UserResponse
from app.services import user_service
from app.services.language_service import is_supported_language, get_all_languages

router = APIRouter(prefix="/api", tags=["Users"])


class LanguageUpdate(BaseModel):
    speaking_language: str
    listening_language: str


@router.get("/users", response_model=List[UserResponse])
def get_all_users(db: Session = Depends(get_db)):
    return user_service.get_all_users(db)


@router.get("/users/{user_id}", response_model=UserResponse)
def get_user(user_id: int, db: Session = Depends(get_db)):
    user = user_service.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy user id={user_id}")
    return user


@router.put("/users/{user_id}/languages")
def update_languages(user_id: int, data: LanguageUpdate, db: Session = Depends(get_db)):
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


@router.get("/languages")
def get_languages():
    return get_all_languages()
