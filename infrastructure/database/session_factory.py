# infrastructure/database/session_factory.py
from __future__ import annotations

import threading

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


class SessionFactory:
    def __init__(
        self,
        database_url: str,
        admin_database_url: str,
        target_database_name: str,
    ) -> None:
        self._database_url = database_url
        self._admin_database_url = admin_database_url
        self._target_database_name = target_database_name
        self._lock = threading.RLock()
        self._engine: Engine | None = None
        self._sessionmaker: sessionmaker[Session] | None = None
        self._generation = 0
        self.ensure_database_and_engine()

    def ensure_database_and_engine(self) -> None:
        with self._lock:
            database_created = self._ensure_database_exists()
            if self._engine is None or self._sessionmaker is None:
                self._rebuild_engine()
                return

            if database_created:
                self._rebuild_engine()

    def _ensure_database_exists(self) -> bool:
        admin_engine = create_engine(
            self._admin_database_url,
            future=True,
            isolation_level="AUTOCOMMIT",
            pool_pre_ping=True,
        )
        try:
            exists_sql = text(
                "SELECT 1 FROM pg_database WHERE datname = :database_name"
            )
            with admin_engine.connect() as connection:
                exists = connection.execute(
                    exists_sql,
                    {"database_name": self._target_database_name},
                ).scalar()
                if exists:
                    return False

                safe_db_name = self._target_database_name.replace('"', '""')
                connection.execute(text(f'CREATE DATABASE "{safe_db_name}"'))
                return True
        finally:
            admin_engine.dispose()

    def _rebuild_engine(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

        self._engine = self._build_engine(self._database_url)
        self._sessionmaker = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            future=True,
        )
        self._generation += 1

    @staticmethod
    def _build_engine(database_url: str) -> Engine:
        return create_engine(
            database_url,
            future=True,
            pool_pre_ping=True,
        )

    @property
    def engine(self) -> Engine:
        self.ensure_database_and_engine()
        assert self._engine is not None
        return self._engine

    @property
    def generation(self) -> int:
        return self._generation

    def create_session(self) -> Session:
        self.ensure_database_and_engine()
        assert self._sessionmaker is not None
        return self._sessionmaker()
