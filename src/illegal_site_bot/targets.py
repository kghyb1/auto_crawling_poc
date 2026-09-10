"""수집 대상 홍보사이트 목록(``config/targets.yaml``) 로딩.

런타임의 기준 데이터는 DB 이고, 이 파일은 '가져오기(import)' 용도입니다.
DB 가 비어 있으면 봇이 시작할 때 자동으로 한 번 가져오고, 이후에는
``python bot.py import-targets`` 로 명시적으로 반영합니다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .normalizer import normalize_url

log = logging.getLogger(__name__)

DEFAULT_PAGINATION_PATTERNS: tuple[str, ...] = (
    "page=",
    "/page/",
    "paged=",
    "bbs",
    "board",
    "list",
    "wr_id",
    "idx=",
)


class TargetsError(Exception):
    """대상 목록 파일이 잘못된 경우."""


@dataclass(frozen=True)
class TargetSpec:
    url: str
    name: str = ""
    enabled: bool = True
    render: bool = False
    max_pages: int | None = None
    note: str = ""


@dataclass
class TargetsFile:
    sources: list[TargetSpec] = field(default_factory=list)
    pagination_patterns: tuple[str, ...] = DEFAULT_PAGINATION_PATTERNS
    path: Path | None = None


def load_targets(path: str | Path) -> TargetsFile:
    """``targets.yaml`` 을 읽습니다. 파일이 없으면 빈 목록을 돌려줍니다."""
    targets_path = Path(path)
    if not targets_path.is_file():
        return TargetsFile(path=None)

    try:
        raw = yaml.safe_load(targets_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise TargetsError(f"대상 목록을 읽을 수 없습니다 ({targets_path}): {exc}") from exc
    if not isinstance(raw, dict):
        raise TargetsError(f"대상 목록 최상위는 매핑이어야 합니다: {targets_path}")

    specs: list[TargetSpec] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw.get("sources") or [], start=1):
        if isinstance(entry, str):
            entry = {"url": entry}
        if not isinstance(entry, dict) or not entry.get("url"):
            raise TargetsError(f"sources[{index}] 에 url 이 없습니다.")
        normalized = normalize_url(str(entry["url"]))
        if not normalized:
            log.warning("주소 형식이 올바르지 않아 건너뜁니다: %s", entry["url"])
            continue
        if normalized in seen:
            log.warning("중복된 대상이라 건너뜁니다: %s", normalized)
            continue
        seen.add(normalized)

        max_pages = entry.get("max_pages")
        specs.append(
            TargetSpec(
                url=normalized,
                name=str(entry.get("name") or ""),
                enabled=bool(entry.get("enabled", True)),
                render=bool(entry.get("render", False)),
                max_pages=int(max_pages) if max_pages else None,
                note=str(entry.get("note") or ""),
            )
        )

    patterns = raw.get("pagination_patterns")
    if patterns:
        pagination = tuple(str(pattern) for pattern in patterns if str(pattern).strip())
    else:
        pagination = DEFAULT_PAGINATION_PATTERNS

    return TargetsFile(sources=specs, pagination_patterns=pagination, path=targets_path)
