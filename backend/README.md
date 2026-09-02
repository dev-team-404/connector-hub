# ConnectorHub 백엔드

단일 Python 프로젝트에 진입점이 둘이다.

| 진입점 | 무엇 | 명령 |
| --- | --- | --- |
| `src/api` | FastAPI. 외부 base path 는 `/connector/api/v1` | `uv run connector-hub-api` |
| `src/worker` | liveness cron · tools 캐시 갱신 (P3-2 에서 들어온다) | — |

둘이 `src/core`(도메인·설정·DB·사이트 인증)를 공유하므로 프로젝트를 나누지 않는다. **배포 이미지는 진입점마다 나눈다** — 코드를 공유하는 것과 함께 배포하는 것은 다르다.

저장소 전체 설명과 경계는 [루트 README](../README.md) 를 본다.

```bash
uv sync --extra dev
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q                # DB 없는 테스트만
uv run connector-hub-api
```

DB 를 쓰는 테스트는 실제 PostgreSQL 이 필요하다.

```bash
createdb connector_hub_test
export DATABASE_URL=postgresql://<user>@localhost:5432/connector_hub_test
uv run alembic -c ../migrations/alembic.ini upgrade head
CONNECTOR_TEST_DATABASE_URL=$DATABASE_URL uv run pytest -q
```
