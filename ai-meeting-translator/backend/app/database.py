from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import settings

_connect_args = {"check_same_thread": False} if settings.DB_SQLITE else {}
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    connect_args=_connect_args,
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
