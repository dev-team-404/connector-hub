# worker

liveness cron 과 tools 캐시 갱신. connector-hub#3 에서 들어온다.

`src/core` 를 api 와 공유하되 **별도 이미지로 배포한다.** 5분 주기 probe 가 폭주해도 API 요청에 영향이 없어야 하기 때문이다(설계 §9.3 의 격리 근거).
