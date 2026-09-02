"""Connector 모더레이터 (connector-hub#4)

댓글 삭제 권한을 작성자 밖으로 넓히려면 "이 서비스의 관리자" 라는 개념이 필요하다.

**사이트 JWT 의 `role` 을 쓰지 않는다.** 그 값은 AgentToolbox 가 발급하는 공통 역할이고
(계약 §2.1), 거기서 admin 이라는 것이 Connector 카탈로그를 조정해도 된다는 뜻은 아니다.
그 둘을 같은 것으로 취급하면 한쪽의 권한 부여가 다른 쪽 권한을 조용히 넓힌다 — 두 서비스의
담당이 갈리는 순간 그것은 사고가 된다.

그래서 판정 축을 **이 DB** 에 둔다. 부여·회수는 아직 화면이 없고 운영자가 SQL 로 넣는다.
관리 화면이 필요해지면 그때 만든다 — 지금 만들면 그 화면을 쓸 사람이 자기 자신뿐이다.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `connector_users` 에 컬럼을 더하지 않는다. 그 테이블은 **표시용 projection** 이라
    # 언제든 상류에서 다시 채워질 수 있고, 그때 권한이 함께 날아가면 안 된다.
    op.execute("""
        CREATE TABLE connector_moderators (
            user_id    TEXT        PRIMARY KEY,
            granted_by TEXT,
            note       TEXT,
            granted_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS connector_moderators")
