"""스키마 제약과 왕복 — CONNECTOR_TEST_DATABASE_URL 설정 시에만 실행 (connector-hub#1).

마이그레이션이 만든 CHECK 가 실제로 잘못된 값을 막는지 본다. CHECK 는 조용히 빠지기 쉬운
자리라(오타 하나로 항상 참이 되는 식이 된다) DB 에 직접 물어보지 않으면 확인할 수 없다.

실행:
    createdb connector_hub_test
    DATABASE_URL=postgresql://.../connector_hub_test \\
      uv run alembic -c ../../migrations/alembic.ini upgrade head
    CONNECTOR_TEST_DATABASE_URL=postgresql://.../connector_hub_test uv run pytest -q
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(
    not os.getenv("CONNECTOR_TEST_DATABASE_URL"),
    reason="CONNECTOR_TEST_DATABASE_URL not set",
)


def _url() -> str:
    raw = os.environ["CONNECTOR_TEST_DATABASE_URL"]
    for prefix in (
        "postgresql+asyncpg://",
        "postgresql+psycopg://",
        "postgresql://",
        "postgres://",
    ):
        if raw.startswith(prefix):
            return "postgresql+asyncpg://" + raw[len(prefix) :]
    return raw


@pytest.fixture
async def session():
    engine = create_async_engine(_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
        await s.rollback()
    async with factory() as s:
        await s.execute(text("DELETE FROM connectors WHERE creator_user_id = 'schema-test'"))
        await s.commit()
    await engine.dispose()


_INSERT = text("""
    INSERT INTO connectors (
        connector_id, name, short_description, category, source_repo_url,
        endpoint_url, transport, scope_type, scope_id,
        creator_user_id, creator_team_id, verified_status
    ) VALUES (
        :cid, :name, 'desc', 'cat', 'https://repo.test/x',
        :endpoint, :transport, :scope_type, :scope_id,
        'schema-test', 'team-1', :verified
    )
