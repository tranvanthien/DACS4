"""Chat router — REST API để load lịch sử tin nhắn."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import room_service, user_service, chat_service
from app.dependencies import get_current_user
from app.models.user import User

router = APIRouter(tags=["Chat"])


@router.get("/api/rooms/{room_code}/messages")
def get_messages(
    room_code: str,
    user_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lấy lịch sử chat — chỉ thành viên hoặc host của phòng mới được xem."""
    room = room_service.get_room_by_code(db, room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Phòng không tồn tại")

    # Kiểm tra quyền: là host, là thành viên, hoặc admin
    is_host = room.host_id == current_user.id
    member = room_service.get_room_member(db, room.id, current_user.id)
    is_admin = getattr(current_user, "is_admin", False)

    if not is_host and not member and not is_admin:
        raise HTTPException(status_code=403, detail="Bạn không phải thành viên phòng này")

    messages = chat_service.get_room_messages(db, room.id)
    result = []
    for m in messages:
        user = user_service.get_user_by_id(db, m.user_id)
        result.append({
            "id": m.id,
            "user_id": m.user_id,
            "user_name": user.full_name if user else f"User {m.user_id}",
            "content": m.content,
            "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        })
    return result
