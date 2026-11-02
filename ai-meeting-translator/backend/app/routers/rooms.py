from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from app.database import get_db
from app.schemas.room import RoomCreate, RoomResponse, RoomMemberResponse, UserRoomResponse
from app.services import room_service, user_service

router = APIRouter(tags=["Rooms"])
# ---------- Tạo phòng ----------
@router.post("/api/rooms", status_code=status.HTTP_201_CREATED)
def create_room(room_data: RoomCreate, host_id: int, db: Session = Depends(get_db)):
    user = user_service.get_user_by_id(db, host_id)
    if not user:
        raise HTTPException(status_code=404, detail="User không tồn tại")

    room = room_service.create_room(db, room_data.room_name, host_id)
    return {"message": "Room created successfully", "room": RoomResponse.model_validate(room)}

# ---------- Join phòng ----------
@router.post("/api/rooms/{room_code}/join")
def join_room(room_code: str, user_id: int, db: Session = Depends(get_db)):
    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Phòng không tồn tại")
    if not room.is_active:
        raise HTTPException(status_code=400, detail="Phòng đã đóng")

    user = user_service.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User không tồn tại")

    room_service.join_room(db, room, user_id)
    return {"message": "Joined room successfully"}

# ---------- Leave phòng ----------
@router.post("/api/rooms/{room_code}/leave")
async def leave_room(room_code: str, user_id: int, db: Session = Depends(get_db)):
    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Phòng không tồn tại")

    result = room_service.leave_room(db, room, user_id)
    if result is None:
        raise HTTPException(status_code=400, detail="User chưa tham gia hoặc đã rời phòng")

    # Host rời qua REST → đóng phòng và thông báo tất cả thành viên
    if room.host_id == user_id and room.is_active:
        room_service.close_room(db, room)
        try:
            from app.websocket.connection_manager import manager
            await manager.broadcast(room_code, {
                "type": "room_closed",
                "message": "Chủ phòng đã rời — phòng họp kết thúc",
            })
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"[Rooms] Failed to broadcast room_closed on host leave: {e}")
        return {"message": "Host left — room closed", "room_closed": True}

    return {"message": "Left room successfully", "room_closed": False}

# ---------- Danh sách tất cả phòng ----------
@router.get("/api/rooms", response_model=List[RoomResponse])
def get_all_rooms_public(db: Session = Depends(get_db)):
    rooms, _ = room_service.get_all_rooms(db, limit=100)
    return rooms

# ---------- Lấy thông tin phòng ----------
@router.get("/api/rooms/{room_code}", response_model=RoomResponse)
def get_room(room_code: str, db: Session = Depends(get_db)):
    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Phòng không tồn tại")
    return room

# ---------- Lấy danh sách thành viên đang active ----------
@router.get("/api/rooms/{room_code}/members", response_model=List[RoomMemberResponse])
def get_members(room_code: str, db: Session = Depends(get_db)):
    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Phòng không tồn tại")
    return room_service.get_active_members(db, room.id)

# ---------- Đóng phòng ----------
@router.post("/api/rooms/{room_code}/close")
async def close_room(room_code: str, host_id: int, db: Session = Depends(get_db)):
    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Phòng không tồn tại")
    if not room.is_active:
        raise HTTPException(status_code=400, detail="Phòng đã đóng rồi")
    if room.host_id != host_id:
        raise HTTPException(status_code=403, detail="Chỉ host mới được đóng phòng")

    room_service.close_room(db, room)

    # Broadcast sự kiện đóng phòng tới tất cả peer đang trong cuộc họp
    try:
        from app.websocket.connection_manager import manager
        await manager.broadcast(room_code, {
            "type": "room_closed",
            "message": "Chủ phòng đã kết thúc cuộc họp",
        })
    except Exception:
        pass

    return {"message": "Room closed successfully"}

# ---------- Danh sách phòng của user ----------
@router.get("/api/users/{user_id}/rooms", response_model=List[UserRoomResponse])
def get_user_rooms(user_id: int, db: Session = Depends(get_db)):
    user = user_service.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User không tồn tại")
    return room_service.get_user_rooms(db, user_id)
