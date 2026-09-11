"""링크 후보가 불법사이트인지 판별하고 카테고리를 붙입니다.

판별 근거는 모두 ``config/rules.yaml`` 에 있습니다. 운영하면서 오탐/미탐이
보이면 코드가 아니라 그 파일을 고치면 됩니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .normalizer import host_of, registrable_domain, tld_of

UNKNOWN_CATEGORY = "unknown"
UNKNOWN_LABEL = "미분류"
CONTACT_CATEGORY = "contact"
CONTACT_LABEL = "연락채널"


class RulesError(Exception):
    """규칙 파일이 잘못된 경우."""


@dataclass(frozen=True)
class Verdict:
    """한 후보에 대한 판별 결과."""

    score: int
    category: str
    label: str
    matched_keywords: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    excluded: bool = False
    exclusion_reason: str = ""
    always_keep: bool = False

    @property
    def matched_keywords_text(self) -> str:
        return ", ".join(self.matched_keywords)

    @property
    def reasons_text(self) -> str:
        return "; ".join(self.reasons)


@dataclass
class PromotionRules:
    """'이 링크가 또 다른 홍보사이트인가'를 판별하는 규칙 (자동 발견용)."""

    link_vocabulary: tuple[str, ...] = ()
    title_vocabulary: tuple[str, ...] = ()
    login_markers: tuple[str, ...] = ()
    known_illegal_scores: dict[int, int] = field(default_factory=dict)
    outbound_domain_scores: dict[int, int] = field(default_factory=dict)
    keyword_density_max: int = 15
    title_vocabulary_each: int = 8
    title_vocabulary_max: int = 20
    banner_ratio_max: int = 10
    landing_page_penalty: int = 30
    landing_page_max_outbound: int = 3

    @staticmethod
    def _tiered(table: dict[int, int], value: int) -> int:
        """'N개 이상이면 M점' 표에서 해당하는 점수를 찾습니다."""
        best = 0
        for threshold, score in sorted(table.items()):
            if value >= threshold:
                best = score
        return best

    def known_illegal_score(self, count: int) -> int:
        return self._tiered(self.known_illegal_scores, count)

    def outbound_domain_score(self, count: int) -> int:
        return self._tiered(self.outbound_domain_scores, count)

    def matched_link_vocabulary(self, text: str) -> list[str]:
        lowered = (text or "").lower()
        return [word for word in self.link_vocabulary if word.lower() in lowered]

    def matched_title_vocabulary(self, text: str) -> list[str]:
        lowered = (text or "").lower()
        return [word for word in self.title_vocabulary if word.lower() in lowered]

    def has_login_marker(self, text: str) -> bool:
        lowered = (text or "").lower()
        return any(marker.lower() in lowered for marker in self.login_markers)


@dataclass
class Rules:
    base_score: int = 15
    anchor_weight: float = 1.0
    context_weight: float = 0.35
    cross_source_bonus_per_source: int = 6
    cross_source_bonus_max: int = 24
    keyword_score_cap: int = 60
    candidate_threshold: int = 25
    high_threshold: int = 65
    categories: dict[str, dict[str, Any]] = field(default_factory=dict)
    domain_patterns: list[tuple[re.Pattern[str], int, str]] = field(default_factory=list)
    suspicious_tlds: dict[str, int] = field(default_factory=dict)
    contact_domains: set[str] = field(default_factory=set)
    exclude_domains: set[str] = field(default_factory=set)
    exclude_domain_suffixes: tuple[str, ...] = ()
    exclude_url_patterns: list[re.Pattern[str]] = field(default_factory=list)
    promotion: PromotionRules = field(default_factory=PromotionRules)

    def category_label(self, category: str) -> str:
        if category == CONTACT_CATEGORY:
            return CONTACT_LABEL
        entry = self.categories.get(category)
        if entry:
            label = entry.get("label")
            if isinstance(label, str):
                return label
        return UNKNOWN_LABEL


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_rules(path: str | Path) -> Rules:
    """``config/rules.yaml`` 을 읽습니다."""
    rules_path = Path(path)
    if not rules_path.is_file():
        raise RulesError(f"규칙 파일이 없습니다: {rules_path}")
    try:
        raw = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RulesError(f"규칙 파일을 읽을 수 없습니다 ({rules_path}): {exc}") from exc
    if not isinstance(raw, dict):
        raise RulesError(f"규칙 파일 최상위는 매핑이어야 합니다: {rules_path}")

    scoring = raw.get("scoring") or {}
    thresholds = raw.get("thresholds") or {}
    exclude = raw.get("exclude") or {}

    categories: dict[str, dict[str, Any]] = {}
    for name, entry in (raw.get("categories") or {}).items():
        if not isinstance(entry, dict):
            raise RulesError(f"categories.{name} 은 매핑이어야 합니다.")
        keywords_raw = entry.get("keywords") or {}
        if not isinstance(keywords_raw, dict):
            raise RulesError(f"categories.{name}.keywords 는 매핑이어야 합니다.")
        keywords = {
            str(keyword).strip().lower(): _as_int(weight, 0)
            for keyword, weight in keywords_raw.items()
            if str(keyword).strip()
        }
        categories[str(name)] = {
            "label": entry.get("label", str(name)),
            "keywords": keywords,
        }

    domain_patterns: list[tuple[re.Pattern[str], int, str]] = []
    for entry in raw.get("domain_patterns") or []:
        if not isinstance(entry, dict) or "pattern" not in entry:
            raise RulesError("domain_patterns 항목에는 pattern 이 필요합니다.")
        try:
            compiled = re.compile(str(entry["pattern"]), re.IGNORECASE)
        except re.error as exc:
            raise RulesError(f"domain_patterns 정규식 오류 ({entry['pattern']}): {exc}") from exc
        domain_patterns.append(
            (compiled, _as_int(entry.get("score"), 0), str(entry.get("reason", "도메인 패턴")))
        )

    exclude_url_patterns: list[re.Pattern[str]] = []
    for pattern in exclude.get("url_patterns") or []:
        try:
            exclude_url_patterns.append(re.compile(str(pattern), re.IGNORECASE))
        except re.error as exc:
            raise RulesError(f"exclude.url_patterns 정규식 오류 ({pattern}): {exc}") from exc

    promotion_raw = raw.get("promotion") or {}
    if not isinstance(promotion_raw, dict):
        raise RulesError("promotion 섹션은 매핑이어야 합니다.")
    promotion_scoring = promotion_raw.get("scoring") or {}

    def _word_list(key: str) -> tuple[str, ...]:
        return tuple(
            str(word).strip()
            for word in (promotion_raw.get(key) or [])
            if str(word).strip()
        )

    def _tier_table(key: str) -> dict[int, int]:
        table: dict[int, int] = {}
        for threshold, score in (promotion_scoring.get(key) or {}).items():
            try:
                table[int(threshold)] = _as_int(score, 0)
            except (TypeError, ValueError):
                raise RulesError(f"promotion.scoring.{key} 의 키는 정수여야 합니다.") from None
        return table

    promotion = PromotionRules(
        link_vocabulary=_word_list("link_vocabulary"),
        title_vocabulary=_word_list("title_vocabulary"),
        login_markers=_word_list("login_markers"),
        known_illegal_scores=_tier_table("known_illegal_scores"),
        outbound_domain_scores=_tier_table("outbound_domain_scores"),
        keyword_density_max=_as_int(promotion_scoring.get("keyword_density_max"), 15),
        title_vocabulary_each=_as_int(promotion_scoring.get("title_vocabulary_each"), 8),
        title_vocabulary_max=_as_int(promotion_scoring.get("title_vocabulary_max"), 20),
        banner_ratio_max=_as_int(promotion_scoring.get("banner_ratio_max"), 10),
        landing_page_penalty=_as_int(promotion_scoring.get("landing_page_penalty"), 30),
        landing_page_max_outbound=_as_int(
            promotion_scoring.get("landing_page_max_outbound"), 3
        ),
    )

    return Rules(
        base_score=_as_int(scoring.get("base_score"), 15),
        anchor_weight=_as_float(scoring.get("anchor_weight"), 1.0),
        context_weight=_as_float(scoring.get("context_weight"), 0.35),
        cross_source_bonus_per_source=_as_int(
            scoring.get("cross_source_bonus_per_source"), 6
        ),
        cross_source_bonus_max=_as_int(scoring.get("cross_source_bonus_max"), 24),
        keyword_score_cap=_as_int(scoring.get("keyword_score_cap"), 60),
        candidate_threshold=_as_int(thresholds.get("candidate"), 25),
        high_threshold=_as_int(thresholds.get("high"), 65),
        categories=categories,
        domain_patterns=domain_patterns,
        suspicious_tlds={
            str(tld).strip().lower().lstrip("."): _as_int(score, 0)
            for tld, score in (raw.get("suspicious_tlds") or {}).items()
        },
        contact_domains={
            str(domain).strip().lower() for domain in raw.get("contact_domains") or []
        },
        exclude_domains={
            str(domain).strip().lower() for domain in exclude.get("domains") or []
        },
        exclude_domain_suffixes=tuple(
            str(suffix).strip().lower() for suffix in exclude.get("domain_suffixes") or []
        ),
        exclude_url_patterns=exclude_url_patterns,
        promotion=promotion,
    )


class Classifier:
    """규칙을 적용해 점수와 카테고리를 계산합니다."""

    def __init__(self, rules: Rules) -> None:
        self.rules = rules

    # -- 제외 판정 ---------------------------------------------------------
    def exclusion_reason(self, url: str) -> str | None:
        """제외 대상이면 이유를, 아니면 ``None`` 을 돌려줍니다."""
        rules = self.rules
        for pattern in rules.exclude_url_patterns:
            if pattern.search(url):
                return "제외 URL 패턴"

        host = host_of(url)
        domain = registrable_domain(url)
        if not domain:
            return "도메인 없음"
        if domain in rules.exclude_domains or host in rules.exclude_domains:
            return "제외 도메인"
        for suffix in rules.exclude_domain_suffixes:
            if domain.endswith(suffix) or host.endswith(suffix):
                return f"제외 도메인 접미사({suffix})"
        return None

    def is_contact(self, url: str) -> bool:
        host = host_of(url)
        domain = registrable_domain(url)
        return host in self.rules.contact_domains or domain in self.rules.contact_domains

    # -- 점수 계산 ---------------------------------------------------------
    def classify(
        self,
        url: str,
        anchor_text: str = "",
        context_text: str = "",
    ) -> Verdict:
        """후보 하나를 판별합니다 (여러 홍보사이트 중복 보너스는 제외한 기본 점수)."""
        rules = self.rules

        # 연락 채널을 먼저 봅니다. open.kakao.com 처럼 제외 도메인(kakao.com)의
        # 하위 호스트인 경우가 있어서, 제외 판정보다 우선해야 합니다.
        if self.is_contact(url):
            return Verdict(
                score=rules.base_score,
                category=CONTACT_CATEGORY,
                label=CONTACT_LABEL,
                reasons=("연락 채널(텔레그램/카톡 등)",),
                always_keep=True,
            )

        reason = self.exclusion_reason(url)
        if reason:
            return Verdict(
                score=0,
                category=UNKNOWN_CATEGORY,
                label=UNKNOWN_LABEL,
                excluded=True,
                exclusion_reason=reason,
            )

        domain = registrable_domain(url)

        anchor = (anchor_text or "").lower()
        context = f"{context_text or ''} {url}".lower()

        # 카테고리별로 링크 자체 텍스트(anchor)와 주변 문맥(context) 점수를 따로 냅니다.
        tallies: dict[str, tuple[float, float, list[str]]] = {}
        for name, entry in rules.categories.items():
            anchor_hits = 0.0
            context_hits = 0.0
            matched: list[str] = []
            for keyword, weight in entry["keywords"].items():
                if not keyword or weight <= 0:
                    continue
                if keyword in anchor:
                    anchor_hits += weight
                    matched.append(keyword)
                elif keyword in context:
                    context_hits += weight
                    matched.append(keyword)
            if matched:
                tallies[name] = (anchor_hits, context_hits, matched)

        # 카테고리는 링크 자체 텍스트를 우선해서 정합니다. 주변 문맥은 같은 페이지의
        # 다른 배너 키워드가 섞이기 쉬워서, 앵커에 단서가 없을 때만 씁니다.
        best_category = UNKNOWN_CATEGORY
        best_keyword_score = 0.0
        best_keywords: list[str] = []
        if tallies:
            has_anchor_hit = any(anchor_hits > 0 for anchor_hits, _, _ in tallies.values())
            key = (
                (lambda item: (item[1][0], item[1][1]))
                if has_anchor_hit
                else (lambda item: (item[1][1], item[1][0]))
            )
            best_category, (anchor_hits, context_hits, best_keywords) = max(
                tallies.items(), key=key
            )
            best_keyword_score = min(
                anchor_hits * rules.anchor_weight + context_hits * rules.context_weight,
                float(rules.keyword_score_cap),
            )

        reasons: list[str] = []
        domain_score = 0
        for pattern, points, why in rules.domain_patterns:
            if points and pattern.search(domain):
                domain_score += points
                reasons.append(why)

        tld_score = rules.suspicious_tlds.get(tld_of(url), 0)
        if tld_score:
            reasons.append(f"의심 TLD(.{tld_of(url)})")

        if best_keywords:
            reasons.insert(0, f"키워드 일치({len(best_keywords)}건)")

        total = rules.base_score + best_keyword_score + domain_score + tld_score
        score = max(0, min(100, int(round(total))))

        return Verdict(
            score=score,
            category=best_category,
            label=rules.category_label(best_category),
            matched_keywords=tuple(dict.fromkeys(best_keywords)),
            reasons=tuple(dict.fromkeys(reasons)),
        )

    # -- 집계 --------------------------------------------------------------
    def final_score(self, base_score: int, distinct_sources: int) -> int:
        """여러 홍보사이트에서 중복 발견된 만큼 가산한 최종 점수."""
        rules = self.rules
        extra = max(0, distinct_sources - 1) * rules.cross_source_bonus_per_source
        bonus = min(extra, rules.cross_source_bonus_max)
        return max(0, min(100, base_score + bonus))

    def risk_label(self, score: int) -> str:
        if score >= self.rules.high_threshold:
            return "높음"
        if score >= self.rules.candidate_threshold:
            return "보통"
        return "낮음"
