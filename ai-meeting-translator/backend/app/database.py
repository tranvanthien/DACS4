from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import settings

_engine_kwargs = {
    "connect_args": {"check_same_thread": False} if settings.DB_SQLITE else {},
    "pool_pre_ping": True,
}

# Cấu hình Connection Pool cho production khi dùng MySQL
if not settings.DB_SQLITE:
    _engine_kwargs.update({
        "pool_size": 10,
        "max_overflow": 20,
        "pool_recycle": 3600,
        "pool_timeout": 30,
    })

engine = create_engine(
    settings.DATABASE_URL,
    **_engine_kwargs,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


# Dependency dùng trong các router
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
