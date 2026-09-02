from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
# `tests` 를 패키지로 import 할 수 있게 한다 — 공통 픽스처(`tests.conftest_db`)를 여러
# 테스트 파일이 쓴다. conftest.py 하나에 다 넣으면 DB 가 필요 없는 테스트까지 그 import 를
# 지나게 되므로 별도 모듈로 뒀다.
sys.path.insert(0, str(_ROOT))
