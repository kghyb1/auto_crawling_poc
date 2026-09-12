"""엑셀 출력용 사이트 묶기(그룹핑).

같은 업체가 경로만 다르게 여러 줄로 나오는 것을 막습니다. 예를 들어
``abc.com/`` 과 ``abc.com/join`` 은 사실 한 곳인데, 홍보사이트마다 링크한
주소가 달라서 URL 단위로는 따로 잡힙니다.

이건 보기 좋으라고 묶는 게 아닙니다. **점수 계산이 정확해집니다.**
"여러 홍보사이트에서 중복 발견되면 가산" 규칙이 URL 단위로 세면
"A는 /join 을, B는 / 를 링크했다"는 이유로 각각 1곳으로 세어져 가산이
안 붙습니다. 묶어서 세야 실제로 2곳에서 홍보된 것으로 잡힙니다.

묶는 기준은 세 가지입니다.

    url     묶지 않음 (주소가 다르면 다른 줄)
    host    ``a.abc.com`` 과 ``b.abc.com`` 은 따로, ``/`` 와 ``/join`` 은 하나로
    domain  등록가능도메인 기준. 서브도메인까지 전부 하나로

기본값은 ``host`` 입니다. 경로 중복은 없애면서, 서브도메인마다 다른 업체가
도는 경우(호스팅형)를 한 줄로 뭉개지 않기 때문입니다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Sequence
from urllib.parse import urlsplit

from .normalizer import host_of, registrable_domain

GROUP_MODES = ("url", "host", "domain")
DEFAULT_GROUP_MODE = "host"

#: 그룹 기준에 따라 첫 컬럼에 붙일 이름
GROUP_LABELS = {"url": "URL", "host": "호스트", "domain": "도메인"}

#: 처리 상태 우선순위 — 그룹 안에 여러 상태가 섞이면 더 진행된 쪽을 보여줍니다.
#: ignored 는 '진행'이 아니라 '제외' 결정이라 가장 낮게 둡니다. 그래야 한 URL 을
#: 오탐 처리했다고 해서 이미 신고한 그룹 전체가 제외로 보이지 않습니다.
_STATUS_ORDER = {"ignored": 0, "new": 1, "confirmed": 2, "reported": 3}

#: 한 셀에 나열할 최대 개수 (엑셀 셀이 지나치게 길어지는 것을 막습니다)
MAX_LISTED = 30


@dataclass
class SiteGroup:
    """엑셀 한 줄에 해당하는 사이트 묶음."""

    key: str
    representative_url: str
    domain: str
    urls: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    site_ids: list[int] = field(default_factory=list)
    category: str = "unknown"
    category_label: str = "미분류"
    base_score: int = 0
    score: int = 0
    seen_count: int = 0
    distinct_sources: int = 0
    source_urls: list[str] = field(default_factory=list)
    matched_keywords: str = ""
    reasons: str = ""
    redirect_from: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    alive: int | None = None
    http_status: int | None = None
    last_checked_at: str = ""
    status: str = "new"
    memo: str = ""

    @property
    def url_count(self) -> int:
        return len(self.urls)

    @property
    def urls_text(self) -> str:
        return "\n".join(self.urls[:MAX_LISTED])

    @property
    def hosts_text(self) -> str:
        return "\n".join(self.hosts[:MAX_LISTED])

    @property
    def sources_text(self) -> str:
        return "\n".join(self.source_urls[:MAX_LISTED])


def group_key_of(url: str, mode: str) -> str:
    """URL 에서 묶음 기준 값을 뽑습니다."""
    if mode == "url":
        return url
    if mode == "domain":
        return registrable_domain(url) or url
    return host_of(url) or url


def _path_length(url: str) -> int:
    try:
        parts = urlsplit(url)
    except ValueError:
        return len(url)
    return len(parts.path.rstrip("/")) + len(parts.query)


def _merge_keywords(values: Iterable[str]) -> str:
    seen: dict[str, None] = {}
    for value in values:
        for token in (value or "").split(","):
            token = token.strip()
            if token:
                seen.setdefault(token, None)
    return ", ".join(seen)


def group_sites(
    rows: Sequence[sqlite3.Row],
    source_map: dict[int, list[str]],
    mode: str = DEFAULT_GROUP_MODE,
    bonus_per_source: int = 6,
    bonus_max: int = 24,
) -> list[SiteGroup]:
    """사이트 행들을 묶어 엑셀 한 줄 단위로 만듭니다.

    ``source_map`` 은 ``site_id -> 그 사이트를 홍보한 홍보사이트 URL 목록``.
    묶음 점수는 ``그룹 내 최고 기본점수 + 그룹 전체 기준 중복 발견 보너스`` 입니다.
    """
    if mode not in GROUP_MODES:
        raise ValueError(f"mode 는 {GROUP_MODES} 중 하나여야 합니다.")

    buckets: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        buckets.setdefault(group_key_of(row["url"], mode), []).append(row)

    groups: list[SiteGroup] = []
    for key, members in buckets.items():
        # 대표 URL 은 경로가 가장 짧은 것(보통 메인 페이지), 같으면 점수가 높은 것.
        representative = min(
            members, key=lambda row: (_path_length(row["url"]), -int(row["score"] or 0))
        )
        best = max(members, key=lambda row: int(row["base_score"] or 0))

        source_urls: list[str] = []
        for row in members:
            for url in source_map.get(int(row["id"]), []):
                if url not in source_urls:
                    source_urls.append(url)

        base_score = int(best["base_score"] or 0)
        bonus = min(max(0, len(source_urls) - 1) * bonus_per_source, bonus_max)
        score = max(0, min(100, base_score + bonus))

        checked = [row for row in members if row["last_checked_at"]]
        alive_rows = [row for row in checked if row["alive"] == 1]
        if alive_rows:
            # 하나라도 열려 있으면 그 사이트는 살아있는 것으로 봅니다.
            latest = max(alive_rows, key=lambda row: row["last_checked_at"])
            alive, http_status = 1, latest["http_status"]
            last_checked = latest["last_checked_at"]
        elif checked:
            latest = max(checked, key=lambda row: row["last_checked_at"])
            alive, http_status = 0, latest["http_status"]
            last_checked = latest["last_checked_at"]
        else:
            alive, http_status, last_checked = None, None, ""

        status = max(
            (row["status"] for row in members),
            key=lambda value: _STATUS_ORDER.get(value, 0),
        )
        memos = [row["memo"] for row in members if row["memo"]]

        groups.append(
            SiteGroup(
                key=key,
                representative_url=representative["url"],
                domain=registrable_domain(representative["url"]),
                urls=sorted({row["url"] for row in members}),
                hosts=sorted({row["host"] for row in members if row["host"]}),
                site_ids=[int(row["id"]) for row in members],
                category=best["category"],
                category_label=best["category_label"],
                base_score=base_score,
                score=score,
                seen_count=sum(int(row["seen_count"] or 0) for row in members),
                distinct_sources=len(source_urls),
                source_urls=source_urls,
                matched_keywords=_merge_keywords(row["matched_keywords"] for row in members),
                reasons=best["reasons"],
                redirect_from=next(
                    (row["redirect_from"] for row in members if row["redirect_from"]), ""
                ),
                first_seen_at=min(row["first_seen_at"] for row in members),
                last_seen_at=max(row["last_seen_at"] for row in members),
                alive=alive,
                http_status=http_status,
                last_checked_at=last_checked,
                status=status,
                memo=" / ".join(dict.fromkeys(memos)),
            )
        )

    # 점수 높은 순 → 같으면 최근 발견 순 (파이썬 정렬이 안정적이라 두 번 나눠 정렬)
    groups.sort(key=lambda group: group.last_seen_at, reverse=True)
    groups.sort(key=lambda group: group.score, reverse=True)
    return groups
