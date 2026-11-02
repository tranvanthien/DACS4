from sqlalchemy.orm import Session
from app.models.user import User
from app.schemas.user import UserCreate
import bcrypt

def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))

def get_user_by_email(db: Session, email: str):
    return db.query(User).filter(User.email == email).first()

def get_user_by_id(db: Session, user_id: int):
    return db.query(User).filter(User.id == user_id).first()

def get_all_users(db: Session):
    return db.query(User).all()

def create_user(db: Session, user_data: UserCreate):
    hashed = hash_password(user_data.password)
    new_user = User(
        full_name=user_data.full_name,
        email=user_data.email,
        password_hash=hashed,
        speaking_language=user_data.speaking_language,
        listening_language=user_data.listening_language,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


def get_users_paged(db: Session, search: str = None, language: str = None, role: str = None, limit: int = 50, offset: int = 0):
    query = db.query(User)
    if search:
        s = f"%{search}%"
        query = query.filter((User.full_name.ilike(s)) | (User.email.ilike(s)))
    if language:
        query = query.filter((User.speaking_language == language) | (User.listening_language == language))
    if role == "admin":
        query = query.filter(User.is_admin == True)
    elif role == "user":
        query = query.filter(User.is_admin == False)
    total = query.count()
    users = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()
    return users, total


def update_user(db: Session, user: User, full_name: str = None, email: str = None, speaking_language: str = None, listening_language: str = None, is_admin: bool = None):
    if full_name is not None:
        user.full_name = full_name
    if email is not None:
        user.email = email
    if speaking_language is not None:
        user.speaking_language = speaking_language
    if listening_language is not None:
        user.listening_language = listening_language
    if is_admin is not None:
        user.is_admin = is_admin
    db.commit()
    db.refresh(user)
    return user


def change_user_password(db: Session, user: User, new_password: str):
    user.password_hash = hash_password(new_password)
    db.commit()
    db.refresh(user)
    return user


def delete_user(db: Session, user: User):
    from app.models.room import Room, RoomMember
    # Delete user's memberships
    db.query(RoomMember).filter(RoomMember.user_id == user.id).delete()
    # Close or reassign rooms hosted by user
    hosted_rooms = db.query(Room).filter(Room.host_id == user.id).all()
    for hr in hosted_rooms:
        hr.is_active = False
    db.delete(user)
    db.commit()
