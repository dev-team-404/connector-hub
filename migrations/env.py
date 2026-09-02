"""Alembic 환경 — 동기 드라이버로 마이그레이션을 실행한다.

런타임은 asyncpg 를 쓰지만 마이그레이션은 psycopg(sync)로 돈다. alembic 의 트랜잭션·락
처리가 sync 경로에서 단순하고, 마이그레이션은 요청 경로가 아니라 성능도 문제되지 않는다.
`DATABASE_URL` 이 asyncpg 형태로 와도 여기서 sync 로 바꿔 받는다.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None  # 마이그레이션은 손으로 쓴다 — autogenerate 를 쓰지 않는다.


def _sync_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL 미설정 — 마이그레이션 대상 DB 를 알 수 없다")
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def run_migrations_offline() -> None:
    context.configure(url=_sync_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
