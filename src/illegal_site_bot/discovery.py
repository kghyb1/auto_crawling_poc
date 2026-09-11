"""홍보사이트 자동 발견.

수집 중에 "또 다른 홍보사이트"로 보이는 링크를 찾아 수집 대상 DB에 넣습니다.
그래야 24시간 돌리는 동안 수집 범위가 스스로 넓어집니다.

두 단계로 나눕니다.

1단계 (요청 없음) — :meth:`Discovery.consider`
    링크 텍스트/주변 문맥에 ``먹튀검증`` ``보증업체`` ``링크모음`` 같은 홍보사이트
    어휘가 있으면 후보로만 등록합니다. 이 관문이 없으면 후보가 폭증합니다.

2단계 (후보당 요청 1회) — :meth:`Discovery.evaluate_pending`
    후보 페이지를 실제로 받아서 점수를 냅니다. 가장 결정적인 신호는
    **이미 우리가 불법사이트로 기록해 둔 도메인으로 링크가 몇 개나 나가는가**
    입니다. 불법사이트 본체는 경쟁 업체를 링크하지 않으므로, 이 값이 높다는
    것은 홍보/모음 페이지라는 뜻입니다. 그리고 이 신호는 수집한 불법사이트가
    쌓일수록 정확해집니다.

폭주 방지는 설정(``discovery`` 섹션)으로 합니다. 탐색 깊이, 사이클당 등록/승인
개수, 전체 상한, 평가용 요청 예산이 모두 상한선을 가집니다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from .classifier import Classifier
from .config import Config
from .extractor import Candidate, extract, extract_page_signals
from .fetcher import Fetcher
from .normalizer import registrable_domain
from .storage import SourceRow, Storage
from .timeutil import days_ago

log = logging.getLogger(__name__)

StopCheck = Callable[[], bool]

#: 미러 판정에 쓰는 링크 도메인 집합의 최대 크기 (DB 에 저장되는 양 제한)
MAX_LINK_DOMAINS = 100


@dataclass
class Evaluation:
    """2단계 평가 결과."""

    score: int = 0
    reasons: list[str] = field(default_factory=list)
    known_illegal: int = 0
    outbound_domains: int = 0
    banner_ratio: float = 0.0
    title: str = ""
    link_domains: list[str] = field(default_factory=list)
    landing_page: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def reasons_text(self) -> str:
        return "; ".join(self.reasons)


@dataclass
class DiscoveryStats:
    """한 사이클의 발견 결과."""

    candidates_added: int = 0
    evaluated: int = 0
    approved: int = 0
    queued: int = 0
    rejected: int = 0
    mirrors: int = 0
    failed: int = 0
    disabled: int = 0


def root_url(url: str) -> str:
    """홍보사이트는 메인 페이지를 수집 대상으로 삼습니다.

    ``https://promo.com/board/read?id=3`` 처럼 깊은 링크로 발견되더라도
    ``https://promo.com/`` 을 등록해야 배너 전체를 볼 수 있습니다.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


