# ConnectorHub

사내 MCP 서버(Connector) 카탈로그. 등록·검색·상세·댓글과 liveness 검사를 소유한다.

AgentToolbox 에서 분리된 독립 서비스다. 사용자에게는 같은 사이트로 보이지만 저장소·DB·배포·릴리스가 따로다. 분리 결정과 근거는 AgentToolbox 쪽 문서에 있다.

- [분리 설계](https://github.com/dev-team-404/AgentToolbox/blob/main/docs/architecture/connector-hub-service-separation.md)
- [실행 계획·진행 상태](https://github.com/dev-team-404/AgentToolbox/blob/main/docs/architecture/connector-hub-separation-plan.md)
- [ADR 0004 — 분리 결정](https://github.com/dev-team-404/AgentToolbox/blob/main/docs/adr/0004-connector-hub-separation.md)
- 추적 이슈: [AgentToolbox #2939](https://github.com/dev-team-404/AgentToolbox/issues/2939)

## 경계

이 저장소가 소유하는 것과 소유하지 않는 것을 흐리지 않는다. 흐리면 분리가 남기는 이득(장애 격리·스키마 소유권·독립 릴리스)까지 사라지고 비용만 남는다.

| 소유한다 | 소유하지 않는다 |
| --- | --- |
| Connector 등록·수정·삭제·상세 | 로그인·세션 발급 |
| 카탈로그·검색·태그·정렬 | 사용자 계정과 팀 원장 |
| 공개범위와 팀 접근 제어 판정 | Skill·Avatar·Marketplace |
| Connector 전용 댓글·별·북마크·알림 | 사이트 공통 디자인 토큰의 원본 |
| MCP tools 미리보기·캐시 | |
| liveness 검사 | |
| 자체 DB·migration·OpenAPI | |

**AgentToolbox DB 를 직접 조회하지 않는다.** 허용된 접점은 HTTP 세 개뿐이다.

| 무엇 | 언제 |
| --- | --- |
| `GET /auth/.well-known/jwks.json` | 세션 서명 검증용 공개키. 캐시한다 |
| `POST /auth/introspect` | 세션 생존과 사용자의 현재 팀. 30~60초 캐시 |
| `GET /auth/directory/teams` | 팀 표시 이름 사전. ETag 로 갱신 확인 |

계약 정본은 [site-auth-contract.md](https://github.com/dev-team-404/AgentToolbox/blob/main/docs/architecture/site-auth-contract.md) 다. 이 저장소는 그 계약의 **소비자**이며, 계약이 바뀌면 AgentToolbox 가 먼저 배포되고 이쪽이 뒤따른다.

## 구조

```
backend/      Python 하나. FastAPI(api) + liveness 워커가 core 를 공유한다
  src/api/      /connector/api/v1/*
  src/core/     도메인·설정·DB·사이트 인증
  src/worker/   liveness 스윕 (브로커 없이 advisory lock 으로 단일 실행)
frontend/     React SPA — Vite base /connector/
migrations/   Connector DB alembic
packages/
  api-client/ backend 의 openapi.json 에서 생성 (code-first 단방향)
docs/
```

Web·API·Worker 는 같은 기능 변경에서 함께 바뀌는 하나의 bounded context 라 한 저장소에 둔다. **배포 이미지는 셋으로 나눈다** — 저장소가 하나인 것과 이미지가 하나인 것은 다르다.

api 와 worker 를 한 Python 프로젝트에 두는 것은 둘이 도메인 코드를 공유하기 때문이다. AgentToolbox `apps/server` 도 같은 구조다(단일 프로젝트 + 다중 진입점).

## 현재 상태

백엔드가 서 있다 — 사이트 인증 계약의 소비자, 스키마, 카탈로그·CRUD, MCP tools 미리보기·캐시, liveness 워커, 댓글·별·북마크·알림까지. 배포도 선다 — compose 로 postgres·migrate·api·worker 가 뜨고, 마이그레이션이 실패하면 새 컨테이너가 아예 시작하지 않는다([배포 문서](docs/deployment.md)). 남은 것은 Web 과 import 도구다.

**Connector 를 죽었다고 목록에서 치우지 않는다.** AgentToolbox 가 도입했다가 철회한 동작이다 — 소유자에게는 "등록한 게 사라졌다" 로만 보이고 사유를 화면에서 알 수 없으며, endpoint 가 되살아나도 스스로 복귀하지 못한다. 대신 **보이되 상태를 알린다**: 카드와 상세에 활성/비활성 배지를 띄우고, 마지막 확인이 오래됐으면 그것도 함께 표시한다.

**모더레이터는 이 서비스 DB 에서 정한다.** 사이트 JWT 의 `role` 은 AgentToolbox 가 발급하는 공통 역할이라, 거기서 admin 이라는 것이 Connector 카탈로그를 조정해도 된다는 뜻은 아니다. 부여는 `connector_moderators` 에 직접 넣는다(관리 화면은 아직 없다).

다음 순서는 AgentToolbox 계획서의 P3-3(Web 이식) → P3-4(import 도구)다.

## 개발

```bash
cd backend && uv sync --extra dev
uv run pytest -q                # DB 없는 테스트만
uv run connector-hub-api        # 개발 서버
```

DB 를 쓰는 테스트는 실제 PostgreSQL 이 필요하다. CHECK 제약이 정말로 막는지는 DB 에 물어봐야 알 수 있어서 스텁으로 대체하지 않는다.

```bash
createdb connector_hub_test
export DATABASE_URL=postgresql://<user>@localhost:5432/connector_hub_test
uv run alembic -c ../migrations/alembic.ini upgrade head
CONNECTOR_TEST_DATABASE_URL=$DATABASE_URL uv run pytest -q
```
