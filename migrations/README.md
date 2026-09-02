# migrations

Connector DB 의 alembic 마이그레이션. **AgentToolbox 의 alembic 과 완전히 별개다** — 리비전 번호 공간도 공유하지 않는다.

`connectors` 를 포함한 11개 테이블을 이 저장소가 소유한다. AgentToolbox 의 `items`/`mcp_servers` 를 참조하는 FK 는 만들지 않는다.

```bash
cd apps/api
export DATABASE_URL=postgresql://<user>@localhost:5432/connector_hub
uv run alembic -c ../../migrations/alembic.ini upgrade head
uv run alembic -c ../../migrations/alembic.ini heads     # 항상 하나여야 한다
```

## 규칙

- **autogenerate 를 쓰지 않는다.** 마이그레이션은 손으로 쓴다 — 생성된 diff 는 왜 그 변경이 필요한지를 남기지 못하고, 이 저장소의 스키마는 이관 제약(기존 UUID 보존 등)을 함께 지켜야 한다.
- head 는 항상 하나다. CI 가 검사한다.
- `downgrade` 를 반드시 채운다. 되돌릴 수 없는 변경이면 그 사실을 docstring 에 적는다.
