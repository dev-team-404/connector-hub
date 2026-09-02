# 사이트 세션 JWT 검증 계약 테스트 벡터 (AgentToolbox 에서 복사)

**정본은 AgentToolbox `apps/server/tests/fixtures/site_jwt/vectors.json` 이다.** 이 파일은 그 사본이며, 두 서비스가 같은 판정을 내는지 확인하는 것이 목적이다.

계약이 바뀌면 AgentToolbox 쪽을 먼저 고치고 이쪽이 뒤따른다. 여기서 먼저 고치면 "우리 구현에 맞춘 벡터" 가 되어 계약으로서의 의미가 사라진다.

형식과 각 케이스의 뜻은 [AgentToolbox 쪽 README](https://github.com/dev-team-404/AgentToolbox/blob/main/apps/server/tests/fixtures/site_jwt/README.md) 를 본다.

## 이쪽이 다르게 기대하는 부분

`expect` 는 **모드별** 판정인데, 그 모드는 발급자(AgentToolbox)의 설정이다. 소비자인 이쪽에는 모드가 없다 — 비대칭 서명만 받는다. 그래서 이 저장소의 테스트는 `asymmetric` 열만 읽는다.

`hs256_legacy_valid` 케이스가 그 차이를 드러낸다. 발급자의 `dual` 모드에서는 통과지만, 이쪽에서는 **거절**이 맞다. HS256 검증에는 공유 비밀이 필요하고 그 비밀을 이쪽이 갖지 않는 것이 분리의 목적이기 때문이다.
