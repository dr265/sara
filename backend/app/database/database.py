from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from app.core.config import settings

DATABASE_URL = settings.DATABASE_URL or settings.SARA_DB_URL or (
    f"sqlite:///{Path(__file__).resolve().parents[2] / 'sara.db'}"
)
if DATABASE_URL.startswith("sqlite:///./"):
    relative_path = DATABASE_URL.removeprefix("sqlite:///./")
    DATABASE_URL = f"sqlite:///{Path(__file__).resolve().parents[2] / relative_path}"

connect_args = {"timeout": 30} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)

# SQLite concurrency: the WhatsApp sheets/knowledge stores already run in WAL
# mode; put the main engine on the same page (WAL + busy timeout) so background
# workers, pollers and API writes never fail with "database is locked".
if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()