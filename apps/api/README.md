# ConnectorHub API

FastAPI 진입점. 외부 base path 는 `/connector/api/v1` 이며 게이트웨이가 그 경로로 보낸다.

저장소 전체 설명과 경계는 [루트 README](../../README.md) 를 본다.

```bash
uv sync --extra dev
uv run pytest -q
uv run connector-hub-api
```
