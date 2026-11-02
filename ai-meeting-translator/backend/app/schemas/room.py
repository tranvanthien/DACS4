from pydantic import BaseModel
from datetime import datetime

class RoomCreate(BaseModel):
    room_name: str

class RoomResponse(BaseModel):
    id: int
    room_code: str
    room_name: str
    host_id: int
    created_at: datetime
    is_active: bool

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
