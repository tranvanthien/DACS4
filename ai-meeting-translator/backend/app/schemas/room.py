from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class RoomCreate(BaseModel):
    room_name: str = Field(..., min_length=1, max_length=100)
    meeting_mode: Optional[str] = Field(default="normal", description="Chế độ phòng họp: 'normal' hoặc 'ai'")


class RoomModeUpdate(BaseModel):
    meeting_mode: str = Field(..., description="Chế độ mới: 'normal' hoặc 'ai'")


class RoomResponse(BaseModel):
    id: int
    room_code: str
    room_name: str
    host_id: int
    created_at: datetime
    is_active: bool
    meeting_mode: str = "normal"

    class Config:
        from_attributes = True


class RoomMemberResponse(BaseModel):
    id: int
    room_id: int
    user_id: int
    joined_at: datetime
    is_active: bool

    class Config:
        from_attributes = True


class UserRoomResponse(BaseModel):
    room_id: int
    room_code: str
    room_name: str
    host_id: int
    joined_at: datetime
    is_active: bool
    meeting_mode: str = "normal"
