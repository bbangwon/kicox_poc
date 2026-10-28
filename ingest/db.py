"""PostgreSQL 접속과 마이그레이션 (db/migrations/*.sql을 이름 순으로 한 번씩 적용)."""

import logging

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ingest import config

log = logging.getLogger(__name__)
MIGRATIONS = config.ROOT / "db" / "migrations"


def connect() -> psycopg.Connection:
    return psycopg.connect(config.DATABASE_URL)


def ensure_database() -> None:
    """대상 DB가 없으면 같은 서버의 postgres DB로 접속해 만든다 (공용 컨테이너의 다른 DB는 건드리지 않음)."""
    params = conninfo_to_dict(config.DATABASE_URL)
    name = params.get("dbname")
    with psycopg.connect(make_conninfo(config.DATABASE_URL, dbname="postgres"), autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            log.info("DB 생성: %s", name)


def migrate() -> list[str]:
    ensure_database()
    applied = []
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.stem in done:
                continue
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.stem,))
            applied.append(f.stem)
            log.info("마이그레이션 적용: %s", f.stem)
    return applied
