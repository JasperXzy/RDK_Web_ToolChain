from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def migrate_database(database_url: str, config_path: Path) -> None:
    config = Config(str(config_path))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def create_database(database_url: str) -> Engine:
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
    )

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    Base.metadata.create_all(engine)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
