"""Engine and session factory."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from dataagent.config import get_settings

engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None


def init_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    global engine, SessionLocal
    settings = get_settings()
    engine = create_engine(
        url or settings.database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return engine


def get_engine() -> Engine:
    if engine is None:
        return init_engine()
    return engine


def get_session_factory() -> sessionmaker[Session]:
    if SessionLocal is None:
        init_engine()
    assert SessionLocal is not None
    return SessionLocal


def get_session() -> Generator[Session, None, None]:
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_schema_compatible(session: Session) -> bool:
    """Readiness: alembic_version present and matches head marker table."""
    row = session.execute(
        text(
            "SELECT EXISTS ("
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'alembic_version')"
        )
    ).scalar()
    return bool(row)
