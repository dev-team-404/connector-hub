# 배포

세 형태가 있다. 코드는 같고 값이 다르다.

| 형태 | 어떻게 뜨는가 | 무엇에 붙는가 |
| --- | --- | --- |
| local | 도커 없이 `uv run`. 프런트는 Vite dev | 로컬에서 띄운 AgentToolbox |
| dev | `docker compose` | **AgentToolbox dev 에만** |
| prod | 같은 compose, 다른 값 | **AgentToolbox prod 에만** |

## 환경 짝짓기 — 이 문서에서 가장 중요한 것

**ConnectorHub dev 는 AgentToolbox dev 와만 붙는다. prod 도 마찬가지다.** 섞으면 동작하지
않거나 위험하다. 둘은 다른 문제다.

**동작하지 않는 이유** — 세션을 검증할 수 없다.

- 서명 열쇠가 환경마다 다르다. 다른 환경의 JWKS 로는 서명이 맞지 않는다.
- 토큰의 `iss` 가 환경마다 다르다. 기대값이 어긋나면 전부 거절된다.
- introspection 은 **그 환경의 세션 원장**을 본다. dev 세션은 prod DB 에 없다.

**위험한 이유** — 데이터 경계가 무너진다.

dev 서비스가 prod 서비스 키를 들게 되면, 그 키 하나로 prod 의 세션 조회와 팀 목록이 열린다.
prod 사용자 데이터가 dev 로 흘러간다. 그래서 **서비스 키는 환경마다 다른 값이어야 한다** —
같은 값을 쓰면 dev 유출이 곧 prod 접근이다.

**호스트도 환경마다 하나여야 한다.** 사용자가 한 번 로그인해 두 앱을 쓰려면 세션 쿠키가
같은 호스트에 붙어야 한다. dev 호스트가 dev AgentToolbox 와 dev ConnectorHub 를 함께
서빙하고, prod 도 마찬가지다. 호스트가 갈리면 쿠키가 따라오지 않아 `/connector/` 에서만
로그아웃 상태가 된다.

### 환경별로 한 벌씩 가는 값

| 값 | 무엇 |
| --- | --- |
| `SITE_AUTH_BASE_URL` | 그 환경의 AgentToolbox API |
| `SITE_AUTH_ISSUER` | 그 환경의 `AUTH_ACCESS_TOKEN_ISSUER` 와 **같은 값** |
| `SITE_AUTH_SERVICE_TOKEN` | 환경 전용 키. AgentToolbox 의 `SITE_AUTH_SERVICE_TOKENS` 에 `connector-hub:<값>` 으로 등록 |
| `DATABASE_URL` | 그 환경의 Connector DB |

나머지 설정과 기본값은 [`.env.example`](../.env.example) 에 있다.

## 게이트웨이와의 경계

게이트웨이(`/connector/*` 경로 계약)는 **AgentToolbox 쪽 nginx 가 소유한다**(설계 §6). 이
저장소의 compose 는 그 nginx 가 붙을 upstream 만 노출한다. 그래서 정상적인 ConnectorHub
릴리스는 게이트웨이나 AgentToolbox 재배포를 요구하지 않는다 — upstream 뒤의 이미지만
바뀐다(설계 §13).

nginx 가 지켜야 할 것 둘.

1. **API 경로를 SPA fallback 보다 먼저 평가한다.** `/connector/api/v1/*` 가 Connector Web
   의 `index.html` 로 떨어지면 안 된다.
2. **접두사를 떼고 보낸다.** `/connector/api/v1/connectors` → `connector-api:8000/connectors`.
   API 는 `root_path` 로 그 접두사를 알고 있어 OpenAPI 의 server URL 과 생성 클라이언트가
   올바른 주소를 부르지만, 요청 경로 자체에는 접두사가 없다고 가정한다. 떼지 않고 보내면
   전 경로가 404 다.

## local

도커 없이 띄운다.

```bash
cd backend
uv sync --extra dev
export DATABASE_URL=postgresql://<user>@localhost:5432/connector_hub
uv run alembic -c ../migrations/alembic.ini upgrade head
uv run connector-hub-api        # :8000
uv run connector-hub-worker     # 별도 터미널
```

**로컬에도 AgentToolbox 가 필요하다.** 이 서비스는 세션을 발급하지 않고 검증만 한다 —
JWKS 로 서명을 보고 introspection 으로 생존을 묻는다. 그 둘이 없으면 로그인이 필요한 모든
경로가 503(설정 없음) 또는 401 이다. 임시 열쇠를 이쪽에서 만들어 우회하는 경로는 두지
않았다. 만들면 "로컬에서만 되는 인증" 이 생기고, 계약이 실제로 도는지 확인하는 일이
배포 때로 밀린다.

인증 없이 확인할 수 있는 것은 헬스체크와 전역 공개 카드의 익명 조회다.

## dev · prod

```bash
docker compose -p connector-hub-dev up -d --build --wait
```

배포는 `.github/workflows/deploy.yml`(수동 dispatch, 대상 선택)이 같은 일을 한다.
marketplace action 을 쓰지 않고 self-hosted 러너에서 checkout + inline shell 로만 돈다 —
GHES 미러링 호환 때문이다.

### 마이그레이션이 먼저다

`migrate` 잡이 `alembic upgrade head` 로 스키마를 올리고, **성공해야** api·worker 가
시작한다(compose 의 `service_completed_successfully`). 실패하면 새 컨테이너가 아예 뜨지
않는다 — 스키마와 코드가 어긋난 채 트래픽을 받는 상태를 만들지 않는 것이 목적이다.

마이그레이션이 꼬여 되돌릴 수 없을 때만 워크플로의 `reset_db` 로 볼륨을 지운다. 데이터가
사라지므로 dev 전용 수단이다.

## 이미지

**백엔드는 이미지 하나에 진입점 셋(api · worker · migrate)이다.** 설계 §7.2 는 이미지를
셋으로 나눈다고 적었지만, 나누는 목적인 런타임·장애 격리는 **컨테이너**를 나누면 달성된다.
api 와 worker 는 같은 Python 프로젝트·같은 의존성 집합이라 이미지를 둘로 빌드하면 내용이
바이트 단위로 같은 이미지가 둘 생기고 빌드 시간과 레지스트리 용량만 두 배가 된다.
AgentToolbox 도 실제로는 한 이미지에 command 만 달리 준다. 나눌 이유가 생기는 시점은
의존성이 갈릴 때다.

**web 이미지는 아직 없다.** frontend 소스가 들어오면(connector-hub#5) compose 에 붙는다.

## 사내 전용 값

프록시·내부 IP·서비스 키는 이 저장소에 두지 않는다. 프록시 값은 compose 의 `build.args` 로
러너 환경에서 주입되고(미주입이면 빈 문자열이라 사외에서도 그대로 빌드된다), 사내 MITM
프록시 CA 는 `.proxy-certs/*.crt` 로 넣으면 이미지에 번들된다(없으면 no-op). 그 CA 가
없으면 Site Auth 호출과 커넥터 endpoint 접속이 둘 다 인증서 검증에서 막힌다 — 전자는
로그인이 통째로, 후자는 모든 커넥터가 unhealthy 로 보이는 형태로 드러난다.
