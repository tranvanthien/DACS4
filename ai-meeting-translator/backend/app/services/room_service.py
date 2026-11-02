import random
import string
from sqlalchemy.orm import Session
from app.models.room import Room, RoomMember

def generate_room_code(db: Session, length: int = 6) -> str:
    """Tạo room_code ngẫu nhiên, đảm bảo không trùng."""
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=length))
        existing = db.query(Room).filter(Room.room_code == code).first()
        if not existing:
            return code

def get_room_by_code(db: Session, room_code: str):
    return db.query(Room).filter(Room.room_code == room_code).first()

def get_room_member(db: Session, room_id: int, user_id: int):
    return db.query(RoomMember).filter(
        RoomMember.room_id == room_id,
        RoomMember.user_id == user_id,
    ).first()

def get_active_members(db: Session, room_id: int):
    return db.query(RoomMember).filter(
        RoomMember.room_id == room_id,
        RoomMember.is_active == True,
    ).all()

def create_room(db: Session, room_name: str, host_id: int) -> Room:
    room_code = generate_room_code(db)
    room = Room(room_code=room_code, room_name=room_name, host_id=host_id, is_active=True)
    db.add(room)
    db.flush()  # lấy room.id trước khi commit

    # Thêm host vào room_members
    member = RoomMember(room_id=room.id, user_id=host_id, is_active=True)
    db.add(member)
    db.commit()
    db.refresh(room)
    return room

def join_room(db: Session, room: Room, user_id: int) -> RoomMember:
    member = get_room_member(db, room.id, user_id)
    if member:
        if not member.is_active:
            member.is_active = True
            db.commit()
            db.refresh(member)
        return member
    member = RoomMember(room_id=room.id, user_id=user_id, is_active=True)
    db.add(member)
    db.commit()
    db.refresh(member)
    return member

def leave_room(db: Session, room: Room, user_id: int):
    member = get_room_member(db, room.id, user_id)
    if not member or not member.is_active:
        return None
    member.is_active = False
    db.commit()
    return member

def close_room(db: Session, room: Room):
    room.is_active = False
    # Đặt tất cả member về inactive
    db.query(RoomMember).filter(
        RoomMember.room_id == room.id,
        RoomMember.is_active == True,
    ).update({"is_active": False})
    db.commit()

def get_user_rooms(db: Session, user_id: int):
    members = db.query(RoomMember).filter(RoomMember.user_id == user_id).all()
    result = []
    for m in members:
        room = db.query(Room).filter(Room.id == m.room_id).first()
        if room:
            result.append({
                "room_id": room.id,
                "room_code": room.room_code,
                "room_name": room.room_name,
                "host_id": room.host_id,
                "joined_at": m.joined_at,
                "is_active": room.is_active,
            })
    return result


def get_all_rooms(db: Session, search: str = None, status: str = None, limit: int = 50, offset: int = 0):
    query = db.query(Room)
    if search:
        s = f"%{search}%"
        query = query.filter((Room.room_name.ilike(s)) | (Room.room_code.ilike(s)))
    if status == "active":
        query = query.filter(Room.is_active == True)
    elif status == "closed":
        query = query.filter(Room.is_active == False)
    total = query.count()
    rooms = query.order_by(Room.created_at.desc()).offset(offset).limit(limit).all()
    return rooms, total


def delete_room(db: Session, room: Room):
    db.query(RoomMember).filter(RoomMember.room_id == room.id).delete()
    db.delete(room)
    db.commit()
