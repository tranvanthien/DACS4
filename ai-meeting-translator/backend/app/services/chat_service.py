"""Chat service — lưu và truy vấn tin nhắn chat phòng họp."""
from sqlalchemy.orm import Session
from app.models.chat import ChatMessage


def save_message(db: Session, room_id: int, user_id: int, content: str) -> ChatMessage:
    msg = ChatMessage(room_id=room_id, user_id=user_id, content=content)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def get_room_messages(db: Session, room_id: int, limit: int = 100) -> list[ChatMessage]:
    """Trả 100 tin nhắn gần nhất của phòng, theo thứ tự tăng dần."""
    return (
        db.query(ChatMessage)
        .filter(ChatMessage.room_id == room_id)
        .order_by(ChatMessage.sent_at.asc())
        .limit(limit)
        .all()
    )
