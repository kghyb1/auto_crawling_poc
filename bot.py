#!/usr/bin/env python3
"""봇 실행 진입점.

설치 없이 이 파일로 바로 실행할 수 있습니다::

    python bot.py init
    python bot.py start
    python bot.py on / off / status / stop
"""

from __future__ import annotations

import sys
from pathlib import Path

# 별도 설치(pip install) 없이도 동작하도록 src 를 모듈 경로에 추가합니다.
SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from illegal_site_bot.cli import main  # noqa: E402  (경로 설정 후 임포트)

if __name__ == "__main__":
    raise SystemExit(main())
