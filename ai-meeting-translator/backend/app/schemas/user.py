from pydantic import BaseModel, EmailStr
from datetime import datetime

class UserCreate(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    speaking_language: str
    listening_language: str

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

    class Config:
        from_attributes = True  # cho phép đọc từ SQLAlchemy model
