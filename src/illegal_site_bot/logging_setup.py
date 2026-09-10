"""로그 설정. 날짜별 파일 회전 + 콘솔 출력."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_CONFIGURED = False

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    directory: Path,
    level: str = "INFO",
    retention_days: int = 14,
    console: bool = True,
    filename: str = "bot.log",
) -> None:
    """루트 로거를 설정합니다. 여러 번 호출해도 한 번만 적용됩니다."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    directory.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        directory / filename,
        when="midnight",
        backupCount=max(retention_days, 1),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # 외부 라이브러리의 잡음을 줄입니다.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("charset_normalizer").setLevel(logging.WARNING)
    _CONFIGURED = True
