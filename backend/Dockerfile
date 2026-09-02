# ConnectorHub 백엔드 이미지 — api 와 worker 가 **같은 이미지, 다른 command** 로 뜬다.
#
# 설계 §7.2 는 "배포 이미지를 셋으로 나눈다" 고 적었다. 나누는 목적은 런타임·장애 격리이고
# 그것은 **컨테이너**를 나누면 달성된다. api 와 worker 는 같은 Python 프로젝트·같은 의존성
# 집합이라 이미지를 둘로 빌드하면 빌드 시간과 레지스트리 용량만 두 배가 되고 두 이미지의
# 내용은 바이트 단위로 같다. AgentToolbox 도 실제로는 한 이미지에 command 만 달리 준다
# (`docker-compose.prod.yml` 의 `x-worker-image` 앵커). 같은 관습을 따른다.
# 나눌 이유가 생기는 시점은 의존성이 갈릴 때다 — 예컨대 워커만 무거운 라이브러리를 쓰게
# 되면 그때 스테이지를 쪼갠다.
#
# 베이스를 digest 로 핀한다. `:python3.13-bookworm-slim` 은 움직이는 태그라 upstream 이
# 새 이미지를 올릴 때마다 아래 모든 레이어 캐시가 깨져 의존성을 통째로 다시 받는다.
ARG UV_BASE_IMAGE=ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

FROM ${UV_BASE_IMAGE} AS deps

WORKDIR /app/backend

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
# 사내 MITM 프록시가 불안정하다 — 다운로드가 stall 했다가 timeout 나고, 동시 TLS 스트림이
# 많을수록 일부가 막힌다. 동시성을 낮추고 타임아웃을 올린 뒤 재시도로 감싼다. 캐시 마운트
# 덕에 재시도는 직전 성공분을 재사용하고 실패한 휠만 다시 받는다.
ENV UV_HTTP_TIMEOUT=300
ENV UV_CONCURRENT_DOWNLOADS=4
# 베이스에 이미 Python 3.13 이 있다. managed Python 다운로드를 끄지 않으면 프록시가
# python-build-standalone 를 재서명하면서 UnknownIssuer 로 막힌다.
ENV UV_PYTHON_DOWNLOADS=never

# 의존성만 먼저 — 소스가 바뀌어도 이 레이어는 재사용된다.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    n=0; until uv --allow-insecure-host pypi.org --allow-insecure-host files.pythonhosted.org \
        sync --frozen --no-dev --no-install-project; do \
        n=$((n+1)); [ "$n" -ge 5 ] && { echo "uv sync(deps) failed after $n attempts" >&2; exit 1; }; \
        echo "uv sync(deps) attempt $n stalled — retrying in 5s" >&2; sleep 5; \
    done

COPY backend ./
RUN --mount=type=cache,target=/root/.cache/uv \
    n=0; until uv --allow-insecure-host pypi.org --allow-insecure-host files.pythonhosted.org \
        sync --frozen --no-dev; do \
        n=$((n+1)); [ "$n" -ge 5 ] && { echo "uv sync(project) failed after $n attempts" >&2; exit 1; }; \
        echo "uv sync(project) attempt $n stalled — retrying in 5s" >&2; sleep 5; \
    done


FROM ${UV_BASE_IMAGE} AS runner

WORKDIR /app/backend

ENV PATH="/app/backend/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

# 사내 TLS-MITM 프록시 CA 스테이징. 이 서비스의 아웃바운드는 둘 다 인증서를 검증한다 —
# Site Auth(JWKS·introspection)와 커넥터 endpoint. 프록시가 재서명한 인증서를 신뢰하지
# 못하면 전자는 로그인이 통째로 막히고 후자는 모든 커넥터가 unhealthy 로 보인다.
# `.proxy-certs/*.crt` 가 없으면(사외·CI) 전부 no-op 다.
COPY .proxy-certs/ /tmp/proxy-certs/
RUN mkdir -p /usr/local/share/ca-certificates \
    && ( cp /tmp/proxy-certs/*.crt /usr/local/share/ca-certificates/ 2>/dev/null || : ) \
    && update-ca-certificates \
    && rm -rf /tmp/proxy-certs

COPY --from=deps /app/backend /app/backend
# 마이그레이션은 backend 밖에 있다(스키마 소유권이 백엔드 코드와 별개라는 표시). 이미지에는
# 넣어야 migrate 잡이 같은 이미지로 돌 수 있다.
COPY migrations /app/migrations

# 이미지 스모크 — 진입점 셋이 모두 import 가능해야 한다. 여기서 걸리면 배포가 아니라
# 빌드에서 실패한다.
RUN python -c "import api.main, worker.main" && alembic --version && test -f /app/migrations/alembic.ini

# 기본은 API. worker 는 compose 가 command 로 덮는다.
CMD ["connector-hub-api"]
