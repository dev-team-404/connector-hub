"""ConnectorHub 초기 스키마 (connector-hub#1)

AgentToolbox 의 `items`(다형성 공통) + `mcp_servers`(subtype) 두 테이블을 **하나의
aggregate** 로 합친다. 다형성이 필요 없고, `items` 를 흉내 내면 거기 붙어 있던 Skill 전용
컬럼(서빙 버전·플러그인 슬러그·아이콘·스캔 결과)까지 따라온다.

`connector_id` 는 이관 시 기존 `items.item_id` 를 그대로 받는다. 그래서 기본값을 두되
insert 시 명시 지정을 허용한다 — deep link 와 알림 참조가 깨지면 안 된다.

Revision ID: 0001
Revises:
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# AgentToolbox 와 같은 생성식(마이그레이션 0064). 이관해 온 카드의 short_id 를 그대로
# 보존하면서 신규 카드도 같은 모양을 갖게 한다 — 두 세대가 섞여도 사람이 구분하지 못한다.
_SHORT_ID_EXPR = "translate(encode(gen_random_bytes(9), 'base64'), '+/', '-_')"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ---- 사용자·팀 projection ---------------------------------------------------------
    # 둘 다 **표시용 사본**이다. 권한 판정의 축이 아니다 — 판정은 introspection 이 주는
    # team_codes 로 한다. 여기에 FK 를 걸지 않는 것도 그래서다: 원장이 이 DB 에 없으므로
    # FK 는 "우리가 아직 못 본 사용자" 를 막는 잘못된 제약이 된다.
    op.execute("""
        CREATE TABLE connector_users (
            user_id      TEXT        PRIMARY KEY,
            display_name TEXT,
            email        TEXT,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE teams_projection (
            team_code  TEXT        PRIMARY KEY,
            name_kr    TEXT,
            name_en    TEXT,
            active     BOOLEAN     NOT NULL DEFAULT TRUE,
            synced_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # ---- connectors -------------------------------------------------------------------
    op.execute(f"""
        CREATE TABLE connectors (
            connector_id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            short_id                TEXT        NOT NULL DEFAULT {_SHORT_ID_EXPR},

            name                    TEXT        NOT NULL,
            title                   TEXT,
            short_description       TEXT        NOT NULL,
            description             TEXT,
            category                TEXT        NOT NULL,
            license                 TEXT,

            source_repo_url         TEXT        NOT NULL,
            endpoint_url            TEXT,
            transport               TEXT,
            manifest                JSONB,
            compatible_hosts        TEXT[]      NOT NULL
                                                DEFAULT ARRAY['claude_code','opencode']::TEXT[],
            claude_config_json      TEXT,
            opencode_config_json    TEXT,

            scope_type              TEXT        NOT NULL DEFAULT 'team',
            scope_id                TEXT,

            creator_user_id         TEXT        NOT NULL,
            creator_team_id         TEXT        NOT NULL,
            creator_organization_id TEXT,

            verified_status         TEXT,
            verified_at             TIMESTAMPTZ,
            archived_at             TIMESTAMPTZ,

            created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

            search_tsv tsvector GENERATED ALWAYS AS (
                setweight(to_tsvector('simple', coalesce(name, '')), 'A')
                || setweight(to_tsvector('simple', coalesce(short_description, '')), 'B')
            ) STORED,

            -- transport 는 remote 둘뿐이다. stdio 는 비목표라 도메인에서도 뺀다 —
            -- 값을 받아 두면 언젠가 "이미 데이터가 있으니" 로 지원 논의가 되살아난다.
            CONSTRAINT connectors_transport_domain
                CHECK (transport IS NULL OR transport IN ('http', 'sse')),
            -- 최소 anchor. transport 가 정해졌으면 접속 주소가 있어야 한다 — 둘 다 없는
            -- 카드는 등록만 되고 아무도 쓸 수 없다.
            CONSTRAINT connectors_endpoint_anchor
                CHECK (transport IS NULL OR endpoint_url IS NOT NULL),
            CONSTRAINT connectors_scope_consistency CHECK (
                (scope_type = 'global' AND scope_id IS NULL)
                OR (scope_type = 'team' AND scope_id IS NOT NULL)
            ),
            CONSTRAINT connectors_scope_type_domain
                CHECK (scope_type IN ('global', 'team')),
            CONSTRAINT connectors_verified_status_domain CHECK (
                verified_status IS NULL
                OR verified_status IN ('verified', 'flagged', 'failed', 'unverified')
            )
        )
    """)
    op.execute("CREATE UNIQUE INDEX connectors_short_id_key ON connectors (short_id)")
    op.execute("CREATE INDEX connectors_creator_idx ON connectors (creator_user_id)")
    op.execute("CREATE INDEX connectors_search_idx ON connectors USING GIN (search_tsv)")
    # 목록 기본 정렬. archive 된 카드는 목록에서 빠지므로 부분 인덱스로 좁힌다.
    op.execute(
        "CREATE INDEX connectors_recent_idx ON connectors (created_at DESC, connector_id) "
        "WHERE archived_at IS NULL"
    )

    # ---- 태그 · 공개범위 ---------------------------------------------------------------
    op.execute("""
        CREATE TABLE connector_tags (
            connector_id UUID NOT NULL REFERENCES connectors(connector_id) ON DELETE CASCADE,
            tag          TEXT NOT NULL,
            PRIMARY KEY (connector_id, tag)
        )
    """)
    op.execute("CREATE INDEX connector_tags_tag_idx ON connector_tags (tag)")
    op.execute("""
        CREATE TABLE connector_visibility_teams (
            connector_id UUID NOT NULL REFERENCES connectors(connector_id) ON DELETE CASCADE,
            team_code    TEXT NOT NULL,
            PRIMARY KEY (connector_id, team_code)
        )
    """)
    op.execute(
        "CREATE INDEX connector_visibility_team_idx ON connector_visibility_teams (team_code)"
    )

    # ---- 댓글 · 반응 ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE connector_comments (
            comment_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            connector_id UUID        NOT NULL REFERENCES connectors(connector_id) ON DELETE CASCADE,
            parent_id    UUID        REFERENCES connector_comments(comment_id) ON DELETE CASCADE,
            author_id    TEXT        NOT NULL,
            body         TEXT        NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at   TIMESTAMPTZ,
            CONSTRAINT connector_comments_no_self_parent CHECK (parent_id <> comment_id)
        )
    """)
    op.execute(
        "CREATE INDEX connector_comments_thread_idx "
        "ON connector_comments (connector_id, created_at)"
    )

    # 별·북마크는 (카드, 사용자) 유일이라 PK 가 곧 멱등성이다. 토글을 UPDATE 로 구현하면
    # 동시 요청이 서로를 덮으므로, insert/delete 로만 다룬다.
    for table in ("connector_stars", "connector_bookmarks"):
        op.execute(f"""
            CREATE TABLE {table} (
                connector_id UUID        NOT NULL
                                         REFERENCES connectors(connector_id) ON DELETE CASCADE,
                user_id      TEXT        NOT NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (connector_id, user_id)
            )
        """)
        op.execute(f"CREATE INDEX {table}_user_idx ON {table} (user_id, created_at DESC)")

    # ---- 알림 --------------------------------------------------------------------------
    # AgentToolbox 알림과 합산하지 않는다(설계 §12). 두 화면의 알림 메뉴는 같은 모양이지만
    # 각자 자기 것만 보여 준다.
    op.execute("""
        CREATE TABLE connector_notifications (
            notification_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         TEXT        NOT NULL,
            connector_id    UUID        REFERENCES connectors(connector_id) ON DELETE CASCADE,
            kind            TEXT        NOT NULL,
            payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
            read_at         TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX connector_notifications_inbox_idx "
        "ON connector_notifications (user_id, created_at DESC)"
    )

    # ---- tools 캐시 · health ------------------------------------------------------------
    # AgentToolbox 는 이 값들을 카드 행의 컬럼으로 갖는다. 그러면 5분 주기 liveness cron 이
    # 카드 본문 행을 계속 UPDATE 한다 — 사람이 쓰는 값과 워커가 쓰는 값의 갱신 주기가 달라
    # 행 잠금과 dead tuple 을 서로 물려 준다. 그래서 1:1 로 분리한다.
    op.execute("""
        CREATE TABLE connector_tools_cache (
            connector_id UUID        PRIMARY KEY
                                     REFERENCES connectors(connector_id) ON DELETE CASCADE,
            tools        JSONB,
            fetched_at   TIMESTAMPTZ,
            fetch_error  TEXT
        )
    """)
    op.execute("""
        CREATE TABLE connector_health_checks (
            connector_id         UUID        PRIMARY KEY
                                             REFERENCES connectors(connector_id) ON DELETE CASCADE,
            health_status        TEXT        NOT NULL DEFAULT 'unknown',
            last_checked_at      TIMESTAMPTZ,
            consecutive_failures INTEGER     NOT NULL DEFAULT 0,
            last_error           TEXT,
            CONSTRAINT connector_health_status_domain
                CHECK (health_status IN ('unknown', 'healthy', 'unhealthy'))
        )
    """)
    # 다음 검사 대상을 고르는 축. 오래 안 본 것부터 본다.
    op.execute(
        "CREATE INDEX connector_health_due_idx "
        "ON connector_health_checks (last_checked_at NULLS FIRST)"
    )


def downgrade() -> None:
    for table in (
        "connector_health_checks",
        "connector_tools_cache",
        "connector_notifications",
        "connector_bookmarks",
        "connector_stars",
        "connector_comments",
        "connector_visibility_teams",
        "connector_tags",
        "connectors",
        "teams_projection",
        "connector_users",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    # 확장은 지우지 않는다 — 다른 스키마가 쓰고 있을 수 있고, 되돌리기의 목적은 이 스키마를
    # 걷어내는 것이지 데이터베이스를 처음 상태로 만드는 것이 아니다.