class Discovery:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        classifier: Classifier,
        fetcher: Fetcher,
    ) -> None:
        self.config = config
        self.storage = storage
        self.classifier = classifier
        self.fetcher = fetcher

        self._added_this_cycle = 0
        self._known_domains: set[str] = set()
        self._domains_loaded = False

    @property
    def settings(self):
        return self.config.discovery

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    # -- 1단계: 후보 등록 --------------------------------------------------
    def begin_cycle(self) -> None:
        """사이클 시작 시 카운터와 도메인 캐시를 초기화합니다."""
        self._added_this_cycle = 0
        self._known_domains = set()
        self._domains_loaded = False

    def _source_domains(self) -> set[str]:
        if not self._domains_loaded:
            self._known_domains = self.storage.source_domains()
            self._domains_loaded = True
        return self._known_domains

    def consider(self, candidate: Candidate, source: SourceRow) -> bool:
        """수집 중 발견한 링크를 홍보사이트 후보로 등록할지 판단합니다.

        등록했으면 ``True``. 호출하는 쪽(파이프라인)은 결과를 무시해도 됩니다.
        """
        if not self.enabled:
            return False

        settings = self.settings
        if settings.max_candidates_per_cycle and (
            self._added_this_cycle >= settings.max_candidates_per_cycle
        ):
            return False

        depth = source.depth + 1
        if depth > settings.max_depth:
            # 상한을 넘는 깊이는 평가는커녕 기록도 하지 않습니다.
            return False

        # 1단계 관문: 홍보사이트 어휘가 링크 텍스트나 주변 설명에 있어야 합니다.
        hint_text = f"{candidate.anchor_text} {candidate.context_text}"
        matched = self.classifier.rules.promotion.matched_link_vocabulary(hint_text)
        if not matched:
            return False

        if self.classifier.exclusion_reason(candidate.url):
            return False
        if self.classifier.is_contact(candidate.url):
            return False

        target = root_url(candidate.url)
        domain = registrable_domain(target)
        if not domain or domain in self._source_domains():
            return False

        if self.storage.count_sources_all_states() >= settings.max_total_sources:
            log.debug("홍보사이트 총량 상한(%d)에 도달해 후보를 더 받지 않습니다.",
                      settings.max_total_sources)
            return False

        source_id = self.storage.add_candidate(
            target,
            name="",
            discovered_from_id=source.id,
            depth=depth,
            note=f"자동 발견 (근거: {', '.join(matched[:3])})",
        )
        if source_id is None:
            return False

        self._source_domains().add(domain)
        self._added_this_cycle += 1
        log.info(
            "홍보사이트 후보 발견 [깊이 %d] %s  ← %s (%s)",
            depth,
            target,
            source.name or source.url,
            ", ".join(matched[:3]),
        )
        return True

    # -- 2단계: 평가 -------------------------------------------------------
    def evaluate_url(self, url: str) -> Evaluation:
        """후보 페이지를 받아 홍보사이트 점수를 냅니다 (요청 1회)."""
        rules = self.classifier.rules.promotion
        evaluation = Evaluation()

        result = self.fetcher.fetch(url)
        if not result.ok:
            evaluation.error = result.error or f"HTTP {result.status}"
            return evaluation

        final_url = result.final_url or url
        own_domain = registrable_domain(final_url)
        extracted = extract(result.html, final_url)
        signals = extract_page_signals(result.html)
        evaluation.title = signals.title or extracted.title

        # 외부 도메인 집합 (제외 규칙에 걸리는 것은 빼고 셉니다)
        domains: set[str] = set()
        banner_links = 0
        total_links = 0
        for item in extracted.candidates:
            total_links += 1
            if item.banner:
                banner_links += 1
            if self.classifier.exclusion_reason(item.url):
                continue
            domain = item.domain
            if domain and domain != own_domain:
                domains.add(domain)

        evaluation.outbound_domains = len(domains)
        evaluation.link_domains = sorted(domains)[:MAX_LINK_DOMAINS]
        evaluation.banner_ratio = banner_links / total_links if total_links else 0.0

        # A. 이미 아는 불법 도메인과의 겹침 — 가장 결정적인 신호
        known = self.storage.known_illegal_domains(sorted(domains))
        evaluation.known_illegal = len(known)
        score = rules.known_illegal_score(len(known))
        if known:
            sample = sorted(known)[:3]
            suffix = "…" if len(known) > len(sample) else ""
            evaluation.reasons.append(
                f"이미 수집된 불법사이트 {len(known)}곳으로 링크 ({', '.join(sample)}{suffix})"
            )

        # B. 외부 도메인 수 (단독으로는 기준을 넘지 못하도록 낮게 배점)
        outbound_score = rules.outbound_domain_score(len(domains))
        if outbound_score:
            score += outbound_score
            evaluation.reasons.append(f"외부 도메인 {len(domains)}개")

        # C. 도박/성인 키워드
        keyword_hits = self._keyword_hits(signals.text)
        if keyword_hits:
            keyword_score = min(keyword_hits * 3, rules.keyword_density_max)
            score += keyword_score
            evaluation.reasons.append(f"불법 키워드 {keyword_hits}종")

        # D. 제목/헤딩의 홍보사이트 어휘
        title_text = f"{signals.title} {signals.headings} {signals.meta_description}"
        title_hits = rules.matched_title_vocabulary(title_text)
        if title_hits:
            score += min(
                len(title_hits) * rules.title_vocabulary_each, rules.title_vocabulary_max
            )
            evaluation.reasons.append(f"제목/헤딩 어휘({', '.join(title_hits[:3])})")

        # E. 이미지 배너 비율
        if evaluation.banner_ratio > 0:
            score += int(evaluation.banner_ratio * rules.banner_ratio_max)
            if evaluation.banner_ratio >= 0.3:
                evaluation.reasons.append(f"배너 링크 비율 {evaluation.banner_ratio:.0%}")

        # F. 역신호 — 로그인 랜딩 페이지는 홍보사이트가 아니라 불법사이트 본체
        has_login = signals.has_password_input or rules.has_login_marker(
            f"{signals.title} {signals.headings}"
        )
        if has_login and len(domains) < rules.landing_page_max_outbound:
            score -= rules.landing_page_penalty
            evaluation.landing_page = True
            evaluation.reasons.append("로그인 위주 + 외부 링크 거의 없음 (불법사이트 본체로 보임)")

        evaluation.score = max(0, min(100, score))
        return evaluation

    def _keyword_hits(self, text: str) -> int:
        """본문에 등장한 불법 키워드 종류 수 (카테고리 사전 재사용)."""
        if not text:
            return 0
        lowered = text.lower()
        hits = 0
        for entry in self.classifier.rules.categories.values():
            for keyword in entry["keywords"]:
                if keyword and keyword in lowered:
                    hits += 1
                    if hits >= 20:  # 충분히 셌으면 조기 종료
                        return hits
        return hits

    def _find_mirror(self, source_id: int, domains: list[str]) -> tuple[int, str] | None:
        """같은 곳을 링크하는 복제 사이트인지 확인합니다 (자카드 유사도)."""
        threshold = self.settings.mirror_similarity
        current = set(domains)
        if len(current) < 5:
            return None  # 링크가 적으면 우연히 겹칠 수 있어 판정하지 않습니다.

        for other_id, other_url, other_domains in self.storage.link_domain_sets(source_id):
            union = current | other_domains
            if not union:
                continue
            similarity = len(current & other_domains) / len(union)
            if similarity >= threshold:
                return other_id, other_url
        return None

    def evaluate_pending(self, stop_check: StopCheck = lambda: False) -> DiscoveryStats:
        """대기 중인 후보들을 예산 범위 안에서 평가합니다."""
        stats = DiscoveryStats()
        settings = self.settings
        if not self.enabled or settings.max_evaluations_per_cycle <= 0:
            return stats

        reevaluate_before = (
            days_ago(settings.reevaluate_after_days)
            if settings.reevaluate_after_days > 0
            else None
        )
        rows = self.storage.sources_to_evaluate(
            settings.max_evaluations_per_cycle, reevaluate_before
        )
        if not rows:
            return stats

        approvals_left = settings.max_new_per_cycle if settings.auto_approve else 0
        log.info("홍보사이트 후보 %d건 평가를 시작합니다.", len(rows))

        for row in rows:
            if stop_check():
                break
            source_id = int(row["id"])
            url = row["url"]

            evaluation = self.evaluate_url(url)
            if not evaluation.ok:
                failures = self.storage.record_evaluation_failure(
                    source_id, settings.max_evaluation_failures
                )
                stats.failed += 1
                log.info("후보 평가 실패(%d회) %s: %s", failures, url, evaluation.error)
                continue

            stats.evaluated += 1

            mirror = self._find_mirror(source_id, evaluation.link_domains)
            if mirror is not None:
                mirror_id, mirror_url = mirror
                self.storage.record_evaluation(
                    source_id,
                    state="rejected",
                    promo_score=evaluation.score,
                    promo_reasons=f"기존 수집원의 미러로 보임 ({mirror_url})",
                    link_domains=",".join(evaluation.link_domains),
                    mirror_of_id=mirror_id,
                    name=evaluation.title,
                )
                stats.mirrors += 1
                log.info("미러 사이트로 판단해 제외: %s (원본 %s)", url, mirror_url)
                continue

            if (
                settings.auto_approve
                and approvals_left > 0
                and evaluation.score >= settings.auto_approve_score
            ):
                state = "approved"
                approvals_left -= 1
            elif evaluation.score >= settings.queue_score:
                state = "pending"
            else:
                state = "rejected"

            self.storage.record_evaluation(
                source_id,
                state=state,
                promo_score=evaluation.score,
                promo_reasons=evaluation.reasons_text,
                link_domains=",".join(evaluation.link_domains),
                name=evaluation.title,
            )

            if state == "approved":
                stats.approved += 1
                self._on_approved(url)
                log.info("홍보사이트 자동 승인 [%d점] %s", evaluation.score, url)
            elif state == "pending":
                stats.queued += 1
                log.info(
                    "홍보사이트 후보 승인 대기 [%d점] %s — %s",
                    evaluation.score,
                    url,
                    evaluation.reasons_text or "근거 없음",
                )
            else:
                stats.rejected += 1
                log.debug("후보 기각 [%d점] %s", evaluation.score, url)

        return stats

    def _on_approved(self, url: str) -> None:
        """홍보사이트로 확정되면 불법사이트 목록에서는 빼줍니다."""
        moved = self.storage.reclassify_site_as_promo(registrable_domain(url))
        if moved:
            log.info("불법사이트 목록에서 %d건을 홍보사이트로 재분류했습니다: %s", moved, url)

    def approve(self, url: str, by: str = "cli", note: str = "") -> bool:
        """사람이 후보를 승인합니다."""
        if not self.storage.review_source(url, "approved", by=by, note=note):
            return False
        self._on_approved(url)
        return True

    def reject(self, url: str, by: str = "cli", note: str = "") -> bool:
        return self.storage.review_source(url, "rejected", by=by, note=note)

    # -- 정리 --------------------------------------------------------------
    def cleanup_dead_sources(self) -> list[str]:
        """연속 실패가 쌓인 홍보사이트를 자동으로 내립니다.

        이게 없으면 수집 대상이 늘기만 하고 죽은 사이트가 남아 사이클 시간이
        계속 길어집니다.
        """
        if not self.enabled:
            return []
        disabled = self.storage.auto_disable_failing_sources(
            self.settings.auto_disable_after_failures
        )
        for url in disabled:
            log.warning(
                "연속 실패 %d회 이상이라 수집에서 제외했습니다: %s",
                self.settings.auto_disable_after_failures,
                url,
            )
        return disabled