""")


def _params(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "cid": str(uuid.uuid4()),
        "name": "c1",
        "endpoint": "https://mcp.test/sse",
        "transport": "http",
        "scope_type": "team",
        "scope_id": "team-1",
        "verified": None,
    }
    base.update(over)
    return base


async def test_insert_and_read_back(session) -> None:
    cid = str(uuid.uuid4())
    await session.execute(_INSERT, _params(cid=cid))
    await session.commit()
    row = (
        (
            await session.execute(
                text("SELECT * FROM connectors WHERE connector_id = :c"), {"c": cid}
            )
        )
        .mappings()
        .one()
    )
    assert row["name"] == "c1"
    assert row["compatible_hosts"] == ["claude_code", "opencode"]
    assert row["short_id"] and len(row["short_id"]) == 12, "AgentToolbox 와 같은 생성식"
    assert row["archived_at"] is None
    assert row["search_tsv"], "생성 컬럼이 채워져야 검색이 인덱스를 탄다"


async def test_supplied_uuid_is_preserved(session) -> None:
    """이관은 기존 item_id 를 그대로 넣는다 — 새로 만들면 deep link 가 전부 깨진다."""
    cid = "11111111-2222-3333-4444-555555555555"
    await session.execute(_INSERT, _params(cid=cid))
    await session.commit()
    got = (
        await session.execute(
            text("SELECT connector_id::text FROM connectors WHERE connector_id = :c"), {"c": cid}
        )
    ).scalar_one()
    assert got == cid


@pytest.mark.parametrize("transport", ["stdio", "grpc", ""])
async def test_transport_domain_rejects_non_remote(session, transport: str) -> None:
    """stdio 는 비목표다. 값을 받아 두면 '이미 데이터가 있으니' 로 지원 논의가 되살아난다."""
    with pytest.raises(IntegrityError):
        await session.execute(_INSERT, _params(transport=transport))
        await session.commit()


async def test_endpoint_anchor_blocks_a_card_nobody_can_use(session) -> None:
    with pytest.raises(IntegrityError):
        await session.execute(_INSERT, _params(endpoint=None, transport="http"))
        await session.commit()


async def test_transport_may_be_absent_before_the_endpoint_is_known(session) -> None:
    """anchor 는 transport 가 정해진 뒤에만 건다 — 초안 상태를 막는 제약이 아니다."""
    await session.execute(_INSERT, _params(endpoint=None, transport=None))
    await session.commit()


@pytest.mark.parametrize(
    "scope_type,scope_id",
    [("global", "team-1"), ("team", None), ("public", None)],
)
async def test_scope_consistency(session, scope_type: str, scope_id: str | None) -> None:
    with pytest.raises(IntegrityError):
        await session.execute(_INSERT, _params(scope_type=scope_type, scope_id=scope_id))
        await session.commit()


async def test_verified_status_domain(session) -> None:
    with pytest.raises(IntegrityError):
        await session.execute(_INSERT, _params(verified="probably-fine"))
        await session.commit()


async def test_short_id_is_unique(session) -> None:
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    await session.execute(_INSERT, _params(cid=a))
    await session.commit()
    short = (
        await session.execute(
            text("SELECT short_id FROM connectors WHERE connector_id = :c"), {"c": a}
        )
    ).scalar_one()
    await session.execute(_INSERT, _params(cid=b))
    with pytest.raises(IntegrityError):
        await session.execute(
            text("UPDATE connectors SET short_id = :s WHERE connector_id = :c"),
            {"s": short, "c": b},
        )
        await session.commit()


async def test_child_rows_cascade_on_delete(session) -> None:
    """카드를 지우면 딸린 행이 남지 않아야 한다 — orphan 은 이관 검증에서 0건이어야 한다."""
    cid = str(uuid.uuid4())
    await session.execute(_INSERT, _params(cid=cid))
    await session.execute(
        text("INSERT INTO connector_tags (connector_id, tag) VALUES (:c, 'mcp')"), {"c": cid}
    )
    await session.execute(
        text(
            "INSERT INTO connector_comments (connector_id, author_id, body) "
            "VALUES (:c, 'u1', 'hello')"
        ),
        {"c": cid},
    )
    await session.execute(
        text("INSERT INTO connector_stars (connector_id, user_id) VALUES (:c, 'u1')"), {"c": cid}
    )
    await session.execute(
        text("INSERT INTO connector_health_checks (connector_id) VALUES (:c)"), {"c": cid}
    )
    await session.commit()

    await session.execute(text("DELETE FROM connectors WHERE connector_id = :c"), {"c": cid})
    await session.commit()

    for table in (
        "connector_tags",
        "connector_comments",
        "connector_stars",
        "connector_health_checks",
    ):
        left = (
            await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE connector_id = :c"), {"c": cid}
            )
        ).scalar_one()
        assert left == 0, table


async def test_star_is_idempotent_by_primary_key(session) -> None:
    """토글을 UPDATE 로 구현하면 동시 요청이 서로를 덮는다. PK 가 곧 멱등성이다."""
    cid = str(uuid.uuid4())
    await session.execute(_INSERT, _params(cid=cid))
    await session.execute(
        text("INSERT INTO connector_stars (connector_id, user_id) VALUES (:c, 'u1')"), {"c": cid}
    )
    await session.commit()
    with pytest.raises(IntegrityError):
        await session.execute(
            text("INSERT INTO connector_stars (connector_id, user_id) VALUES (:c, 'u1')"),
            {"c": cid},
        )
        await session.commit()


async def test_health_status_domain(session) -> None:
    cid = str(uuid.uuid4())
    await session.execute(_INSERT, _params(cid=cid))
    await session.commit()
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO connector_health_checks (connector_id, health_status) "
                "VALUES (:c, 'probably-up')"
            ),
            {"c": cid},
        )
        await session.commit()
