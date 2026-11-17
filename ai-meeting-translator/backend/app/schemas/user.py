from pydantic import BaseModel, EmailStr, field_validator, Field
from datetime import datetime


class UserCreate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=128)
    speaking_language: str
    listening_language: str

    @field_validator("full_name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Tên không được để trống")
        return v.strip()

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    id: int
    full_name: str
    email: str
    speaking_language: str
    listening_language: str
    created_at: datetime
    is_admin: bool = False
    avatar_url: str | None = None

    class Config:
        from_attributes = True  # cho phép đọc từ SQLAlchemy model
