"""
app/database.py

SQLAlchemy engine and session factory for the mithravoice database.

Usage:
    from app.database import get_db, Base

    # in a FastAPI route:
    def route(db: Session = Depends(get_db)): ...
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """
    get_db()
    Usage: FastAPI dependency — `db: Session = Depends(get_db)`. Yields a
    session for the request's lifetime and always closes it afterward,
    even if the route raises.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
