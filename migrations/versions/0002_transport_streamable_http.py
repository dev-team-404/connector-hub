"""transport 어휘를 MCP 스펙·상류 데이터와 맞춘다 (connector-hub#3)

0001 은 도메인을 `('http','sse')` 로 뒀다. 그런데 실제로 이 값을 소비하는 곳은 셋이고
셋 다 다른 이름을 쓴다:

- MCP SDK — `streamable_http` / `sse`
- AgentToolbox `mcp_servers.transport`(이관해 올 데이터, 마이그레이션 0135) — 같은 이름
- ConnectorHub DB — `http` / `sse`

두 어휘를 두면 번역이 필요한 자리가 셋이 된다(등록·이관·폴백 되기록). 그중 **폴백
되기록**이 위험하다 — liveness 는 실제로 성공한 transport 를 DB 에 다시 쓰는데, 거기서
번역이 한 번 빠지면 `streamable_http` 가 `('http','sse')` CHECK 에 부딪혀 워커가 밤중에
터진다. 번역을 없애는 쪽이 맞다.

`http` 라는 이름 자체도 나쁘다. sse 역시 HTTP 위에서 돌기 때문에 무엇을 가리키는지
구분하지 못한다.

0001 을 고치지 않고 새 리비전으로 올린다. 아직 어느 환경에도 배포되지 않았지만 0001 은
이미 main 에 있고, "머지된 마이그레이션은 덧붙이기만 한다" 는 규칙을 처음부터 깨면 나중에
데이터가 있을 때도 같은 판단을 하게 된다.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 제약을 먼저 떼야 UPDATE 가 통과한다.
    op.execute("ALTER TABLE connectors DROP CONSTRAINT connectors_transport_domain")
    op.execute("UPDATE connectors SET transport = 'streamable_http' WHERE transport = 'http'")
    op.execute(
        "ALTER TABLE connectors ADD CONSTRAINT connectors_transport_domain "
        "CHECK (transport IS NULL OR transport IN ('streamable_http', 'sse'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE connectors DROP CONSTRAINT connectors_transport_domain")
    op.execute("UPDATE connectors SET transport = 'http' WHERE transport = 'streamable_http'")
    op.execute(
        "ALTER TABLE connectors ADD CONSTRAINT connectors_transport_domain "
        "CHECK (transport IS NULL OR transport IN ('http', 'sse'))"
    )
