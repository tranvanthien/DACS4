from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
import logging

from app.database import get_db
from app.schemas.room import RoomCreate, RoomResponse, RoomMemberResponse, UserRoomResponse, RoomModeUpdate
from app.services import room_service, user_service
from app.dependencies import get_current_user, get_current_user_optional
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Rooms"])


# ---------- Tạo phòng ----------
@router.post("/api/rooms", status_code=status.HTTP_201_CREATED)
def create_room(
    room_data: RoomCreate,
    host_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Luôn lấy host_id từ current_user đã xác thực qua JWT token
    actual_host_id = current_user.id

    room = room_service.create_room(
        db,
        room_data.room_name,
        actual_host_id,
        meeting_mode=room_data.meeting_mode or "normal",
    )
    return {"message": "Room created successfully", "room": RoomResponse.model_validate(room)}


# ---------- Join phòng ----------
@router.post("/api/rooms/{room_code}/join")
def join_room(
    room_code: str,
    user_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    actual_user_id = current_user.id

    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Phòng không tồn tại")
    if not room.is_active:
        raise HTTPException(status_code=400, detail="Phòng đã đóng")

    room_service.join_room(db, room, actual_user_id)
    return {"message": "Joined room successfully"}


# ---------- Leave phòng ----------
@router.post("/api/rooms/{room_code}/leave")
async def leave_room(
    room_code: str,
    user_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    actual_user_id = current_user.id

    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Phòng không tồn tại")

    result = room_service.leave_room(db, room, actual_user_id)
    if result is None:
        raise HTTPException(status_code=400, detail="User chưa tham gia hoặc đã rời phòng")

    # Host rời qua REST → đóng phòng và thông báo tất cả thành viên
    if room.host_id == actual_user_id and room.is_active:
        room_service.close_room(db, room)
        try:
            from app.websocket.connection_manager import manager
            await manager.broadcast(room_code, {
                "type": "room_closed",
                "message": "Chủ phòng đã rời — phòng họp kết thúc",
            })
        except Exception as e:
            logger.warning(f"[Rooms] Failed to broadcast room_closed on host leave: {e}")
        return {"message": "Host left — room closed", "room_closed": True}

    return {"message": "Left room successfully", "room_closed": False}


# ---------- Danh sách phòng (yêu cầu đăng nhập) ----------
@router.get("/api/rooms", response_model=List[RoomResponse])
def get_all_rooms(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rooms, _ = room_service.get_all_rooms(db, limit=100)
    return rooms


# ---------- Lấy thông tin phòng ----------
@router.get("/api/rooms/{room_code}", response_model=RoomResponse)
def get_room(
    room_code: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Phòng không tồn tại")
    return room


# ---------- Đổi chế độ cuộc họp (Chỉ Host) ----------
@router.put("/api/rooms/{room_code}/mode")
async def change_room_mode(
    room_code: str,
    mode_data: RoomModeUpdate,
    host_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Phòng không tồn tại")
    if not room.is_active:
        raise HTTPException(status_code=400, detail="Phòng đã đóng")
    if room.host_id != current_user.id and not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Chỉ chủ phòng mới có quyền thay đổi chế độ cuộc họp")

    mode = "ai" if str(mode_data.meeting_mode).lower() in ("ai", "ai_translation", "translation") else "normal"
    room = room_service.update_room_mode(db, room, mode)

    mode_name = "Họp dịch thuật AI" if mode == "ai" else "Họp bình thường"
    try:
        from app.websocket.connection_manager import manager
        await manager.broadcast(room_code, {
            "type": "room_mode_changed",
            "meeting_mode": mode,
            "changed_by": current_user.id,
            "message": f"Chủ phòng đã chuyển chế độ cuộc họp sang: {mode_name}",
        })
    except Exception as e:
        logger.warning(f"[Rooms] Failed to broadcast room_mode_changed: {e}")

    return {
        "message": f"Đã chuyển sang {mode_name}",
        "meeting_mode": mode,
        "room": RoomResponse.model_validate(room),
    }


# ---------- Lấy danh sách thành viên đang active ----------
@router.get("/api/rooms/{room_code}/members", response_model=List[RoomMemberResponse])
def get_members(
    room_code: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Phòng không tồn tại")

    # Kiểm tra quyền: là host, là thành viên của phòng, hoặc là admin
    member = room_service.get_room_member(db, room.id, current_user.id)
    if room.host_id != current_user.id and not member and not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Bạn không có quyền xem thành viên phòng này")

    return room_service.get_active_members(db, room.id)


# ---------- Đóng phòng ----------
@router.post("/api/rooms/{room_code}/close")
async def close_room(
    room_code: str,
    host_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Phòng không tồn tại")
    if not room.is_active:
        raise HTTPException(status_code=400, detail="Phòng đã đóng rồi")
    if room.host_id != current_user.id and not getattr(current_user, "is_admin", False):
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
def get_user_rooms(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Chỉ cho phép chính chủ hoặc Admin xem lịch sử phòng
    if current_user.id != user_id and not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Không có quyền xem lịch sử phòng của người dùng khác")

    user = user_service.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User không tồn tại")
    return room_service.get_user_rooms(db, user_id)
