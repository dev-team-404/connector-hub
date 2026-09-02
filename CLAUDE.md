# CLAUDE.md

ConnectorHub — 사내 MCP 서버(Connector) 카탈로그. AgentToolbox 에서 분리된 독립 서비스다.

저장소가 무엇을 소유하고 무엇을 소유하지 않는지는 [README](README.md) 의 경계 표가 정본이다. 작업 전에 그것을 먼저 읽는다.

## 절대 하지 않는 것

이 셋을 어기면 분리가 남기는 이득(장애 격리·스키마 소유권·독립 릴리스)이 사라지고 비용만 남는다.

1. **AgentToolbox DB 를 직접 조회하지 않는다.** 같은 PostgreSQL 인스턴스라 해도 마찬가지다.
2. **AgentToolbox 의 Python/TypeScript 도메인 코드를 복사해 오지 않는다.** 옮기는 것은 도메인 로직이지 공유 모듈이 아니다.
3. **허용된 서버 간 호출은 셋뿐이다** — JWKS, introspection, 팀 Directory. 다른 목적으로 AgentToolbox 를 부르지 않는다. 필요가 생기면 계약 문서를 먼저 고친다.

## 계약

사이트 인증 계약의 정본은 AgentToolbox 의 [`docs/architecture/site-auth-contract.md`](https://github.com/dev-team-404/AgentToolbox/blob/main/docs/architecture/site-auth-contract.md) 다. 이 저장소는 **소비자**다.

- 계약이 바뀌면 AgentToolbox 가 먼저 배포되고 이쪽이 뒤따른다.
- `apps/api/tests/fixtures/site_jwt/vectors.json` 은 정본의 **사본**이다. 여기서 먼저 고치지 않는다 — 고치면 "우리 구현에 맞춘 벡터" 가 되어 계약으로서의 의미가 사라진다. CI 가 정본과 갈라졌는지 알린다.

## 한 사람이 양쪽을 만들 때

당분간 AgentToolbox 와 이 저장소를 같은 사람이 구현한다. 그래서 다음 둘을 명시적으로 경계한다.

- **경계 침범의 유혹.** 저쪽 DB 를 읽으면 당장은 편하다. 그 순간 분리가 무너진다.
- **동시 변경.** 두 저장소를 한 세션에서 함께 고치면 계약 없이 맞물린 코드가 생긴다. 계약이 바뀌는 변경은 반드시 AgentToolbox 쪽을 먼저 배포한다.

## 구조와 명령

```
apps/api/      FastAPI. 외부 base path 는 /connector/api/v1
apps/worker/   liveness cron · tools 캐시 (P3-2 에서 들어온다)
apps/web/      React SPA. Vite base /connector/ (P3-3 에서 들어온다)
migrations/    Connector DB alembic (P3-2)
packages/      api-client — apps/api 의 openapi.json 에서 생성
```

```bash
cd apps/api
uv sync --extra dev
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

PR 을 올리기 전에 위 넷이 모두 통과해야 한다.

## 진행 상태

작업 순서와 완료 조건은 AgentToolbox 의 [실행 계획·체크리스트](https://github.com/dev-team-404/AgentToolbox/blob/main/docs/architecture/connector-hub-separation-plan.md) 가 SSOT 다. 이 저장소 작업은 그 문서의 P3 항목에 해당한다. 추적 이슈는 [AgentToolbox #2939](https://github.com/dev-team-404/AgentToolbox/issues/2939).
